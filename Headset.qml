import QtQuick
import Quickshell
import Quickshell.Io
import "lib/text.js" as Text
import "lib/devices.js" as Devices
import "lib/backends.js" as Backends
import "lib/battery.js" as Battery

// One headset, for as long as the service follows its address.
//
// The service (Service.qml) makes one of these per followed address and
// never re-points it: everything here is about this one device, so two sets
// connected at once can never mix their readings. When the device goes, the
// service drops this object and Quickshell stops its processes.
//
// It owns the device's bridge (bridges/cornerstone-bridge <brand> ...), the
// SDP probe that picks the brand, the device's share of the Fast Pair
// reader's lines (the reader itself is the service's: BlueZ allows one), and
// the low-battery warning for this device.
Item {
  id: headset

  property var service: null
  property string address: ""

  // Looked up by address every time, so a BlueZ object that is replaced (an
  // adapter reset, a re-pair) is followed through, and never another device.
  readonly property var device: Devices.byAddress(service ? service.devices : [], address)
  readonly property bool hasDevice: !!device
  readonly property bool connected: hasDevice && device.connected === true
  readonly property string name: hasDevice ? Devices.label(device) : ""
  // The name the headset reports, never the alias: bridges pick their model
  // row by it.
  readonly property string reportedName: hasDevice
    ? (Text.plainText(device.deviceName).trim() || name) : ""

  readonly property bool useFastPair: service ? service.useFastPair : true
  readonly property bool useModeControl: service ? service.useModeControl : true
  readonly property int lowThreshold: service ? service.lowThreshold : 20

  // ---- The Fast Pair reader's lines for this device.

  property var reading: ({})
  property real readingStamp: 0
  // True while this device's channel is being cycled on purpose, so the rows
  // do not drop to BlueZ's figure for the second it takes.
  property bool refreshing: false
  property string readingError: ""
  readonly property bool readerRunning: service ? service.readerRunning : false
  readonly property string readerError: !useFastPair ? ""
    : (readingError !== "" ? readingError : (service ? service.readerError : ""))
  readonly property string modelId: Text.str(reading.modelId)
  readonly property string bleAddress: Text.str(reading.bleAddress)

  function applyReaderLine(line) {
    reading = Battery.mergeReading(reading, line)
    readingError = reading.stream === false ? Text.str(reading.error) : ""
    if (Battery.carriesLevels(line)) {
      readingStamp = Date.now()
      refreshing = false
    }
  }

  function clearReaderError() { readingError = "" }

  // ---- Which bridge, from the device's SDP record (read once per connect).

  property var uuids: []
  readonly property string backend: Backends.pick(uuids, bleAddress, reportedName)
  readonly property bool bleBackend: Backends.isBle(backend)

  // ---- The bridge.

  property var bridgeState: ({})
  property bool answered: false
  // What the bridge's own error line said, and what is shown.
  property string bridgeSaid: ""
  property string bridgeError: ""
  // False while waiting out a backoff or a deliberate restart.
  property bool bridgeEnabled: true
  property bool restartSoon: false

  readonly property int supportVerdict: Backends.supportVerdict(service ? service.modeSupport : ({}), modelId)
  readonly property bool modelParked: service ? service.parkedModels[modelId] === true : false
  readonly property bool addressParked: service ? service.parkedAddresses[address] === true : false
  // What identifies this device for the retry pause: the Fast Pair model on
  // the BLE path (the address rotates), the Classic address otherwise.
  readonly property string backoffKey: bleBackend ? modelId : address

  readonly property bool bridgeWanted: {
    if (!useModeControl || !connected || backend === "" || !bridgeEnabled || !service) return false
    if (bleBackend)
      return useFastPair && bleAddress !== "" && supportVerdict !== 0 && !modelParked
    return !addressParked
  }
  readonly property var bridgeCommand: !service || backend === "" ? []
    : [service.bridgePath, Backends.brandOf(backend)].concat(Backends.bridgeArgs(backend, {
        address: address, uuid: Backends.serviceUuid(backend, uuids), name: reportedName,
        bleAddress: bleAddress, modelId: modelId }))
  // `running` follows one event-loop turn behind `bridgeWanted`: the command
  // and the wish both depend on the SDP probe and the Fast Pair stream, and
  // QML does not promise which binding settles first. Started early, a
  // bridge got an empty argument, exited 4 and parked a working device.
  property bool bridgeArmed: false
  onBridgeWantedChanged: {
    if (!bridgeWanted) { bridgeArmed = false; return }
    Qt.callLater(function () { headset.bridgeArmed = headset.bridgeWanted })
  }
  // A new BLE address (they rotate) or a backend that changed under a
  // running bridge means the running one is talking to the wrong thing.
  // Compared as text: the array is rebuilt whenever anything it reads is
  // touched, even when every word in it stays the same.
  readonly property string bridgeKey: bridgeCommand.join("\n")
  onBridgeKeyChanged: if (bridge.running) restartBridge()

  readonly property bool bridgeRunning: bridge.running
  readonly property bool live: bridge.running && answered

  readonly property bool modesReported: bridgeState.modes === true
  readonly property string mode: Text.str(bridgeState.mode)
  readonly property var modesAvailable: Backends.modesOffered(bridgeState, backend)
  readonly property var ancLevels: Backends.ancLevelsOffered(bridgeState)
  readonly property string ancLevel: Backends.ancLevelOf(bridgeState)
  readonly property bool latencyKnown: live && typeof bridgeState.latency === "boolean"
  readonly property bool latencyEnabled: latencyKnown && bridgeState.latency === true
  readonly property int ambientLevel: typeof bridgeState.level === "number" ? bridgeState.level : -1
  readonly property bool ambientVoice: bridgeState.voice === true
  readonly property bool ambientControls: live && ambientLevel >= 0
  readonly property var ambientRange: Backends.ambientRange(backend)
  readonly property int ancStrength: typeof bridgeState.strength === "number" ? bridgeState.strength : -1
  readonly property var strengthRange: Backends.strengthRange(backend)
  readonly property bool strengthControls: live && strengthRange !== null && ancStrength >= 0
  // Undefined until a wear sensor speaks; then whether it says "on the ears".
  readonly property bool wearSupported: bridgeState.worn !== undefined
  readonly property bool worn: bridgeState.worn !== false

  // Why there is no mode row, when there is a reason worth showing.
  readonly property string modeError: {
    if (!useModeControl) return ""
    if (bleBackend && !useFastPair) return "needs Fast Pair for the BLE address"
    return bridgeError
  }

  // One bridge line. Keys a line leaves out keep their last value: a bridge
  // may report only what changed, and silence about the dial is not "no dial".
  function applyBridgeLine(line) {
    var parsed = Text.parseObject(line)
    if (!parsed) return
    var sticky = ["available", "level", "voice", "worn", "strength",
                  "ancLevel", "ancLevels", "latency", "battery"]
    for (var i = 0; i < sticky.length; i++)
      if (parsed[sticky[i]] === undefined && bridgeState[sticky[i]] !== undefined)
        parsed[sticky[i]] = bridgeState[sticky[i]]
    var wasWorn = bridgeState.worn
    bridgeState = parsed
    // Taken off, or put back on, while connected: only a real edge counts.
    if (service && wasWorn === true && parsed.worn === false) service.pauseMedia()
    else if (service && wasWorn === false && parsed.worn === true) service.resumeMedia()
    if (parsed.modes === true) {
      answered = true
      bridgeError = ""
      if (service) service.resetBackoff(backoffKey)
    } else if (parsed.error !== undefined) {
      bridgeSaid = Text.plainText(parsed.error)
      bridgeError = bridgeSaid
    }
  }

  // Stop the bridge and start it again in a moment, not after a backoff.
  function restartBridge() {
    if (bridge.running) {
      restartSoon = true
      bridgeEnabled = false
      return
    }
    bridgeRestart.stop()
    bridgeEnabled = true
  }

  function write(line) {
    if (!live) return false
    bridge.write(line + "\n")
    return true
  }

  // Each returns true only when a command actually went to the device.
  function setMode(value) {
    if (!modesReported || modesAvailable.indexOf(Text.str(value)) === -1) return false
    return write("set " + value)
  }

  // The device stores the dial with Ambient, so the bridge switches to it.
  function setAmbientLevel(value) {
    if (!Backends.hasAmbientDial(backend) || !isFinite(Number(value))) return false
    return write("level " + Text.clamp(value, ambientRange.min, ambientRange.max))
  }

  function setAncStrength(value) {
    if (strengthRange === null || mode !== "anc" || !isFinite(Number(value))) return false
    return write("strength " + Text.clamp(value, strengthRange.min, strengthRange.max))
  }

  function setAmbientVoice(on) {
    if (ambientRange.command === "") return false
    return write(ambientRange.command + (on ? " on" : " off"))
  }

  // A named ANC grade turns ANC on at it, which is how the device stores it.
  function setAncLevel(value) {
    if (ancLevels.indexOf(Text.str(value)) === -1) return false
    return write("level " + value)
  }

  function setLatency(on) {
    if (!latencyKnown) return false
    return write("latency " + (on ? "on" : "off"))
  }

  // ---- Battery.

  readonly property int bluezLevel: Battery.bluezLevel(device)
  readonly property var battery: Battery.snapshot({
    reading: reading,
    streamLive: readerRunning || refreshing,
    streamNeedsLevels: Backends.fastPairWithoutBattery(backend),
    bridgeState: bridgeState,
    bridgeLive: live,
    bluez: bluezLevel
  })
  readonly property int level: battery.level
  readonly property bool charging: battery.charging
  readonly property int leftLevel: battery.left
  readonly property int rightLevel: battery.right
  readonly property int caseLevel: battery.caseLevel
  readonly property bool caseStale: battery.caseStale
  readonly property bool perBud: battery.perBud
  readonly property bool single: battery.single
  readonly property int singleLevel: battery.singleLevel
  readonly property bool leftCharging: battery.leftCharging
  readonly property bool rightCharging: battery.rightCharging
  readonly property bool caseCharging: battery.caseCharging
  readonly property bool singleCharging: battery.singleCharging
  readonly property bool low: connected && level >= 0 && level <= lowThreshold && !charging

  readonly property var summary: ({
    hasDevice: hasDevice, connected: connected, name: name, charging: charging,
    level: level, left: battery.left, right: battery.right, caseLevel: battery.caseLevel,
    single: battery.single, singleLevel: battery.singleLevel
  })

  // ---- Refreshing.
  //
  // The device pushes a reading when a level changes, and an earbud docked
  // in its case is not a change: its last level stands until asked. Asking
  // means dropping the Fast Pair channel and letting it announce again.
  // Nothing is blanked meanwhile.
  function refresh() {
    if (!connected) return
    readingError = ""
    // r is also the way past a bridge's backoff, for someone who just fixed
    // what it was complaining about.
    if (service) {
      service.resetBackoff(modelId)
      service.resetBackoff(address)
    }
    if (bridgeRestart.running) {
      bridgeRestart.stop()
      bridgeEnabled = true
    }
    if (useFastPair && service && service.refreshReader(address)) {
      refreshing = true
      refreshGuard.restart()
    }
  }

  // Opening the panel is when someone wants the truth; older than this and
  // the reading is asked for again.
  readonly property int staleAfterMs: 30000
  function refreshIfStale() {
    if (connected && (readingStamp === 0 || Date.now() - readingStamp > staleAfterMs)) refresh()
  }

  Timer {
    id: refreshGuard
    interval: 12000
    onTriggered: headset.refreshing = false
  }

  // ---- The low-battery warning, decided by Battery.lowStep.

  property var lowMemory: ({ source: "", level: -1, primed: false })
  property bool sawDisconnected: false

  function checkLow() {
    if (!service) return
    var result = Battery.lowStep(lowMemory, {
      enabled: service.notifyLow,
      connected: connected,
      level: level,
      source: battery.single ? "single" : battery.source === "bluez" ? "bluez" : "bud",
      charging: charging,
      threshold: lowThreshold,
      notified: service.notifiedLow[address] === true
    })
    lowMemory = result.memory
    service.setNotifiedLow(address, result.notified)
    // A turn later: `level` is the lower bud, and the other may not have
    // caught up with the line that moved it.
    if (result.warn) Qt.callLater(warnLow)
  }

  function warnLow() {
    if (connected && level >= 0 && service)
      service.notify(name + " battery low", Battery.lowBody(summary))
  }

  onLevelChanged: checkLow()
  onChargingChanged: checkLow()
  onLowThresholdChanged: checkLow()

  onConnectedChanged: {
    if (connected) {
      if (service) service.rememberAddress(address)
      // A connect seen happening is news: its first level may warn.
      if (sawDisconnected) lowMemory = Text.withKey(lowMemory, "primed", true)
      probeUuids()
    } else {
      if (hasDevice) sawDisconnected = true
      if (service) service.pauseMedia()
      reading = ({})
      readingStamp = 0
      readingError = ""
      bridgeState = ({})
      answered = false
      uuids = []
    }
    checkLow()
  }

  onHasDeviceChanged: if (hasDevice && !connected) sawDisconnected = true

  Component.onCompleted: {
    if (connected && service) service.rememberAddress(address)
    probeUuids()
    checkLow()
  }

  // ---- Processes.

  function probeUuids() {
    if (!connected || address === "") { uuids = []; return }
    if (!uuidProbe.running) {
      uuidProbe.command = ["/usr/bin/bluetoothctl", "info", address]
      uuidProbe.running = true
    }
  }

  // Started by probeUuids, not by a binding: it exits as soon as it has
  // printed, and a binding still true then would run it again for ever.
  Process {
    id: uuidProbe
    stdout: StdioCollector { id: uuidOut; waitForEnd: true }
    onExited: function (exitCode) {
      headset.uuids = exitCode === 0 ? Devices.uuidsFromInfo(uuidOut.text) : []
    }
  }

  Process {
    id: bridge
    running: headset.bridgeArmed && headset.bridgeCommand.length > 0
    command: headset.bridgeCommand
    stdinEnabled: true
    stdout: SplitParser { onRead: function (line) { headset.applyBridgeLine(line) } }
    stderr: StdioCollector { id: bridgeStderr; waitForEnd: true }
    // No bridge, no mode row: a stopped bridge's last line controls nothing.
    onRunningChanged: {
      if (running) {
        headset.bridgeSaid = ""
        headset.bridgeError = ""
      }
      headset.bridgeState = ({})
      headset.answered = false
    }
    onExited: function (exitCode) {
      var svc = headset.service
      // 3: linked and silent. The device lacks this, which is not a fault to
      // show; parked for the session (by model on BLE, else by address).
      if (exitCode === 3) {
        headset.bridgeError = ""
        if (svc && headset.bleBackend && headset.modelId !== "") svc.parkModel(headset.modelId)
        else if (svc && !headset.bleBackend) svc.parkAddress(headset.address)
      } else if (exitCode !== 0 && headset.bridgeSaid === "") {
        headset.bridgeError = Text.shortError(bridgeStderr.text, exitCode === 4
          ? "the listening-mode bridge could not start"
          : "the listening-mode link dropped (exit " + exitCode + ")")
      }
      var soon = headset.restartSoon
      headset.restartSoon = false
      headset.bridgeEnabled = false
      bridgeRestart.interval = soon ? 600 : (svc ? svc.backoffFor(headset.backoffKey) : 10000)
      // 1 is the link, 4 is this machine: both wait longer each time.
      if (!soon && (exitCode === 1 || exitCode === 4) && svc) svc.bumpBackoff(headset.backoffKey)
      // A failed BLE dial is usually a rotated address, announced only when
      // the Fast Pair channel reopens: ask for that rather than wait.
      if (exitCode === 1 && headset.bleBackend && svc) svc.cycleChannel(headset.address)
      bridgeRestart.restart()
    }
  }

  Timer {
    id: bridgeRestart
    onTriggered: headset.bridgeEnabled = true
  }
}
