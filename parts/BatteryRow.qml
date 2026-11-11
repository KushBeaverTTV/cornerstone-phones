import QtQuick
import qs.Commons
import "../lib/battery.js" as Battery

// One battery in the panel: glyph, name, meter, percentage. Always drawn: a
// part that reports nothing gets the unknown glyph, an empty track and a
// placeholder, because a row that vanishes reads as a broken widget.
Item {
  id: row

  property string label: ""
  property int level: -1
  property bool charging: false
  // The last reading of a part that has stopped reporting (a closed case):
  // drawn at half strength, a memory rather than a fault.
  property bool stale: false
  property int lowThreshold: 20
  property real labelWidth: 0
  property string fontFamily: Style.font.family
  property color foreground: "white"
  property color dim: "gray"
  property color urgent: "red"
  property color track: "#333"

  readonly property bool known: level >= 0
  readonly property bool low: known && level <= lowThreshold && !charging
  readonly property color tint: !known ? dim : low ? urgent : foreground
  readonly property real strength: stale ? 0.5 : 1.0

  width: parent ? parent.width : 0
  implicitHeight: Math.max(glyph.implicitHeight, name.implicitHeight, figure.implicitHeight)

  Text {
    id: glyph
    anchors.left: parent.left
    anchors.verticalCenter: parent.verticalCenter
    textFormat: Text.PlainText
    text: row.known ? Battery.levelGlyph(row.level, row.charging) : Battery.UNKNOWN_GLYPH
    color: row.tint
    font.family: row.fontFamily
    font.pixelSize: Style.font.iconLarge
  }

  Text {
    id: name
    anchors.left: glyph.right
    anchors.leftMargin: Style.spacing.md
    anchors.verticalCenter: parent.verticalCenter
    width: Math.ceil(row.labelWidth)
    elide: Text.ElideRight
    textFormat: Text.PlainText
    text: row.label
    color: row.foreground
    font.family: row.fontFamily
    font.pixelSize: Style.font.body
  }

  Rectangle {
    id: meter
    anchors.left: name.right
    anchors.leftMargin: Style.spacing.md
    anchors.right: figure.left
    anchors.rightMargin: Style.spacing.md
    anchors.verticalCenter: parent.verticalCenter
    height: Math.max(Style.space(5), Math.round(Style.spacing.controlHeight * 0.16))
    radius: height / 2
    color: row.track

    Rectangle {
      height: parent.height
      radius: parent.radius
      width: row.known ? Math.max(parent.height, parent.width * row.level / 100) : 0
      color: row.tint
      opacity: row.strength
      Behavior on width { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }
      Behavior on color { ColorAnimation { duration: 200 } }
    }
  }

  Text {
    id: figure
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    textFormat: Text.PlainText
    text: Battery.percentLabel(row.level)
    color: row.tint
    opacity: row.strength
    font.family: row.fontFamily
    font.pixelSize: Style.font.bodySmall
  }
}
