from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QMimeData, QPointF, Qt, QUrl
from PyQt6.QtGui import QDropEvent
from PyQt6.QtWidgets import QApplication

from hamchat.ui.widgets.chat_display import ChatDisplay, IMAGE_FILE_FILTER


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


def make_png(path):
    Image.new("RGB", (12, 8), "red").save(path)
    return str(path)


def select_paths(monkeypatch, paths):
    calls = []

    def picker(*args):
        calls.append(args)
        return paths, IMAGE_FILE_FILTER

    monkeypatch.setattr(
        "hamchat.ui.widgets.chat_display.QFileDialog.getOpenFileNames", picker
    )
    return calls


def drop_paths(display, paths):
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(path) for path in paths])
    event = QDropEvent(
        QPointF(1, 1),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    display.input.dropEvent(event)
    return event


def test_attach_button_is_accessible_and_before_prompt(display):
    assert display.attach.objectName() == "AttachButton"
    assert display.attach.accessibleName() == "Attach files"
    assert display.attach.toolTip() == "Attach files"
    assert display.attach.statusTip() == "Attach files"
    assert display.attach.minimumSize().width() >= 40
    assert display.attach.iconSize().width() == 16
    assert display.attach.iconSize().height() == 16
    assert display.layout().itemAt(1).widget().layout().itemAt(0).widget() is display.attach


def test_picker_cancellation_preserves_text_and_attachments(display, monkeypatch, tmp_path):
    existing = make_png(tmp_path / "existing.png")
    display._stage_attachment(existing, "image")
    display.input.setPlainText("keep this text")
    select_paths(monkeypatch, [])

    display._choose_attachments()

    assert display.input.toPlainText() == "keep this text"
    assert display.get_pending_attachments() == [existing]
    assert display.input.hasFocus()


def test_picker_stages_one_valid_image(display, monkeypatch, tmp_path):
    image = make_png(tmp_path / "one.png")
    calls = select_paths(monkeypatch, [image])

    display._choose_attachments()

    assert display.get_pending_attachments() == [image]
    assert calls[0][3] == IMAGE_FILE_FILTER


def test_picker_stages_multiple_images_in_order_without_duplicates(display, monkeypatch, tmp_path):
    first = make_png(tmp_path / "first.png")
    second = make_png(tmp_path / "second.png")
    third = make_png(tmp_path / "third.png")
    select_paths(monkeypatch, [first, second, third, second])

    display._choose_attachments()

    assert display.get_pending_attachments() == [first, second, third]


def test_picker_retains_existing_attachments(display, monkeypatch, tmp_path):
    existing = make_png(tmp_path / "existing.png")
    additional = make_png(tmp_path / "additional.png")
    display._stage_attachment(existing, "image")
    select_paths(monkeypatch, [existing, additional])

    display._choose_attachments()

    assert display.get_pending_attachments() == [existing, additional]


def test_picker_rejects_invalid_images_without_staging(display, monkeypatch, tmp_path):
    invalid = str(tmp_path / "invalid.jpg")
    (tmp_path / "invalid.jpg").write_bytes(b"not an image")
    rejected = []
    display.attachmentRejected.connect(rejected.append)
    select_paths(monkeypatch, [invalid])

    display._choose_attachments()

    assert display.get_pending_attachments() == []
    assert rejected == ["HamChat couldn’t read this image. The file may be corrupt or use an unsupported format."]


def test_streaming_disables_attachment_and_drops_then_restores(display):
    display.set_streaming(True)
    assert not display.attach.isEnabled()
    assert not display.input.acceptDrops()

    display.set_streaming(False)
    assert display.attach.isEnabled()
    assert display.input.acceptDrops()


def test_submit_with_picker_selected_images_emits_one_payload(display, monkeypatch, tmp_path):
    first = make_png(tmp_path / "first.png")
    second = make_png(tmp_path / "second.png")
    payloads = []
    display.sig_send_payload.connect(lambda text, paths: payloads.append((text, paths)))
    select_paths(monkeypatch, [first, second])
    display._choose_attachments()

    display._submit_text("describe these")

    assert payloads == [("describe these", [first, second])]


def test_single_dropped_url_retains_existing_attachment_signal_behavior(display, tmp_path):
    image = make_png(tmp_path / "single.png")
    dropped = []
    detected = []
    display.input.fileDropped.connect(dropped.append)
    display.input.fileDetected.connect(lambda path, kind: detected.append((path, kind)))

    event = drop_paths(display, [image])

    assert event.isAccepted()
    assert dropped == [image]
    assert detected == [(image, "image")]
    assert display.get_pending_attachments() == [image]


def test_multiple_dropped_urls_stage_in_order_and_deduplicate_exact_paths(display, tmp_path):
    first = make_png(tmp_path / "first.png")
    second = make_png(tmp_path / "second.png")
    dropped = []
    display.input.fileDropped.connect(dropped.append)

    drop_paths(display, [first, second, first])

    assert dropped == [first, second, first]
    assert display.get_pending_attachments() == [first, second]


def test_non_url_drop_does_not_emit_attachment_signals(display):
    mime_data = QMimeData()
    mime_data.setText("ordinary pasted text")
    detected = []
    display.input.fileDetected.connect(lambda path, kind: detected.append((path, kind)))
    event = QDropEvent(
        QPointF(1, 1),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    display.input.dropEvent(event)

    assert detected == []
    assert display.input.canInsertFromMimeData(mime_data)
