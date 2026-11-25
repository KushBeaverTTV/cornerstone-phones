import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "lib/text.js" as Text
import "lib/backends.js" as Backends
import "lib/battery.js" as Battery
import "parts"

// The bar widget and its popup. Built once per monitor, so it only draws:
// everything that talks to a headset is in Service.qml, which this finds
// through the shell and pushes the widget's settings into.
//
// One mark per followed headset, side by side. Clicking a mark opens the
// popup for that headset; , and . walk between them once it is open.
// Connecting and volume belong to the stock Bluetooth and Audio panels,
// which b and v (and right and middle click) open.
Panel {
  id: root
  moduleName: "cornerstone.phones"
  // The IPC target is the service's: one per session, not one per monitor.
  manageIpc: false

  // ---- Settings. Defaults match manifest.json and Service.qml.
  readonly property string deviceMatch: Text.str(setting("deviceMatch", ""))
  readonly property int lowThreshold: Math.max(5, Math.min(50,
    Text.asInt(setting("lowBatteryThreshold", 20), 20)))
  readonly property bool showPercentage: Text.asBool(setting("showPercentage", false), false)
  readonly property bool hideWhenDisconnected: Text.asBool(setting("hideWhenDisconnected", true), true)
  readonly property bool notifyLowBattery: Text.asBool(setting("notifyLowBattery", true), true)
  readonly property bool useFastPair: Text.asBool(setting("useFastPair", true), true)
  readonly property bool useModeControl: Text.asBool(setting("useModeControl", true), true)
  readonly property bool pauseMedia: Text.asBool(setting("pauseMediaOnDisconnect", true), true)

  readonly property var service: bar && bar.shell ? bar.shell.serviceFor("cornerstone.phones") : null

  Binding { when: root.service !== null; target: root.service; property: "deviceMatch"; value: root.deviceMatch }
  Binding { when: root.service !== null; target: root.service; property: "useFastPair"; value: root.useFastPair }
  Binding { when: root.service !== null; target: root.service; property: "useModeControl"; value: root.useModeControl }
  Binding { when: root.service !== null; target: root.service; property: "lowThreshold"; value: root.lowThreshold }
  Binding { when: root.service !== null; target: root.service; property: "notifyLow"; value: root.notifyLowBattery }
  Binding { when: root.service !== null; target: root.service; property: "pauseOnDisconnect"; value: root.pauseMedia }

  // ---- Which headset the popup is about.

  readonly property var followed: service ? service.followed : []
  readonly property bool anyConnected: {
    for (var i = 0; i < followed.length; i++) if (followed[i].connected) return true
    return false
  }
  // The headset last clicked or walked to; only a preference, so one that
  // has gone leaves the popup on the first mark rather than on nothing.
  property var chosen: null
  readonly property var current: followed.indexOf(chosen) !== -1 ? chosen
    : (followed.length > 0 ? followed[0] : null)

  readonly property bool connected: !!current && current.connected
  readonly property bool shouldShow: followed.length > 0 && (anyConnected || !hideWhenDisconnected)
  visible: shouldShow
  onShouldShowChanged: if (!shouldShow) close()

  // ---- Colours and type.
  readonly property bool vertical: bar ? bar.vertical : false
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color track: Style.selectedFillFor(foreground, Commons.Color.accent, urgent)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // ---- The marks along the bar. Only the positioner for this bar's
  //      direction gets a model; the popup hangs under the current mark.

  readonly property var marks: vertical ? markColumn : markRow
  readonly property Item currentMark: {
    var index = followed.indexOf(current)
    return index >= 0 && marks.repeater.count > index ? marks.repeater.itemAt(index) : null
  }

  implicitWidth: marks.implicitWidth
  implicitHeight: marks.implicitHeight

  // With several marks, the bar's open-panel indicator should run under all
  // of them with a single mark's margins. 0 keeps the host's default.
  readonly property real openPanelIndicatorWidth: !vertical && followed.length > 1
    ? Math.round(markRow.implicitWidth / followed.length * (followed.length - 0.45)) : 0
  readonly property real openPanelIndicatorHeight: vertical && followed.length > 1
    ? Math.round(markColumn.implicitHeight / followed.length * (followed.length - 0.45)) : 0

  readonly property string tooltipText: {
    if (followed.length === 0) return Battery.tooltip({ hasDevice: false })
    var lines = []
    for (var i = 0; i < followed.length; i++) lines.push(Battery.tooltip(followed[i].summary))
    return lines.join("\n")
  }

  component Marks: Item {
    property alias repeater: repeater
    property bool column: false
    visible: column === root.vertical
    implicitWidth: column ? colBox.implicitWidth : rowBox.implicitWidth
    implicitHeight: column ? colBox.implicitHeight : rowBox.implicitHeight

    Row { id: rowBox; visible: !parent.column }
    Column { id: colBox; visible: parent.column }

    Repeater {
      id: repeater
      parent: column ? colBox : rowBox
      model: column === root.vertical ? root.followed : []
      BarMark {
        required property var modelData
        headset: modelData
        bar: root.bar
        showPercentage: root.showPercentage
        tooltip: root.opened ? "" : root.tooltipText
        foreground: root.barForeground
        urgent: root.urgent
        onPressed: function (button) { root.markPressed(modelData, button) }
      }
    }
  }

  Marks { id: markRow; column: false }
  Marks { id: markColumn; column: true }

  function markPressed(headset, button) {
    if (button === Qt.RightButton) return openPanel("omarchy.bluetooth")
    if (button === Qt.MiddleButton) return openAudioPanel()
    // A click on another headset's mark while open switches to it and stays
    // open; a click on the current one is the toggle.
    var switching = opened && current !== headset
    chosen = headset
    if (!switching) toggle()
  }

  function openPanel(target) {
    close()
    if (bar && bar.shell && typeof bar.shell.summon === "function" && bar.shell.summon(target)) return
    if (bar) bar.run("omarchy-shell shell summon " + target)
  }

  function openAudioPanel() {
    close()
    if (bar && bar.shell && typeof bar.shell.summon === "function") {
      if (bar.shell.summon("cornerstone.audio") || bar.shell.summon("omarchy.audio")) return
    }
    if (bar) bar.run("omarchy-shell shell summon cornerstone.audio || omarchy-shell shell summon omarchy.audio")
  }

  function stepHeadset(delta) {
    if (followed.length < 2) return
    var index = Math.max(0, followed.indexOf(current))
    chosen = followed[(index + delta + followed.length) % followed.length]
  }

  Connections {
    target: root.service
    enabled: root.service !== null
    function onOpenRequestChanged() {
      var want = root.service.openRequest
      for (var i = 0; i < root.followed.length; i++)
        if (want !== "" && root.followed[i].address === want) root.chosen = root.followed[i]
    }
  }

  // Opening the panel, or walking to another headset, is when someone wants
  // the truth about a reading that may have gone stale in the case.
  onOpenedChanged: if (opened && current) current.refreshIfStale()
  onCurrentChanged: if (opened && current) current.refreshIfStale()

  // ---- The current headset's controls, as the popup draws them.

  readonly property string backend: current ? current.backend : ""
  readonly property string mode: current ? current.mode : ""
  readonly property var modeButtons: current ? Backends.modeButtons(current.modesAvailable, backend) : []
  // Seven buttons do not fit one row: three, then the rest.
  readonly property var modeRows: modeButtons.length > 4
    ? [modeButtons.slice(0, 3), modeButtons.slice(3)] : [modeButtons]
  readonly property var levelButtons: current ? Backends.ancLevelButtons(current.ancLevels) : []

  readonly property bool modeRowVisible: useModeControl && !!current && current.modesReported
    && modeButtons.length > 0
  // The dial belongs to Ambient and the strength slider to ANC: drawn next
  // to another mode, either would move a number nothing is listening to.
  readonly property bool ambientVisible: modeRowVisible && current.ambientControls && mode === "ambient"
  readonly property bool strengthVisible: modeRowVisible && current.strengthControls && mode === "anc"
  // Named grades stay drawn in every mode (pressing one turns ANC on at it);
  // only under ANC is one lit.
  readonly property bool levelsVisible: modeRowVisible && levelButtons.length > 0
  readonly property bool latencyVisible: modeRowVisible && current.latencyKnown
  readonly property string voiceLabel: current ? current.ambientRange.voice : ""

  // A dragged slider writes once it settles, and draws what was asked for
  // until the device reports it: the device answers half a second later,
  // and the knob would otherwise snap back and forth on every drag.
  property int askedAmbient: -1
  property int askedStrength: -1
  readonly property int ambientNow: current ? current.ambientLevel : -1
  readonly property int strengthNow: current ? current.ancStrength : -1
  onAmbientNowChanged: askedAmbient = -1
  onStrengthNowChanged: askedStrength = -1

  function askAmbient(value) {
    if (!current) return
    askedAmbient = Text.clamp(value, current.ambientRange.min, current.ambientRange.max)
    ambientWrite.restart()
  }

  function askStrength(value) {
    if (!current || !current.strengthRange) return
    askedStrength = Text.clamp(value, current.strengthRange.min, current.strengthRange.max)
    strengthWrite.restart()
  }

  Timer {
    id: ambientWrite
    interval: 150
    // A write that went nowhere will never be reported back: let go of it.
    onTriggered: if (root.askedAmbient >= 0 && !(root.current && root.current.setAmbientLevel(root.askedAmbient))) root.askedAmbient = -1
  }

  Timer {
    id: strengthWrite
    interval: 150
    onTriggered: if (root.askedStrength >= 0 && !(root.current && root.current.setAncStrength(root.askedStrength))) root.askedStrength = -1
  }

  // [ ] and the arrows step whichever slider is drawn.
  function stepSlider(delta) {
    if (strengthVisible) askStrength((askedStrength >= 0 ? askedStrength : strengthNow) + delta)
    else if (ambientVisible) askAmbient((askedAmbient >= 0 ? askedAmbient : ambientNow) + delta)
  }

  // A key does something only while what it drives is on screen.
  function pressKey(key) {
    var k = Text.str(key).toLowerCase()
    if (k === "b") openPanel("omarchy.bluetooth")
    else if (k === "v") openAudioPanel()
    else if (k === "r") { if (current) current.refresh() }
    else if (k === "[") stepSlider(-1)
    else if (k === "]") stepSlider(1)
    else if (k === ",") stepHeadset(-1)
    else if (k === ".") stepHeadset(1)
    else if (k === "f") { if (ambientVisible && voiceLabel !== "") current.setAmbientVoice(!current.ambientVoice) }
    else if (k === "g") { if (latencyVisible) current.setLatency(!current.latencyEnabled) }
    else if (modeRowVisible) {
      for (var i = 0; i < modeButtons.length; i++)
        if (modeButtons[i].key === k) return current.setMode(modeButtons[i].value)
      for (var j = 0; j < levelButtons.length; j++)
        if (levelButtons[j].key === k) return current.setAncLevel(levelButtons[j].value)
    }
  }

  readonly property string modeHint: {
    var parts = []
    for (var i = 0; i < modeButtons.length; i++) parts.push(modeButtons[i].key + " " + modeButtons[i].label)
    if (strengthVisible) parts.push("[ ] Strength")
    if (ambientVisible) {
      parts.push("[ ] Level")
      if (current.ambientRange.key !== "") parts.push("f " + current.ambientRange.key)
    }
    if (levelsVisible)
      for (var j = 0; j < levelButtons.length; j++) parts.push(levelButtons[j].key + " " + levelButtons[j].label)
    if (latencyVisible) parts.push("g Latency")
    return parts.join(" · ")
  }

  readonly property string keyHint: ["r Refresh"]
    .concat(followed.length > 1 ? [", . Device"] : [])
    .concat(["b Bluetooth", "v Volume", "tab Next panel"]).join(" · ")

  // ---- The hero's second line while connected: one of these, changing
  //      every few seconds. The two about modes are left out on a headset
  //      the panel has no mode row for.
  readonly property var phrases: [
    "Keeping an ear on things",
    "Both buds checked in",
    "Case closed, levels open",
    "Noise, meet cancelled",
    "Letting the room in",
    "Listening for the buds",
    "Charge in every ear",
    "Talking to the firmware",
    "One meter per bud"
  ]
  readonly property var modePhrases: ["Noise, meet cancelled", "Letting the room in"]
  readonly property var shownPhrases: phrases.filter(function (p) {
    return root.modeRowVisible || root.modePhrases.indexOf(p) === -1
  })
  property int phraseIndex: 0

  Timer {
    interval: 2800
    repeat: true
    running: root.opened && root.connected
    onTriggered: phraseFade.restart()
  }

  SequentialAnimation {
    id: phraseFade
    PropertyAnimation { target: hero; property: "metaOpacity"; to: 0; duration: 180; easing.type: Easing.OutQuad }
    ScriptAction { script: root.phraseIndex = (root.phraseIndex + 1) % Math.max(1, root.shownPhrases.length) }
    PropertyAnimation { target: hero; property: "metaOpacity"; to: 1; duration: 260; easing.type: Easing.InQuad }
  }

  onConnectedChanged: if (!connected) { phraseFade.stop(); hero.metaOpacity = 1 }

  // The open card's rectangle, for screenshots of it from outside the shell.
  readonly property string cardRect: panel.open
    ? [panel.cardOrigin.x, panel.cardOrigin.y, panel.contentWidth, panel.contentHeight]
        .map(Math.round).join(" ")
    : ""
  onCardRectChanged: if (service) service.panelRect = cardRect

  // Every battery row reserves room for the longest label, so the meters
  // line up.
  TextMetrics {
    id: labelMetrics
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    text: "Battery"
  }

  component LabelledRow: Item {
    // A label on the left and something on the right.
    property alias label: leftText.text
    default property alias trailing: holder.data
    width: parent ? parent.width : 0
    implicitHeight: Math.max(leftText.implicitHeight, holder.childrenRect.height)
    Text {
      id: leftText
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
    }
    Item {
      id: holder
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      width: childrenRect.width
      height: childrenRect.height
    }
  }

  component Note: Text {
    width: parent ? parent.width : 0
    textFormat: Text.PlainText
    color: root.dim
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
    wrapMode: Text.WordWrap
  }

  component PanelBattery: BatteryRow {
    lowThreshold: root.lowThreshold
    labelWidth: labelMetrics.width
    fontFamily: root.fontFamily
    foreground: root.foreground
    dim: root.dim
    urgent: root.urgent
    track: root.track
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.currentMark ? root.currentMark : root
    owner: root
    bar: root.bar
    open: root.opened && !!root.current && root.current.hasDevice
    focusTarget: keys
    contentWidth: panel.fittedContentWidth(Style.space(340))
    contentHeight: panel.fittedContentHeight(content.implicitHeight)

    PanelKeyCatcher {
      id: keys
      anchors.fill: parent
      onCloseRequested: root.close()
      onActivateRequested: { if (root.current) root.current.refresh() }
      onTabRequested: function (direction) { root.switchPanel(direction) }
      onTextKey: function (t) { root.pressKey(t) }
      // h / l and the arrows move the one slider this panel can have.
      onMoveRequested: function (dx, dy) { if (dx !== 0) root.stepSlider(dx) }

      Column {
        id: content
        width: parent.width
        spacing: Style.space(12)

        Item {
          id: header
          width: parent.width
          implicitHeight: hero.implicitHeight
          // A Component's `root` can resolve to the component itself, so
          // what the icon needs is published here.
          readonly property color iconTint: root.current && root.current.low ? root.urgent : root.foreground
          readonly property bool earbuds: !!root.current && root.current.singleLevel < 0 && root.current.perBud

          PanelHero {
            id: hero
            width: parent.width
            title: root.current && root.current.name !== "" ? root.current.name : "Headphones"
            meta: root.connected && root.shownPhrases.length > 0
              ? root.shownPhrases[root.phraseIndex % root.shownPhrases.length]
              : Battery.statusLine(root.current ? root.current.summary : { hasDevice: false })
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconOpacity: root.connected ? 1.0 : 0.5
            iconComponent: Component {
              Item {
                implicitWidth: Math.round(Style.font.display)
                implicitHeight: Math.round(Style.font.display * 1.15)
                HeadsetGlyph {
                  width: Math.round(Style.font.display * 0.85)
                  height: width
                  anchors.centerIn: parent
                  earbuds: header.earbuds
                  tint: header.iconTint
                }
              }
            }
          }
        }

        PanelSeparator { foreground: root.foreground }

        // One battery for a headset; left, right and case for earbuds;
        // otherwise BlueZ's single figure. No charging bolt on the case:
        // earbuds report it second-hand and late, and a bolt that stays lit
        // with the cable out is a plain lie where a lagging level is a small
        // one.
        Column {
          width: parent.width
          spacing: Style.space(8)

          PanelBattery {
            visible: !!root.current && root.current.single
            label: "Battery"
            level: root.current ? root.current.singleLevel : -1
            charging: !!root.current && root.current.singleCharging
          }
          PanelBattery {
            visible: !!root.current && root.current.perBud
            label: "Left"
            level: root.current ? root.current.leftLevel : -1
            charging: !!root.current && root.current.leftCharging
          }
          PanelBattery {
            visible: !!root.current && root.current.perBud
            label: "Right"
            level: root.current ? root.current.rightLevel : -1
            charging: !!root.current && root.current.rightCharging
          }
          PanelBattery {
            visible: !!root.current && root.current.perBud
            label: "Case"
            level: root.current ? root.current.caseLevel : -1
            stale: !!root.current && root.current.caseStale
          }
          PanelBattery {
            visible: !!root.current && !root.current.perBud && !root.current.single
              && root.current.bluezLevel >= 0
            label: "Battery"
            level: root.current ? root.current.bluezLevel : -1
          }
        }

        // Why the per-bud rows are missing, when there is a reason.
        Note {
          visible: root.connected && text !== ""
          text: {
            if (!root.current) return ""
            if (root.current.readerError !== "") return root.current.readerError
            if (root.current.level >= 0) return ""
            if (!root.useFastPair) return "Fast Pair is off, so only BlueZ can report a level."
            return "No level yet. Press r to reopen the channel."
          }
        }

        Note {
          visible: !!root.current && root.current.hasDevice && !root.connected
          text: "Paired but not connected. Press b to connect."
        }

        // ---- Listening mode, once the device has answered for it.
        Column {
          width: parent.width
          visible: root.modeRowVisible
          spacing: Style.space(8)

          PanelSectionHeader {
            width: parent.width
            text: "MODE"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Repeater {
            model: root.modeRows
            ButtonGroup {
              required property var modelData
              width: parent.width
              options: modelData
              // Lit by what the device reports, including a change made on
              // the device itself.
              value: root.mode
              foreground: root.foreground
              background: Commons.Color.background
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              onChanged: function (value) { if (root.current) root.current.setMode(value) }
            }
          }

          Column {
            width: parent.width
            visible: root.levelsVisible
            spacing: Style.space(6)
            LabelledRow { label: "ANC level" }
            ButtonGroup {
              width: parent.width
              options: root.levelButtons
              value: root.mode === "anc" && root.current ? root.current.ancLevel : ""
              foreground: root.foreground
              background: Commons.Color.background
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              onChanged: function (value) { if (root.current) root.current.setAncLevel(value) }
            }
          }

          Column {
            width: parent.width
            visible: root.ambientVisible
            spacing: Style.space(6)

            LabelledRow {
              label: "Ambient level"
              Text {
                textFormat: Text.PlainText
                text: String(Math.round(ambientSlider.liveValue))
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
            }
            PanelSlider {
              id: ambientSlider
              bar: root.bar
              width: parent.width
              minimum: root.current ? root.current.ambientRange.min : 0
              maximum: root.current ? root.current.ambientRange.max : 20
              step: 1
              integer: true
              value: root.askedAmbient >= 0 ? root.askedAmbient : Math.max(minimum, root.ambientNow)
              onMoved: function (value) { root.askAmbient(value) }
            }
            LabelledRow {
              visible: root.voiceLabel !== ""
              label: root.voiceLabel
              ToggleSwitch {
                checked: !!root.current && root.current.ambientVoice
                foreground: root.foreground
                accent: Commons.Color.accent
                onToggled: { if (root.current) root.current.setAmbientVoice(!root.current.ambientVoice) }
              }
            }
          }

          Column {
            width: parent.width
            visible: root.strengthVisible
            spacing: Style.space(6)

            LabelledRow {
              label: "ANC strength"
              Text {
                textFormat: Text.PlainText
                text: String(Math.round(strengthSlider.liveValue))
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
            }
            PanelSlider {
              id: strengthSlider
              bar: root.bar
              width: parent.width
              minimum: root.current && root.current.strengthRange ? root.current.strengthRange.min : 0
              maximum: root.current && root.current.strengthRange ? root.current.strengthRange.max : 0
              step: 1
              integer: true
              value: root.askedStrength >= 0 ? root.askedStrength : Math.max(minimum, root.strengthNow)
              onMoved: function (value) { root.askStrength(value) }
            }
          }

          LabelledRow {
            visible: root.latencyVisible
            label: "Low latency"
            ToggleSwitch {
              checked: !!root.current && root.current.latencyEnabled
              foreground: root.foreground
              accent: Commons.Color.accent
              onToggled: { if (root.current) root.current.setLatency(!root.current.latencyEnabled) }
            }
          }
        }

        Note {
          visible: root.connected && !root.modeRowVisible && !!root.current && root.current.modeError !== ""
          text: "Mode: " + (root.current ? root.current.modeError : "")
        }

        PanelSeparator { visible: root.modeRowVisible; foreground: root.foreground }

        Note {
          visible: root.modeRowVisible
          text: root.modeHint
          horizontalAlignment: Text.AlignHCenter
        }

        PanelSeparator { foreground: root.foreground }

        Note {
          text: root.keyHint
          horizontalAlignment: Text.AlignHCenter
        }
      }
    }
  }
}
