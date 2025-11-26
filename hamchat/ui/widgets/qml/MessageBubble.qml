// hamchat/ui/widgets/qml/MessageBubble.qml
import QtQuick 2.15
import QtQuick.Controls 2.15

Item {
  id: root
  property string role: "assistant"
  property string text: ""
  property var thumbs: []       // array of thumbnail URLs (file://)
  property var theme: ({})    // injected from Python; may be {}

  width: parent ? parent.width : 400
  height: bubble.implicitHeight

  readonly property bool isUser: role === "user"
  readonly property bool isSystem: role === "system"

  // Theme fallbacks to avoid "undefined → QColor" warnings
  readonly property color bubbleColor:
      isSystem  ? (theme.msg_system_bg    || "#2B2412") :
      isUser    ? (theme.msg_user_bg      || "#1E2A1E") :
                  (theme.msg_assistant_bg || "#1F2733")
  readonly property color textColor:
      isSystem  ? (theme.msg_system_text    || "#FDECC8") :
      isUser    ? (theme.msg_user_text      || "#DFF7D6") :
                  (theme.msg_assistant_text || "#E6EEF9")
  readonly property color borderColor: (theme.border || "#2F3742")
  readonly property color accentColor: (theme.accent || "#66C2FF")
  readonly property bool hasThumbs: thumbs && thumbs.length > 0

  // lightweight predicate instead of string compare everywhere
  readonly property bool isThinking: role === "assistant" && text === "Thinking…"

  Rectangle {
    id: bubble
    property real maxBubbleWidth: parent ? parent.width * 0.7 : 280
    property real basePadding: 24
    property real spinnerSpacing: spinner.visible ? spinner.width + 6 : 0
    property real verticalPadding: 24
    radius: 12
    color: bubbleColor
    border.color: borderColor
    border.width: 1

    anchors {
      left:  isUser ? undefined : parent.left
      right: isUser ? parent.right : undefined
      leftMargin:  isUser ? 56 : 0
      rightMargin: isUser ? 0  : 56
    }

    width: Math.min(maxBubbleWidth, contentBox.implicitWidth + basePadding + spinnerSpacing)
    implicitHeight: Math.max(contentBox.implicitHeight + verticalPadding, spinner.visible ? spinner.height + 16 : 0)

    // --- TEXT / THUMBS STACK ----------------------------------------------
    Column {
      id: contentBox
      spacing: 8
      anchors {
        left: parent.left
        right: parent.right
        top: parent.top
        bottom: parent.bottom
        leftMargin: 12
        rightMargin: spinner.visible ? (spinner.width + 18) : 12
        topMargin: 12
        bottomMargin: 12
      }

      property real maxContentWidth: Math.max(0, bubble.maxBubbleWidth - bubble.basePadding - bubble.spinnerSpacing)

      Text {
        id: content
        visible: root.text.length > 0
        text: root.text
        wrapMode: Text.Wrap
        color: textColor
        width: Math.min(contentBox.maxContentWidth, implicitWidth)
      }

      Flow {
        id: thumbFlow
        visible: root.text.length === 0 && root.hasThumbs
        spacing: 8
        width: Math.min(contentBox.maxContentWidth, implicitWidth)

        Repeater {
          model: root.hasThumbs ? thumbs : []
          delegate: Rectangle {
            radius: 10
            color: "#1A1D24"
            border.color: borderColor
            border.width: 1
            width: 132; height: 96

            Image {
              anchors.fill: parent
              anchors.margins: 4
              fillMode: Image.PreserveAspectCrop
              source: (typeof modelData === "string" && modelData.startsWith("file://"))
                      ? modelData
                      : ("file://" + modelData)
            }
          }
        }
      }
    }

    // --- THEMED "Thinking…" SPINNER (lives INSIDE the bubble) --------------
    // Using a tiny Canvas arc so we can tint it with theme.accent.
    Canvas {
      id: spinner
      visible: root.isThinking
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.margins: 8
      width: 16; height: 16

      property color ringColor: accentColor
      property real spinAngle: 0

      onPaint: {
        var ctx = getContext("2d");
        ctx.reset();
        ctx.lineWidth = 2;
        ctx.strokeStyle = ringColor;
        var r = width/2 - 1;
        ctx.beginPath();
        // draw a 200° arc, offset by spinAngle
        var start = (spinAngle - 90) * Math.PI/180;
        var end   = (spinAngle + 110) * Math.PI/180;
        ctx.arc(width/2, height/2, r, start, end);
        ctx.stroke();
      }

      NumberAnimation on spinAngle {
        from: 0; to: 360; duration: 900
        loops: Animation.Infinite
        running: spinner.visible
      }
      onSpinAngleChanged: requestPaint()
    }
  }
}
