// hamchat/ui/widgets/qml/ImageBubble.qml
import QtQuick 2.15
import QtQuick.Controls 2.15

Item {
    id: root
    property string role: "assistant"
    property string source: ""
    property var theme: ({})    // same token object you feed into MessageBubble

    width: parent ? parent.width : 400
    height: bubble.implicitHeight

    readonly property bool isUser: role === "user"
    readonly property bool isSystem: role === "system"

    // Theme fallbacks to avoid "undefined → QColor" warnings
    readonly property color bubbleColor:
        isSystem  ? (theme.msg_system_bg    || "#2B2412") :
        isUser    ? (theme.msg_user_bg      || "#1E2A1E") :
                    (theme.msg_assistant_bg || "#222533")

    Row {
        id: row
        anchors.left: isUser ? undefined : parent.left
        anchors.right: isUser ? parent.right : undefined
        anchors.margins: 8
        spacing: 6

        Rectangle {
            id: bubble
            radius: 12
            color: bubbleColor
            border.color: theme.msg_border || "#444"
            border.width: 1

            Column {
                id: content
                anchors.margins: 8
                anchors.fill: parent

                Image {
                    id: img
                    source: root.source
                    fillMode: Image.PreserveAspectFit
                    asynchronous: true
                    cache: true

                    // Reasonable default size; adjust if you like
                    width: 96
                    height: width
                }
            }
        }
    }
}

