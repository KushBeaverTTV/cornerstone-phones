.pragma library
.import "text.js" as Text

// Which bridge can control a device, what it is started with, and what the
// panel's mode controls look like on it.
//
// A device is matched to a row from what it says about itself: the service
// UUIDs in its SDP record, for some rows together with the name it reports,
// and for the JBL row the BLE address its Fast Pair stream announced. Rows
// are tried in order and the first claim wins. A UUID that only one brand
// serves is a strong claim and sits near the top; claims that need a name
// sit lower; the Fast Pair row is last, because every Fast Pair device
// announces an address whether or not it speaks JBL's protocol.
//
// Each row:
//   id         the backend's name; also what an IPC word can match ("sony")
//   brand      the bridge module: bridges/brands/<brand>.py
//   claim      { uuids: [...] }            any one of these in the record
//              { uuidPrefix: "..." }       a record UUID starting with this
//              { names: [...], uuids: [...] }  this reported name AND one of these
//              { fastPair: true }          a BLE address from the Fast Pair stream
//   args       what the bridge takes after its brand, filled by bridgeArgs():
//              address, uuid, name, bleAddress, modelId
//   ambient    the Ambient dial: { min, max, voice: switch label, command, key }
//   strength   a numeric ANC strength slider: { min, max }
//   extraModes modes beyond off / anc / ambient / talkthru
//   modeLabels what the device's own app calls a mode
//   fastPairWithoutBattery  the Fast Pair stream opens but never carries a
//              battery, so it must not stand in front of the bridge's figures

var UUID = {
  sonyV2: "956c7b26-d49a-4ba8-b03f-b17d393cb6e2",
  sonyV1: "96cc203e-5068-46ad-b32d-e316f5e069ba",
  samsung: "2e73a4ad-332d-41fc-90e2-16bef06523f2",
  nothing: "aeac4a03-dff5-498f-843a-34487cf133eb",
  gaia: "00001100-d102-11e1-9b23-00025b00a5a5",
  soundcorePrefix: "0cf12d31-fac3-4553-bd80-d6832e7",
  oppo: "0000079a-d102-11e1-9b23-00025b00a5a5",
  boseBmap: "00000000-deca-fade-deca-deafdecacaff",
  boseSpp: "9b26d8c0-a8ed-440b-95b0-c4714a518bcc",
  spp: "00001101-0000-1000-8000-00805f9b34fb",
  tozo: "0000b610-0000-1000-8000-00805f9b34fb",
  redmi: "0000fd2d-0000-1000-8000-00805f9b34fb",
  airoha: "00000000-0000-0000-0099-aabbccddeeff",
  moto: "fc9d9fe0-4899-11ee-be56-0242ac120002"
}

var ROWS = [
  { id: "sony", brand: "sony",
    claim: { uuids: [UUID.sonyV2, UUID.sonyV1] },
    args: ["address", "uuid", "name"],
    ambient: { min: 0, max: 20, voice: "Focus on voice", command: "voice", key: "Voice" } },
  { id: "samsung", brand: "samsung",
    claim: { uuids: [UUID.samsung] }, args: ["address"] },
  { id: "nothing", brand: "nothing",
    claim: { uuids: [UUID.nothing] }, args: ["address", "name"] },
  { id: "xiaomi", brand: "xiaomi",
    claim: { uuids: [UUID.gaia] }, args: ["address"] },
  { id: "soundcore", brand: "soundcore",
    // The vendor UUID's last digits name the model, so the bridge is told
    // which one this device serves.
    claim: { uuidPrefix: UUID.soundcorePrefix }, args: ["address", "uuid"],
    ambient: { min: 1, max: 5, voice: "Wind noise reduction", command: "wind", key: "Wind" } },
  { id: "oppo", brand: "oppo",
    claim: { uuids: [UUID.oppo] }, args: ["address"] },
  // BMAP sits on a raw RFCOMM channel the bridge finds by asking; these two
  // UUIDs are the QC45's vendor services, which is what identifies a Bose.
  { id: "bose", brand: "bose",
    claim: { uuids: [UUID.boseBmap, UUID.boseSpp] }, args: ["address"] },
  { id: "tozo", brand: "tozo",
    claim: { names: ["TOZO NC9 Pro"], uuids: [UUID.spp, UUID.tozo] },
    args: ["address", "name"],
    extraModes: ["wind", "leisure", "adaptive"],
    modeLabels: { off: "Normal", ambient: "Transparency" } },
  // Battery only: the FreeBuds SE 2 have no noise control.
  { id: "huawei", brand: "huawei",
    claim: { names: ["HUAWEI FreeBuds SE 2"], uuids: [UUID.spp] },
    args: ["address", "name"] },
  // Battery and modes over Xiaomi's 0xFD2D service. The Buds 5 Pro are not
  // this: they speak GAIA and are the xiaomi row.
  { id: "redmi", brand: "redmi",
    claim: { names: ["Redmi Buds 5"], uuids: [UUID.redmi] },
    args: ["address"],
    fastPairWithoutBattery: true,
    modeLabels: { ambient: "Transparency" } },
  // The same service behind an authentication the Buds 5 are not asked for;
  // the bridge's model row for the name does it.
  { id: "redmi-buds-3-pro", brand: "redmi",
    claim: { names: ["Redmi Buds 3 Pro"], uuids: [UUID.redmi] },
    args: ["address", "name"],
    modeLabels: { ambient: "Transparency" } },
  // Above the Fast Pair row: these earbuds announce a BLE address too.
  { id: "earfun", brand: "earfun",
    claim: { names: ["EarFun Air Pro 4"], uuids: [UUID.spp] },
    args: ["address", "name"] },
  { id: "airoha", brand: "airoha",
    claim: { names: ["JBL Tour Pro 3"], uuids: [UUID.airoha] },
    args: ["address", "name"],
    ambient: { min: 1, max: 7, voice: "", command: "", key: "" },
    strength: { min: 1, max: 7 } },
  { id: "moto", brand: "moto",
    claim: { names: ["moto buds+"], uuids: [UUID.moto] },
    args: ["address", "name"] },
  { id: "jbl", brand: "jbl",
    claim: { fastPair: true }, args: ["bleAddress", "modelId"] }
]

