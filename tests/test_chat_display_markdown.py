from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtQuickWidgets import QQuickWidget
from PyQt6.QtWidgets import QApplication

from hamchat.ui.widgets.chat_display import (
    ChatDisplay,
    MessageListModel,
    Msg,
    message_display_blocks,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def display(qapp):
    widget = ChatDisplay()
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def test_ordinary_markdown_is_one_safe_markdown_block():
    source = "# Heading\n\n**bold** [link](https://example.test)\n"

    assert message_display_blocks(source, render_markdown=True) == [
        {"kind": "markdown", "text": source}
    ]


def test_heading_fence_and_quote_are_distinct_blocks():
    source = "### Heading\n\n```html\n<a href=\"https://example.test\">Example</a>\n```\n\n> Following quote\n"

    assert message_display_blocks(source, render_markdown=True) == [
        {"kind": "markdown", "text": "### Heading\n\n"},
        {"kind": "code", "text": '<a href="https://example.test">Example</a>'},
        {"kind": "markdown", "text": "\n> Following quote\n"},
    ]


def test_fence_positions_and_consecutive_fences_keep_ordered_blocks():
    source = "```\nfirst\n```\n\n~~~\nsecond\n~~~\nAfter"

    assert message_display_blocks(source, render_markdown=True) == [
        {"kind": "code", "text": "first"},
        {"kind": "code", "text": "second"},
        {"kind": "markdown", "text": "After"},
    ]


def test_long_matching_fences_and_unclosed_fences_are_code_blocks():
    closed = "````\nfirst\n```\nsecond\n````\n"
    unclosed = "~~~\nunclosed\n"

    assert message_display_blocks(closed, render_markdown=True) == [
        {"kind": "code", "text": "first\n```\nsecond"}
    ]
    assert message_display_blocks(unclosed, render_markdown=True) == [
        {"kind": "code", "text": "unclosed\n"}
    ]


def test_code_block_preserves_exact_plain_content_without_generated_markup():
    payload = "\tline one\n\n</div><img src=\"https://example.test/x.png\"> &#10; ![image](x)\n``` inside"
    source = f"```html\n{payload}\n```\n"

    blocks = message_display_blocks(source, render_markdown=True)

    assert blocks == [{"kind": "code", "text": payload}]
    assert "<pre" not in blocks[0]["text"]
    assert "<br" not in blocks[0]["text"]
    assert "&#10;" in blocks[0]["text"]


def test_raw_html_and_images_outside_code_remain_neutralized():
    source = "<img src=https://example.test/x.png> ![remote](https://example.test/x.png)"

    assert message_display_blocks(source, render_markdown=True) == [
        {
            "kind": "markdown",
            "text": "&lt;img src=https://example.test/x.png&gt; \\![remote](https://example.test/x.png)",
        }
    ]


def test_streaming_messages_are_single_plain_blocks():
    source = "**partial**\n```\nnot final"

    assert message_display_blocks(source, render_markdown=False) == [
        {"kind": "plain", "text": source}
    ]


def test_model_keeps_canonical_text_and_refreshes_display_blocks():
    source = "**bold**\n\n```\nraw <img src=x>\n```"
    model = MessageListModel([Msg("user", source)])
    changes = []
    model.dataChanged.connect(lambda first, last, roles: changes.append((first.row(), last.row(), roles)))
    index = model.index(0)

    assert model.data(index, model.TEXT_ROLE) == source
    assert model.data(index, model.DISPLAY_BLOCKS_ROLE) == [
        {"kind": "markdown", "text": "**bold**\n\n"},
        {"kind": "code", "text": "raw <img src=x>"},
    ]
    assert model.to_list() == [{"role": "user", "text": source, "thumbs": []}]

    model.set_render_markdown(0, False)

    assert model.data(index, model.DISPLAY_BLOCKS_ROLE) == [{"kind": "plain", "text": source}]
    assert changes[-1] == (0, 0, [model.DISPLAY_BLOCKS_ROLE, model.RENDER_MARKDOWN_ROLE])


def test_streaming_row_changes_to_finalized_blocks_only_on_success(display):
    row = display.begin_assistant_stream()
    model = display.message_model()
    index = model.index(row)
    display.stream_chunk(row, "**partial**\n```\ncode")

    assert model.data(index, model.DISPLAY_BLOCKS_ROLE) == [
        {"kind": "plain", "text": "**partial**\n```\ncode"}
    ]
    display.end_assistant_stream(row, successful=False)
    assert model.data(index, model.DISPLAY_BLOCKS_ROLE)[0]["kind"] == "plain"

    successful_row = display.begin_assistant_stream()
    display.stream_chunk(successful_row, "**complete**\n\n```\ncode\n```")
    display.end_assistant_stream(successful_row, successful=True)
    successful_index = model.index(successful_row)
    assert model.data(successful_index, model.DISPLAY_BLOCKS_ROLE) == [
        {"kind": "markdown", "text": "**complete**\n\n"},
        {"kind": "code", "text": "code"},
    ]


def test_copy_and_thumbnail_rows_keep_raw_message_behavior(display, qapp, tmp_path):
    raw = "**copy this**\n\n```\ncode\n```"
    display.append_message("user", raw)
    display._on_bubble_action("copy", 0, "user", raw)
    assert qapp.clipboard().text() == raw

    display.draw_thumbs([str(tmp_path / "thumb.png")])
    model = display.message_model()
    index = model.index(1)
    assert model.data(index, model.TEXT_ROLE) == ""
    assert model.data(index, model.DISPLAY_BLOCKS_ROLE) == []


def test_qml_loads_with_display_blocks(display):
    assert display.qml.status() == QQuickWidget.Status.Ready
    display.append_message("assistant", "# Heading\n\n```\ncode\n```")
    display.qml.engine().collectGarbage()
