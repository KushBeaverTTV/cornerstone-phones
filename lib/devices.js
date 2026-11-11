.pragma library
.import "text.js" as Text

// Which Bluetooth devices the widget follows, in what order, and how a
// headset is found again by address or by a word typed at the IPC.

// The name to show: the alias a person set in the Bluetooth panel first (so
// renaming the buds there renames them here), then the name the device
// reports, then its address.
function label(device) {
  if (!device) return ""
  return Text.plainText(device.name).trim()
    || Text.plainText(device.deviceName).trim()
    || Text.str(device.address)
}

var AUDIO_WORDS = ["buds", "headphone", "headset", "airpod"]

// BlueZ sets Icon from the device class: earbuds and headsets land on
// audio-headset or audio-headphones. The words catch the sets that arrive
// with a bare audio-card icon or none at all.
function isAudio(device) {
  if (!device) return false
  if (Text.str(device.icon).toLowerCase().indexOf("audio") !== -1) return true
  var name = label(device).toLowerCase()
  for (var i = 0; i < AUDIO_WORDS.length; i++)
    if (name.indexOf(AUDIO_WORDS[i]) !== -1) return true
  return false
}

// The deviceMatch setting: empty follows every audio device; otherwise a
// substring of the name or the address, whatever kind of device it is.
function matches(device, filter) {
  var needle = Text.str(filter).trim().toLowerCase()
  if (needle === "") return isAudio(device)
  return (label(device) + " " + Text.str(device.address)).toLowerCase().indexOf(needle) !== -1
}

function sameAddress(a, b) {
  var left = Text.str(a).trim().toUpperCase()
  return left !== "" && left === Text.str(b).trim().toUpperCase()
}

// Order by points (higher first), then by name, then by position, so the
// sort is total and icons do not swap places between two readings.
function ordered(rows) {
  rows.sort(function (a, b) {
    if (a.points !== b.points) return b.points - a.points
    if (a.name !== b.name) return a.name < b.name ? -1 : 1
    return a.index - b.index
  })
  var out = []
  for (var i = 0; i < rows.length; i++) out.push(rows[i].item)
  return out
}

// Every candidate, best first: connected beats everything, then a device
// that reports a battery, then being audio at all. The set used last only
// breaks ties. A paired device that is not connected still counts, so the
// panel can name the earbuds it is waiting for.
function rank(devices, filter, preferAddress) {
  var list = devices || []
  var rows = []
  for (var i = 0; i < list.length; i++) {
    var device = list[i]
    if (!device || !(device.paired || device.connected) || !matches(device, filter)) continue
    var points = (device.connected ? 8 : 0) + (device.batteryAvailable ? 4 : 0)
      + (isAudio(device) ? 2 : 0) + (sameAddress(device.address, preferAddress) ? 1 : 0)
    rows.push({ item: device, points: points, name: label(device).toLowerCase(), index: i })
  }
  return ordered(rows)
}

// The addresses to follow: every connected match (two headsets on one desk
// are two icons, not a contest), or, with none connected, the single best
// candidate so the widget can still say what it is waiting for.
function followAddresses(devices, filter, preferAddress) {
  var ranked = rank(devices, filter, preferAddress)
  var out = []
  for (var i = 0; i < ranked.length; i++)
    if (ranked[i].connected) out.push(Text.str(ranked[i].address))
  if (out.length === 0 && ranked.length > 0) out.push(Text.str(ranked[0].address))
  return out
}

// The entry with this address, case-insensitively. A machine with two
// adapters lists a device once per adapter; the connected entry wins,
// whichever order BlueZ listed them in.
function byAddress(list, address) {
  var found = null
  var items = list || []
  for (var i = 0; i < items.length; i++) {
    if (!items[i] || !sameAddress(items[i].address, address)) continue
    if (items[i].connected === true) return items[i]
    if (found === null) found = items[i]
  }
  return found
}

// The headsets in drawing order: connected first, then the set used last,
// then by name, then by the order they were made.
function orderHeadsets(headsets, preferAddress) {
  var list = headsets || []
  var rows = []
  for (var i = 0; i < list.length; i++) {
    var headset = list[i]
    if (!headset) continue
    var points = (headset.connected ? 2 : 0) + (sameAddress(headset.address, preferAddress) ? 1 : 0)
    rows.push({ item: headset, points: points, name: Text.str(headset.name).toLowerCase(), index: i })
  }
  return ordered(rows)
}

// Which headset an IPC word means, as an index; -1 for none. A substring of
// the name, the address or the backend ("sony" finds a WH-CH720N, whose name
// does not say Sony). No word is the first headset.
function find(headsets, which) {
  var list = headsets || []
  var needle = Text.str(which).trim().toLowerCase()
  if (needle === "") return list.length > 0 ? 0 : -1
  for (var i = 0; i < list.length; i++) {
    var h = list[i]
    if (!h) continue
    var haystack = [Text.str(h.name), Text.str(h.address), Text.str(h.backend)].join(" ").toLowerCase()
    if (haystack.indexOf(needle) !== -1) return i
  }
  return -1
}

// The addresses the Fast Pair reader should hold a channel for: connected
// headsets only. Asking BlueZ for a channel to a set in its case would
// connect the earbuds behind their owner's back.
function readerAddresses(headsets) {
  var list = headsets || []
  var out = []
  for (var i = 0; i < list.length; i++) {
    if (!list[i] || !list[i].connected) continue
    var address = Text.str(list[i].address).trim().toUpperCase()
    if (address !== "" && out.indexOf(address) === -1) out.push(address)
  }
  return out
}

// The service UUIDs in `bluetoothctl info <address>` output, lowercased.
// Quickshell.Bluetooth has no uuids property, so the record is read the way a
// person reads it:   UUID: Vendor specific   (956c7b26-d49a-...)
var UUID_LINE = /UUID:.*\(([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\)/

function uuidsFromInfo(text) {
  var out = []
  var lines = Text.str(text).split("\n")
  for (var i = 0; i < lines.length; i++) {
    var found = lines[i].match(UUID_LINE)
    if (found) out.push(found[1].toLowerCase())
  }
  return out
}