// No dial: the shape a row without `ambient` reads as.
var NO_AMBIENT = { min: 0, max: 20, voice: "Focus on voice", command: "", key: "Voice" }

function row(id) {
  for (var i = 0; i < ROWS.length; i++)
    if (ROWS[i].id === Text.str(id)) return ROWS[i]
  return null
}

function claims(entry, uuids, bleAddress, reportedName) {
  var claim = entry.claim
  if (claim.fastPair) return Text.str(bleAddress).trim() !== ""
  if (claim.names && claim.names.indexOf(Text.str(reportedName)) === -1) return false
  for (var i = 0; i < uuids.length; i++) {
    if (claim.uuids && claim.uuids.indexOf(uuids[i]) !== -1) return true
    if (claim.uuidPrefix && uuids[i].indexOf(claim.uuidPrefix) === 0) return true
  }
  return false
}

// The backend id for a device, or "" for one no bridge can reach.
function pick(uuids, bleAddress, reportedName) {
  var ids = []
  var list = uuids || []
  for (var i = 0; i < list.length; i++) ids.push(Text.str(list[i]).trim().toLowerCase())
  for (var r = 0; r < ROWS.length; r++)
    if (claims(ROWS[r], ids, bleAddress, reportedName)) return ROWS[r].id
  return ""
}

function brandOf(id) {
  var entry = row(id)
  return entry ? entry.brand : ""
}

// A bridge that dials the BLE address Fast Pair announced, rather than the
// device's Classic address.
function isBle(id) {
  var entry = row(id)
  return !!(entry && entry.claim.fastPair)
}

// The service UUID to hand a bridge whose args take "uuid": the first of the
// row's own UUIDs this device serves, in the row's order (Sony: v2 first).
// For a prefix row, the record's UUID that starts with the prefix.
function serviceUuid(id, uuids) {
  var entry = row(id)
  if (!entry || entry.args.indexOf("uuid") === -1) return ""
  var served = []
  var list = uuids || []
  for (var i = 0; i < list.length; i++) served.push(Text.str(list[i]).trim().toLowerCase())
  if (entry.claim.uuidPrefix) {
    for (var k = 0; k < served.length; k++)
      if (served[k].indexOf(entry.claim.uuidPrefix) === 0) return served[k]
    return ""
  }
  if (!entry.claim.uuids) return ""
  for (var j = 0; j < entry.claim.uuids.length; j++)
    if (served.indexOf(entry.claim.uuids[j]) !== -1) return entry.claim.uuids[j]
  return ""
}

// The bridge's arguments after its brand, in the row's order. An unknown
// value goes out as "", which every bridge reads as "not given".
function bridgeArgs(id, known) {
  var entry = row(id)
  if (!entry) return []
  var values = known || {}
  var out = []
  for (var i = 0; i < entry.args.length; i++) out.push(Text.str(values[entry.args[i]]))
  return out
}

function fastPairWithoutBattery(id) {
  var entry = row(id)
  return !!(entry && entry.fastPairWithoutBattery)
}

function ambientRange(id) {
  var entry = row(id)
  return entry && entry.ambient ? entry.ambient : NO_AMBIENT
}

function hasAmbientDial(id) {
  var entry = row(id)
  return !!(entry && entry.ambient)
}

function strengthRange(id) {
  var entry = row(id)
  return entry && entry.strength ? entry.strength : null
}

// ---- Listening modes.

// The names every bridge may report, in the order the panel draws them.
var BASE_MODES = ["off", "anc", "ambient", "talkthru"]

