import QtQuick
import qs.Commons
import qs.Ui
import "../lib/battery.js" as Battery

// One headset's place in the bar: a button with the headset's mark on it,
// and the mark is the meter. A dim copy of the mark is drawn first; a bright
// copy fills it from the bottom, the left half by the left earbud and the
// right half by the right one (a headset with one battery fills both). A bud
// that reports nothing leaves its half dim, which is what one resting in the
// case looks like. Exact numbers are in the tooltip and the panel.
Item {
  id: mark

  property var headset: null
  property QtObject bar: null
  property bool showPercentage: false
  property string tooltip: ""
  property color foreground: "white"
  property color urgent: "red"

  signal pressed(int button)

  readonly property bool connected: headset ? headset.connected : false
  readonly property int level: headset ? headset.level : -1
  readonly property bool earbuds: headset ? (headset.singleLevel < 0 && headset.perBud) : false
  // One battery for the set fills both halves; per-bud levels fill one each;
  // otherwise BlueZ's single figure fills both.
  readonly property int leftFill: !headset ? -1
    : headset.singleLevel >= 0 ? headset.singleLevel
    : headset.perBud ? headset.leftLevel : headset.bluezLevel
  readonly property int rightFill: !headset ? -1
    : headset.singleLevel >= 0 ? headset.singleLevel
    : headset.perBud ? headset.rightLevel : headset.bluezLevel
  readonly property color tint: headset && headset.low ? urgent
    : connected ? foreground : Qt.darker(foreground, 1.55)

  readonly property bool vertical: bar ? bar.vertical : false
  // A percentage beside the glyph only fits a horizontal bar.
  readonly property bool written: showPercentage && !vertical

  // The mark keeps the parity of its slot, so it centres on a whole pixel
  // exactly as the bar's open-panel indicator under it does; and it is four
  // fifths of the icon canvas, the share the neighbouring font icons paint.
  readonly property int markSize: {
    var canvas = Math.round(Style.bar.iconCanvas)
    var slot = Math.round(width)
    if (slot > 0 && (slot - canvas) % 2 !== 0) canvas -= 1
    var size = Math.round(Math.max(8, canvas) * 0.8)
    if ((canvas - size) % 2 !== 0) size -= 1
    return Math.max(6, size)
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: mark.bar
    text: mark.written ? Battery.barText(mark.level, true) : ""
    hasVisualContent: true
    foreground: mark.tint
    slotSize: Style.bar.iconSlot * (mark.written && mark.level >= 0 ? 2 : 1)
    tooltipText: mark.tooltip
    onPressed: function (b) { mark.pressed(b) }
  }

  // Font icons beside this one sit a little above the slot's centre (they
  // paint above the baseline); the mark takes the same lift, unrounded,
  // because the glyphs' own position is fractional too.
  FontMetrics {
    id: iconMetrics
    font.family: mark.bar ? mark.bar.fontFamily : Style.font.family
    font.pixelSize: Math.max(1, Math.round(Style.bar.iconFont))
  }

  Item {
    id: canvas
    visible: !mark.written
    width: mark.markSize
    height: mark.markSize
    x: Math.round((mark.width - width) / 2)
    y: (mark.height - height - iconMetrics.descent / 2) / 2

    // The dim copy, flattened into a layer before it is faded: the band
    // overlaps the cups, and a translucent tint drawn twice there would
    // leave a brighter seam.
    HeadsetGlyph {
      anchors.fill: parent
      earbuds: mark.earbuds
      tint: mark.tint
      opacity: 0.5
      layer.enabled: true
    }

    Repeater {
      model: 2
      Item {
        id: half
        required property int index
        readonly property int split: Math.round(canvas.width / 2)
        readonly property int value: index === 0 ? mark.leftFill : mark.rightFill
        // Any charge shows at least one row, so 3% is nearly empty rather
        // than empty; and less than two rows short of full draws full,
        // because a lone dim row at the crown reads as a broken outline.
        readonly property int rows: {
          if (value <= 0) return 0
          var n = Math.max(1, Math.round(canvas.height * value / 100))
          return canvas.height - n < 2 ? canvas.height : n
        }
        x: index === 0 ? 0 : split
        width: index === 0 ? split : canvas.width - split
        height: rows
        y: canvas.height - height
        clip: true

        Behavior on height { NumberAnimation { duration: 240; easing.type: Easing.OutCubic } }

        HeadsetGlyph {
          x: -half.x
          y: -half.y
          width: canvas.width
          height: canvas.height
          earbuds: mark.earbuds
          tint: mark.tint
        }
      }
    }
  }
}
