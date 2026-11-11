import QtQuick

// The drawn mark for a headset: a pair of earbuds, or over-ear headphones.
//
// Drawn from rectangles rather than set from an icon font, so its ink runs
// exactly from the top of the item to the bottom. That is what lets the bar
// fill it by percentage: a fraction of the item's height is the same
// fraction of the drawing, which is not true of a font glyph's metrics.
Item {
  id: glyph

  property bool earbuds: false
  property color tint: "white"

  // ---- Headphones: a band (the top half of a ring) and two cups.
  Item {
    id: phones
    visible: !glyph.earbuds
    anchors.fill: parent

    readonly property real stroke: Math.max(1, Math.round(glyph.height * 0.09))
    readonly property real cupWidth: Math.max(3, Math.round(glyph.width * 0.30))
    readonly property real cupHeight: Math.max(4, Math.round(glyph.height * 0.46))

    Item {
      id: band
      width: parent.width
      // Down to a stroke into the cups, so the join has no gap.
      height: phones.height - phones.cupHeight + phones.stroke
      clip: true

      Rectangle {
        width: band.width
        height: band.width
        radius: width / 2
        color: "transparent"
        border.width: phones.stroke
        border.color: glyph.tint
      }
    }

    Repeater {
      model: 2
      Rectangle {
        required property int index
        x: index === 0 ? 0 : glyph.width - width
        y: phones.height - height
        width: phones.cupWidth
        height: phones.cupHeight
        radius: Math.max(1, Math.round(width * 0.35))
        color: glyph.tint
      }
    }
  }

  // ---- Earbuds: two rounded bodies, each with a stem that hangs from its
  //      inner lower corner, the way a pair sits in its case.
  Item {
    id: buds
    visible: glyph.earbuds
    anchors.fill: parent

    readonly property real bodyWidth: Math.max(3, Math.round(glyph.width * 0.36))
    readonly property real bodyHeight: Math.max(5, Math.round(glyph.height * 0.64))
    readonly property real stemWidth: Math.max(1, Math.round(glyph.width * 0.16))
    readonly property real inset: Math.round(glyph.width * 0.04)

    Repeater {
      model: 2
      Item {
        id: bud
        required property int index
        readonly property bool right: index === 1
        x: right ? glyph.width - buds.inset - buds.bodyWidth : buds.inset
        width: buds.bodyWidth
        height: glyph.height

        Rectangle {
          width: bud.width
          height: buds.bodyHeight
          radius: width / 2
          color: glyph.tint
        }
        Rectangle {
          x: bud.right ? 0 : bud.width - width
          y: buds.bodyHeight - width
          width: buds.stemWidth
          height: glyph.height - y
          radius: width / 2
          color: glyph.tint
        }
      }
    }
  }
}