// Every button the panel can draw, in drawing order, with its key.
var MODE_BUTTONS = [
  { value: "off", key: "o", label: "Off", tooltip: "No noise control" },
  { value: "anc", key: "n", label: "ANC", tooltip: "Noise Cancelling" },
  { value: "ambient", key: "a", label: "Ambient", tooltip: "Ambient Aware" },
  { value: "talkthru", key: "t", label: "TalkThru", tooltip: "TalkThru" },
  { value: "wind", key: "w", label: "Wind Noise", tooltip: "Reduce Wind Noise" },
  { value: "leisure", key: "e", label: "Leisure", tooltip: "Leisure Mode" },
  { value: "adaptive", key: "d", label: "Adaptive", tooltip: "Adaptive Mode" }
]

// The names this backend may report: the base four plus its row's extras.
function modeNames(id) {
  var entry = row(id)
  var extra = entry && entry.extraModes ? entry.extraModes : []
  var out = []
  for (var i = 0; i < MODE_BUTTONS.length; i++) {
    var value = MODE_BUTTONS[i].value
    if (BASE_MODES.indexOf(value) !== -1 || extra.indexOf(value) !== -1) out.push(value)
  }
  return out
}

// Every name any backend may report: what setMode accepts before the
// headset checks it against what the device itself offers.
function allModeNames() {
  var out = BASE_MODES.slice()
  for (var r = 0; r < ROWS.length; r++) {
    var extra = ROWS[r].extraModes || []
    for (var i = 0; i < extra.length; i++)
      if (out.indexOf(extra[i]) === -1) out.push(extra[i])
  }
  return out
}

// The modes to offer for the bridge's last line. No `available` key is the
// JBL bridge, whose protocol has the four fixed slots; otherwise exactly
// what the device listed, in drawing order, dropping names this backend
// cannot have.
function modesOffered(state, id) {
  var listed = state ? state.available : undefined
  if (!Array.isArray(listed)) return BASE_MODES.slice()
  var out = []
  var names = modeNames(id)
  for (var i = 0; i < names.length; i++)
    if (listed.indexOf(names[i]) !== -1) out.push(names[i])
  return out
}

// The buttons for those modes, under the names the row gives them.
function modeButtons(available, id) {
  var entry = row(id)
  var labels = entry && entry.modeLabels ? entry.modeLabels : {}
  var out = []
  for (var i = 0; i < MODE_BUTTONS.length; i++) {
    var button = MODE_BUTTONS[i]
    if (available.indexOf(button.value) === -1) continue
    out.push({ value: button.value, key: button.key,
               label: labels[button.value] || button.label, tooltip: button.tooltip })
  }
  return out
}

// ---- ANC strength in named grades (Nothing: Low, Mid, High, Adaptive).
//      A bridge that grades nothing sends no list, and there is no default.

var ANC_LEVELS = ["low", "mid", "high", "adaptive"]

var ANC_LEVEL_BUTTONS = [
  { value: "low", key: "1", label: "Low", tooltip: "Noise Cancelling, low" },
  { value: "mid", key: "2", label: "Mid", tooltip: "Noise Cancelling, medium" },
  { value: "high", key: "3", label: "High", tooltip: "Noise Cancelling, high" },
  { value: "adaptive", key: "4", label: "Adaptive", tooltip: "Noise Cancelling, adaptive" }
]

function ancLevelsOffered(state) {
  var listed = state ? state.ancLevels : undefined
  if (!Array.isArray(listed)) return []
  var out = []
  for (var i = 0; i < ANC_LEVELS.length; i++)
    if (listed.indexOf(ANC_LEVELS[i]) !== -1) out.push(ANC_LEVELS[i])
  return out
}

function ancLevelButtons(levels) {
  var out = []
  for (var i = 0; i < ANC_LEVEL_BUTTONS.length; i++)
    if (levels.indexOf(ANC_LEVEL_BUTTONS[i].value) !== -1) out.push(ANC_LEVEL_BUTTONS[i])
  return out
}

// The grade last reported, or "" for one this panel cannot name.
function ancLevelOf(state) {
  var value = Text.str(state ? state.ancLevel : "")
  return ANC_LEVELS.indexOf(value) !== -1 ? value : ""
}

// ---- The JBL bridge's record of which Fast Pair models answered.
//      $XDG_STATE_HOME/cornerstone-phones/mode-support.json, written by the
//      bridge, read by the service. 1 answered, 0 gave up after repeated
//      silence, -1 never settled.

function parseSupport(raw) {
  return Text.parseObject(raw) || {}
}

function supportVerdict(support, modelId) {
  if (!modelId) return -1
  var entry = support ? support[modelId] : undefined
  if (!entry || typeof entry !== "object") return -1
  if (entry.supported === true) return 1
  if (entry.supported === false) return 0
  return -1
}
