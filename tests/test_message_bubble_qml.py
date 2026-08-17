from pathlib import Path


def test_message_block_policy_keeps_markdown_and_code_rendering_separate():
    source = (
        Path(__file__).resolve().parents[1]
        / "hamchat/ui/widgets/qml/MessageBubble.qml"
    ).read_text(encoding="utf-8")

    assert "width: Math.min(contentBox.maxContentWidth, implicitWidth)" in source
    assert "textFormat: block.kind === \"markdown\" ? Text.MarkdownText : Text.PlainText" in source
    assert "wrapMode: block.kind === \"code\" ? Text.WrapAnywhere : Text.Wrap" in source
    assert (
        "font.family: block.kind === \"code\" ? \"monospace\" : Qt.application.font.family"
        in source
    )
    assert "displayText" not in source
    assert "<pre" not in source and "<div" not in source and "<br" not in source

    chat_view = (
        Path(__file__).resolve().parents[1]
        / "hamchat/ui/widgets/qml/ChatView.qml"
    ).read_text(encoding="utf-8")
    assert "displayBlocks: model.displayBlocks" in chat_view
