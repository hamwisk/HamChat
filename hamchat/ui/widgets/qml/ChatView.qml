// hamchat/ui/widgets/qml/ChatView.qml
import QtQuick 2.15
import QtQuick.Controls 2.15

Rectangle {
  id: root
  color: (Theme && Theme.chat_bg) ? Theme.chat_bg : "#2B3037"
  anchors.fill: parent

  ListView {
    id: list
    anchors.fill: parent
    anchors.margins: 12
    model: messageModel
    spacing: 8
    cacheBuffer: 24000
    clip: true

    property bool stickToBottom: true
    readonly property real bottomThreshold: 40

    function nearBottom() {
      return (contentY + height) >= (contentHeight - bottomThreshold);
    }
    function ensureAtEnd() {
      if (stickToBottom) positionViewAtEnd();
    }
    function forceStickAndEnd() {
      stickToBottom = true;
      positionViewAtEnd();
    }

    onMovementStarted: { stickToBottom = nearBottom(); }
    onMovementEnded:   { stickToBottom = nearBottom(); }

    delegate: MessageBubble {
      role: model.role
      text: model.text
      thumbs: model.thumbs || []
      theme: Theme || ({})
      width: list.width
    }

    Component.onCompleted: { positionViewAtEnd(); stickToBottom = true; }
    onCountChanged:        { if (stickToBottom) positionViewAtEnd(); }
    onModelChanged:        { if (stickToBottom) positionViewAtEnd(); }

  footer: Item {
  width: list.width
  visible: attachRep.count > 0          // ← use Repeater's count
  height: visible ? strip.implicitHeight + 8 : 0

  Row {
    id: strip
    spacing: 8
    anchors {
      left: parent.left
      right: parent.right
      bottom: parent.bottom
      margins: 8
    }

    Repeater {
      id: attachRep
      model: attachmentsModel || []      // ← null-safe fallback for model

      delegate: Rectangle {
        radius: 10
        border.width: 1
        width: 84; height: 60

        Image {
          anchors.fill: parent
          anchors.margins: 4
          fillMode: Image.PreserveAspectCrop
          // your _AttachModel stores plain paths; make sure QML sees a file URL:
          source: (path && path.startsWith("file://")) ? path : ("file://" + path)
        }

        MouseArea {
          anchors.fill: parent
          onDoubleClicked: ChatBridge.qmlOpenAttachmentAt(index)
        }

        ToolButton {
          text: "✕"
          width: 20; height: 20
          anchors.top: parent.top
          anchors.right: parent.right
          anchors.margins: 2
          onClicked: ChatBridge.qmlRemoveAttachmentAt(index)
        }
      }
    }
  }
}}

}
