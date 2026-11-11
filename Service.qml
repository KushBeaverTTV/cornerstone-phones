import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Bluetooth
import Quickshell.Services.Mpris
import "lib/text.js" as Text
import "lib/devices.js" as Devices
import "lib/backends.js" as Backends
import "lib/battery.js" as Battery

// What exists once per session, however many bars and headsets there are.
//
// The bar widget (Panel.qml) is built once per monitor, so anything that
// talks to a device lives here instead: one Headset per followed address,
// one Fast Pair reader for all of them (BlueZ hands out one Message Stream
// registration per system), the caches they share, one notification queue,
// and the IPC target `omarchy-shell cornerstone-phones <method>`.
Item {
  id: root

  property var shell: null
  readonly property string pluginId: "cornerstone.phones"

  // ---- Settings, pushed in by the panel. Until a panel does, these are the
  //      defaults, which match manifest.json.
  property string deviceMatch: ""
  property bool useFastPair: true
  property bool useModeControl: true
  property int lowThreshold: 20
  property bool notifyLow: true
  property bool pauseOnDisconnect: true

  readonly property string bridgePath: localPath("bridges/cornerstone-bridge")
  readonly property string readerPath: localPath("bridges/cornerstone-fastpair")

  function localPath(file) {
    return Qt.resolvedUrl(file).toString().replace(/^file:\/\//, "")
  }

  // ---- Which headsets.

  readonly property var devices: Bluetooth.devices ? Bluetooth.devices.values : []
  // Re-evaluates whenever a device's connected or battery state changes,
  // because the ranking reads them.
  readonly property var followAddresses: Devices.followAddresses(devices, deviceMatch, persisted.lastAddress)

  // One row per followed address. Rows are added and removed one at a time,
  // never replaced wholesale: a replaced list rebuilds every Headset, and
  // with it every bridge, whenever a second set connects.
  ListModel { id: rows }

  function syncRows() {
    var wanted = followAddresses
    for (var i = rows.count - 1; i >= 0; i--)
      if (wanted.indexOf(rows.get(i).headsetAddress) === -1) rows.remove(i)
    for (var j = 0; j < wanted.length; j++) {
      var present = false
      for (var k = 0; k < rows.count && !present; k++)
        present = rows.get(k).headsetAddress === wanted[j]
      if (!present) rows.append({ headsetAddress: wanted[j] })
    }
  }

  onFollowAddressesChanged: syncRows()
  Component.onCompleted: syncRows()

  property var headsets: []
  // Drawing order: connected first, then the set used last, then by name.
  readonly property var followed: Devices.orderHeadsets(headsets, persisted.lastAddress)
  readonly property var primary: followed.length > 0 ? followed[0] : null

  Instantiator {
    id: headsetHost
    model: rows
    delegate: Headset {
      required property string headsetAddress
      service: root
      address: headsetAddress
    }
    onObjectAdded: root.collectHeadsets()
    onObjectRemoved: root.collectHeadsets()
  }

  function collectHeadsets() {
    var list = []
    for (var i = 0; i < headsetHost.count; i++)
      if (headsetHost.objectAt(i)) list.push(headsetHost.objectAt(i))
    headsets = list
  }

  function headsetFor(which) {
    var index = Devices.find(followed, which)
    return index >= 0 ? followed[index] : null
  }

  // ---- The Fast Pair reader: one process for every connected headset.
  //      Each line it writes names its device by `address`; a line without
  //      one is about the reader itself.

  readonly property var readerAddresses: Devices.readerAddresses(followed)
  readonly property bool readerRunning: reader.running
  property bool readerEnabled: true
  property int readerBackoffMs: 8000
  // What the running reader has been told to follow: a device coming or
  // going is one stdin line, because a restart would drop every channel.
  property var readerFollowing: []
  // The reader's own complaint (shown by every headset), and the copy that
  // tells an exit that explained itself from one that did not.
  property string readerError: ""
  property string readerSaid: ""

  function applyReaderLine(line) {
    var address = Battery.readingAddress(line)
    if (address !== "") {
      var headset = Devices.byAddress(headsets, address)
      if (headset) headset.applyReaderLine(line)
      readerBackoffMs = 8000
      return
    }
    var fields = Text.parseObject(line)
    if (!fields || fields.stream === undefined) return
    readerError = fields.stream === false ? Text.plainText(fields.error) : ""
    readerSaid = readerError
    if (fields.stream !== false) readerBackoffMs = 8000
  }

  function syncReader() {
    if (!reader.running) return
    var wanted = readerAddresses
    for (var i = 0; i < readerFollowing.length; i++)
      if (wanted.indexOf(readerFollowing[i]) === -1) reader.write("unfollow " + readerFollowing[i] + "\n")
    for (var j = 0; j < wanted.length; j++)
      if (readerFollowing.indexOf(wanted[j]) === -1) reader.write("follow " + wanted[j] + "\n")
    readerFollowing = wanted.slice()
  }

  onReaderAddressesChanged: syncReader()

  // Reopen one device's channel if the reader is up and following it. A
  // reader between attempts is left to its pause: a JBL bridge failing every
  // few seconds must not cancel the reader's backoff.
  function cycleChannel(address) {
    var key = Text.str(address).toUpperCase()
    if (!useFastPair || !reader.running || readerFollowing.indexOf(key) === -1) return false
    reader.write("refresh " + key + "\n")
    return true
  }

  // A fresh reading, asked for by a person (r in the panel). True only when
  // the request went out; otherwise the reader's next attempt is brought
  // forward.
  function refreshReader(address) {
    if (!useFastPair) return false
    if (cycleChannel(address)) return true
    readerBackoffMs = 8000
    readerRestart.stop()
    readerEnabled = true
    return false
  }

  // One turn behind, like a headset's bridge: seen live, `running` settled
  // before `command`, and the reader started with no address at all.
  readonly property bool readerWanted: useFastPair && readerAddresses.length > 0 && readerEnabled
  property bool readerArmed: false
  onReaderWantedChanged: {
    if (!readerWanted) { readerArmed = false; return }
    Qt.callLater(function () { root.readerArmed = root.readerWanted })
  }

  Process {
    id: reader
    running: root.readerArmed
    command: [root.readerPath].concat(root.readerAddresses)
    stdinEnabled: true
    stdout: SplitParser { onRead: function (line) { root.applyReaderLine(line) } }
    stderr: StdioCollector { id: readerStderr; waitForEnd: true }
    onRunningChanged: {
      if (running) {
        root.readerError = ""
        root.readerSaid = ""
        root.readerFollowing = root.readerAddresses.slice()
        for (var i = 0; i < root.headsets.length; i++) root.headsets[i].clearReaderError()
      } else {
        root.readerFollowing = []
      }
    }
    onExited: function (exitCode) {
      // 0 is the reader asked to stop. Anything else waits longer each time,
      // up to five minutes: the usual causes (the earbuds dropping the
      // channel, another program holding it) do not clear in a second.
      var clean = exitCode === 0
      if (!clean && root.readerSaid === "")
        root.readerError = Text.shortError(readerStderr.text,
          "the Fast Pair reader stopped (exit " + exitCode + ")")
      root.readerEnabled = false
      readerRestart.interval = clean ? 600 : root.readerBackoffMs
      if (!clean) root.readerBackoffMs = Math.min(300000, root.readerBackoffMs * 2)
      readerRestart.restart()
    }
  }

  Timer {
    id: readerRestart
    onTriggered: root.readerEnabled = true
  }

  // ---- Caches the headsets share, because they outlive any one of them.

  // Which Fast Pair models answered the JBL bridge's mode query, as the
  // bridge recorded them on disk. An unknown model gets one try.
  property var modeSupport: ({})
  // Failed for good this session: by Fast Pair model on the BLE path, by
  // address everywhere else.
  property var parkedModels: ({})
  property var parkedAddresses: ({})
  // The pause before a bridge's next attempt, per model or address.
  property var backoff: ({})

  function park(map, key) {
    return Text.str(key) === "" ? map : Text.withKey(map, Text.str(key), true)
  }
  function parkModel(model) { parkedModels = park(parkedModels, model) }
  function parkAddress(address) { parkedAddresses = park(parkedAddresses, address) }

  function backoffFor(key) {
    var value = backoff[Text.str(key)]
    return typeof value === "number" && value > 0 ? value : 10000
  }
  function bumpBackoff(key) {
    backoff = Text.withKey(backoff, Text.str(key), Math.min(300000, backoffFor(key) * 2))
  }
  function resetBackoff(key) {
    if (backoff[Text.str(key)] !== undefined) backoff = Text.withKey(backoff, Text.str(key), undefined)
  }

  // Warned already, per address, kept across a reload.
  readonly property var notifiedLow: persisted.notifiedLow || ({})
  function setNotifiedLow(address, value) {
    var key = Text.str(address)
    if (key === "" || (notifiedLow[key] === true) === (value === true)) return
    persisted.notifiedLow = Text.withKey(notifiedLow, key, value ? true : undefined)
  }

  // The set to name while nothing is connected. Written a turn later: the
  // follow list is ranked with it, and writing it from inside that update
  // would be a binding loop.
  function rememberAddress(address) {
    if (persisted.lastAddress !== address)
      Qt.callLater(function () { persisted.lastAddress = address })
  }

  // ---- Notifications, one at a time: a busy Process would drop the second.

  property var notifyQueue: []

  function notify(title, body) {
    notifyQueue = notifyQueue.concat([{ title: Text.str(title), body: Text.str(body) }])
    sendNextNotification()
  }

  function sendNextNotification() {
    if (notifier.running || notifyQueue.length === 0) return
    var next = notifyQueue[0]
    notifyQueue = notifyQueue.slice(1)
    notifier.command = ["omarchy-notification-send", "-u", "critical",
                        "-g", Battery.HEADPHONES_GLYPH, next.title, next.body]
    notifier.running = true
  }

  Process {
    id: notifier
    onExited: root.sendNextNotification()
  }

  // ---- Pausing media when a headset disconnects or comes off the ears, and
  //      resuming exactly what was paused when it goes back on. Only playing
  //      players are paused; playerctld (a proxy for some other player) is
  //      skipped, as the shell's own media widget skips it.

  property var pausedPlayers: []
  readonly property var players: Mpris.players ? Mpris.players.values : []

  function isProxy(player) {
    return Text.str(player.dbusName).toLowerCase().indexOf("playerctld") !== -1
      || Text.str(player.desktopEntry).toLowerCase() === "playerctld"
  }

  function pauseMedia() {
    if (!pauseOnDisconnect) return
    var paused = []
    for (var i = 0; i < players.length; i++) {
      var p = players[i]
      if (!p || isProxy(p) || !p.isPlaying || !p.canPause) continue
      p.pause()
      paused.push(Text.str(p.dbusName))
    }
    pausedPlayers = paused
  }

  function resumeMedia() {
    if (!pauseOnDisconnect || pausedPlayers.length === 0) return
    var names = pausedPlayers
    pausedPlayers = []
    for (var i = 0; i < players.length; i++) {
      var p = players[i]
      if (p && names.indexOf(Text.str(p.dbusName)) !== -1 && p.canPlay) p.play()
    }
  }

  // ---- For the panel: an IPC request to show one headset, and where the
  //      open card is (written by the panel that is open).
  property string openRequest: ""
  property string panelRect: ""

  // ---- IPC. Each question has a plain form, about the first headset in the
  //      bar, and a ...For form taking a word that picks one (a substring of
  //      its name, address or backend). Two names because an IPC function's
  //      arity is fixed. status with no argument lists every headset.

  function levelOf(headset, key) { return Text.str(headset ? headset[key] : -1) }

  function modeOf(h) {
    if (!h) return "unsupported"
    if (h.live && h.mode !== "") return h.mode
    // A live bridge listing no modes (battery only) never will.
    if (h.live && h.modesAvailable.length === 0) return "unsupported"
    return h.bridgeRunning ? "pending" : "unsupported"
  }

  function writeMode(h, mode) {
    if (Backends.allModeNames().indexOf(Text.str(mode)) === -1) return "unknown mode: " + mode
    if (!h) return "unavailable"
    if (h.setMode(mode)) return "ok"
    return h.bridgeRunning && !h.live ? "busy" : "unavailable"
  }

  function modesOf(h) { return h && h.live && h.modesReported ? h.modesAvailable.join(",") : "" }

  function okOr(done) { return done ? "ok" : "unavailable" }

  function voiceOf(h) { return h && h.ambientControls ? (h.ambientVoice ? "on" : "off") : "" }
  function wornOf(h) { return h && h.wearSupported ? (h.worn ? "worn" : "not worn") : "unsupported" }
  function ancLevelOf(h) { return h && h.live && h.ancLevels.length > 0 ? h.ancLevel : "" }
  function latencyOf(h) { return h && h.latencyKnown ? (h.latencyEnabled ? "on" : "off") : "" }

  function writeAncLevel(h, level) {
    if (Backends.ANC_LEVELS.indexOf(Text.str(level)) === -1) return "unknown level: " + level
    return okOr(h && h.setAncLevel(Text.str(level)))
  }

  function chargingOf(h) {
    if (!h) return ""
    var parts = []
    if (h.singleCharging) parts.push("battery")
    if (h.leftCharging) parts.push("left")
    if (h.rightCharging) parts.push("right")
    if (h.caseCharging) parts.push("case")
    return parts.join(",")
  }

  function summon() { if (shell) shell.summon(pluginId) }

  IpcHandler {
    target: "cornerstone-phones"

    function open(): void { root.summon() }
    function show(): void { root.summon() }
    function close(): void { if (root.shell) root.shell.hide(root.pluginId) }
    function hide(): void { if (root.shell) root.shell.hide(root.pluginId) }
    function toggle(): void { if (root.shell) root.shell.toggle(root.pluginId) }
    function panelRect(): string { return root.panelRect }
    function openFor(which: string): string {
      var h = root.headsetFor(which)
      if (!h) return "no device matching: " + which
      root.openRequest = ""
      root.openRequest = h.address
      root.summon()
      return "ok"
    }

    function refresh(): void { if (root.primary) root.primary.refresh() }
    function refreshFor(which: string): void { var h = root.headsetFor(which); if (h) h.refresh() }

    function status(): string {
      if (root.followed.length === 0) return Battery.tooltip({ hasDevice: false })
      var lines = []
      for (var i = 0; i < root.followed.length; i++) lines.push(Battery.tooltip(root.followed[i].summary))
      return lines.join("\n")
    }
    function statusFor(which: string): string {
      var h = root.headsetFor(which)
      return h ? Battery.tooltip(h.summary) : "no device matching: " + which
    }

    function battery(): string { return root.levelOf(root.primary, "level") }
    function batteryFor(which: string): string { return root.levelOf(root.headsetFor(which), "level") }
    function left(): string { return root.levelOf(root.primary, "leftLevel") }
    function leftFor(which: string): string { return root.levelOf(root.headsetFor(which), "leftLevel") }
    function right(): string { return root.levelOf(root.primary, "rightLevel") }
    function rightFor(which: string): string { return root.levelOf(root.headsetFor(which), "rightLevel") }
    function batteryCase(): string { return root.levelOf(root.primary, "caseLevel") }
    function batteryCaseFor(which: string): string { return root.levelOf(root.headsetFor(which), "caseLevel") }
    function charging(): string { return root.chargingOf(root.primary) }
    function chargingFor(which: string): string { return root.chargingOf(root.headsetFor(which)) }

    // The rotating BLE address the Fast Pair stream announced.
    function bleAddress(): string { return root.primary ? root.primary.bleAddress : "" }
    function bleAddressFor(which: string): string {
      var h = root.headsetFor(which)
      return h ? h.bleAddress : ""
    }

    // A mode, "pending" while the bridge comes up, or "unsupported".
    function mode(): string { return root.modeOf(root.primary) }
    function modeFor(which: string): string { return root.modeOf(root.headsetFor(which)) }
    // The modes the headset offers, comma-separated; empty until it answers.
    function modes(): string { return root.modesOf(root.primary) }
    function modesFor(which: string): string { return root.modesOf(root.headsetFor(which)) }
    // "ok", "busy" (bridge up, not answering yet), or "unavailable".
    function setMode(mode: string): string { return root.writeMode(root.primary, mode) }
    function setModeFor(mode: string, which: string): string { return root.writeMode(root.headsetFor(which), mode) }

    function ambientLevel(): string { return root.levelOf(root.primary, "ambientLevel") }
    function ambientLevelFor(which: string): string { return root.levelOf(root.headsetFor(which), "ambientLevel") }
    function setAmbientLevel(level: int): string { return root.okOr(root.primary && root.primary.setAmbientLevel(level)) }
    function setAmbientLevelFor(level: int, which: string): string {
      var h = root.headsetFor(which)
      return root.okOr(h && h.setAmbientLevel(level))
    }

    function ambientVoice(): string { return root.voiceOf(root.primary) }
    function ambientVoiceFor(which: string): string { return root.voiceOf(root.headsetFor(which)) }
    function setAmbientVoice(on: string): string {
      return root.okOr(root.primary && root.primary.setAmbientVoice(Text.asBool(on, false)))
    }
    function setAmbientVoiceFor(on: string, which: string): string {
      var h = root.headsetFor(which)
      return root.okOr(h && h.setAmbientVoice(Text.asBool(on, false)))
    }

    function worn(): string { return root.wornOf(root.primary) }
    function wornFor(which: string): string { return root.wornOf(root.headsetFor(which)) }

    function ancLevel(): string { return root.ancLevelOf(root.primary) }
    function ancLevelFor(which: string): string { return root.ancLevelOf(root.headsetFor(which)) }
    function setAncLevel(level: string): string { return root.writeAncLevel(root.primary, level) }
    function setAncLevelFor(level: string, which: string): string { return root.writeAncLevel(root.headsetFor(which), level) }

    function ancStrength(): string { return root.levelOf(root.primary, "ancStrength") }
    function ancStrengthFor(which: string): string { return root.levelOf(root.headsetFor(which), "ancStrength") }
    function setAncStrength(level: int): string { return root.okOr(root.primary && root.primary.setAncStrength(level)) }
    function setAncStrengthFor(level: int, which: string): string {
      var h = root.headsetFor(which)
      return root.okOr(h && h.setAncStrength(level))
    }

    function latency(): string { return root.latencyOf(root.primary) }
    function latencyFor(which: string): string { return root.latencyOf(root.headsetFor(which)) }
    function setLatency(on: string): string { return root.okOr(root.primary && root.primary.setLatency(Text.asBool(on, false))) }
    function setLatencyFor(on: string, which: string): string {
      var h = root.headsetFor(which)
      return root.okOr(h && h.setLatency(Text.asBool(on, false)))
    }
  }

  PersistentProperties {
    id: persisted
    reloadableId: "cornerstone-phones"
    property var notifiedLow: ({})
    property string lastAddress: ""
  }

  FileView {
    path: (Quickshell.env("XDG_STATE_HOME") || Quickshell.env("HOME") + "/.local/state")
      + "/cornerstone-phones/mode-support.json"
    watchChanges: true
    printErrors: false
    onLoaded: root.modeSupport = Backends.parseSupport(text())
    onFileChanged: reload()
  }
}
