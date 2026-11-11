.pragma library
.import "text.js" as Text

// Batteries: where a headset's levels come from, how they read as text and
// glyphs, and when a low one is worth a notification.
//
// Three sources, in order of what they can say:
//   stream  the Google Fast Pair Message Stream (cornerstone-fastpair):
//           left, right and case, or one battery for a headset, pushed by
//           the device when a level changes
//   bridge  the brand bridge's `battery` object, on devices whose control
//           channel carries one; used while there is no live stream
//   bluez   org.bluez.Battery1: one figure for the set, in steps of ten

// Nerd Font battery ramps, 0-9 for each tenth, plain and charging.
var LEVEL_GLYPHS = [
  "\u{f007a}", "\u{f007b}", "\u{f007c}", "\u{f007d}", "\u{f007e}",
  "\u{f007f}", "\u{f0080}", "\u{f0081}", "\u{f0082}", "\u{f0079}"
]
var CHARGING_GLYPHS = [
  "\u{f089c}", "\u{f0086}", "\u{f0087}", "\u{f0088}", "\u{f089d}",
  "\u{f0089}", "\u{f089e}", "\u{f008a}", "\u{f008b}", "\u{f0085}"
]
var HEADPHONES_GLYPH = "\u{f02cb}"
// A battery with a question mark: a part that is not reporting is unknown,
// not empty and not urgent.
var UNKNOWN_GLYPH = "\u{f0091}"

function known(level) {
  return typeof level === "number" && isFinite(level) && level >= 0
}

function levelGlyph(level, charging) {
  if (!known(level)) return ""
  var step = Math.max(0, Math.min(9, Math.floor(level / 10)))
  return (charging ? CHARGING_GLYPHS : LEVEL_GLYPHS)[step]
}

// BlueZ's figure as 0-100, or -1 when there is none to show.
function bluezLevel(device) {
  if (!device || !device.connected || !device.batteryAvailable) return -1
  var level = Math.round(Number(device.battery) * 100)
  return isFinite(level) ? Math.max(0, Math.min(100, level)) : -1
}

function percent(value) {
  return typeof value === "number" && isFinite(value) && value >= 0 && value <= 100
    ? Math.round(value) : -1
}

// ---- The Fast Pair stream.

// One reader line merged over the previous reading. Keys a line leaves out
// keep their value: the reader resends everything on each change, and only
// its short "stream down" line ({address, stream, error}) lacks the model
// id and firmware, which must survive it.
function mergeReading(previous, raw) {
  var next = {}
  for (var key in previous) next[key] = previous[key]
  var parsed = Text.parseObject(raw)
  if (parsed) for (var incoming in parsed) next[incoming] = parsed[incoming]
  return next
}

// Which device a reader line is about, upper-cased; "" for a line about
// the reader itself (or junk), which is every device's business.
function readingAddress(raw) {
  var parsed = Text.parseObject(raw)
  return parsed && typeof parsed.address === "string" ? parsed.address.trim().toUpperCase() : ""
}

// Whether a line carried a level at all: only such a line is a fresh reading.
function carriesLevels(raw) {
  var parsed = Text.parseObject(raw)
  return !!parsed && (parsed.left !== undefined || parsed.right !== undefined
                      || parsed.battery !== undefined)
}

function streamHasLevels(reading) {
  if (reading.single === true) return percent(reading.battery) >= 0
  return percent(reading.left) >= 0 || percent(reading.right) >= 0 || percent(reading.case) >= 0
}

// ---- A bridge's battery object: {"left", "right", "case", "headset",
//      "charging": [parts], "caseStale": bool}.

function bridgeLevel(state, part) {
  var battery = state ? state.battery : undefined
  if (!battery || typeof battery !== "object" || Array.isArray(battery)) return -1
  return percent(battery[part])
}

function bridgeCharging(state, part) {
  var battery = state ? state.battery : undefined
  return !!battery && Array.isArray(battery.charging) && battery.charging.indexOf(part) !== -1
}

// ---- One picture of a headset's battery from all three sources.
//
//   input.reading       the merged reader lines for this device
//   input.streamLive    the reader is running (or refreshing this device)
//   input.streamNeedsLevels  the backend's stream never carries a battery,
//                       so an empty stream does not count as one
//   input.bridgeState   the bridge's last line
//   input.bridgeLive    the bridge is running and has answered
//   input.bluez         BlueZ's figure, -1 for none
//
// Returns { source, single, singleLevel, left, right, caseLevel, caseStale,
// leftCharging, rightCharging, caseCharging, singleCharging, perBud, level,
// charging }. Every level is 0-100 or -1. `level` is the one the bar
// carries: a headset's own figure, else the bud that will die first, else
// BlueZ's.
function snapshot(input) {
  var reading = input.reading || {}
  var state = input.bridgeState || {}
  var out = {
    source: "", single: false, singleLevel: -1, left: -1, right: -1, caseLevel: -1,
    caseStale: false, leftCharging: false, rightCharging: false, caseCharging: false,
    singleCharging: false, perBud: false, level: -1, charging: false
  }
  var stream = reading.stream === true && input.streamLive === true
    && !(input.streamNeedsLevels === true && !streamHasLevels(reading))
  var bridge = !stream && input.bridgeLive === true
    && (bridgeLevel(state, "left") >= 0 || bridgeLevel(state, "right") >= 0
        || bridgeLevel(state, "case") >= 0 || bridgeLevel(state, "headset") >= 0)
  if (stream) {
    out.source = "stream"
    if (reading.single === true) {
      out.single = true
      out.singleLevel = percent(reading.battery)
      out.singleCharging = reading.batteryCharging === true
    } else {
      out.left = percent(reading.left)
      out.right = percent(reading.right)
      out.caseLevel = percent(reading.case)
      out.leftCharging = reading.leftCharging === true
      out.rightCharging = reading.rightCharging === true
      out.caseCharging = reading.caseCharging === true
    }
  } else if (bridge) {
    out.source = "bridge"
    if (bridgeLevel(state, "headset") >= 0) {
      out.single = true
      out.singleLevel = bridgeLevel(state, "headset")
      out.singleCharging = bridgeCharging(state, "headset")
    } else {
      out.left = bridgeLevel(state, "left")
      out.right = bridgeLevel(state, "right")
      out.caseLevel = bridgeLevel(state, "case")
      out.leftCharging = bridgeCharging(state, "left")
      out.rightCharging = bridgeCharging(state, "right")
      out.caseCharging = bridgeCharging(state, "case")
      out.caseStale = !!state.battery && state.battery.caseStale === true
    }
  } else if (known(input.bluez)) {
    out.source = "bluez"
  }
  out.perBud = out.left >= 0 || out.right >= 0
  if (out.singleLevel >= 0) out.level = out.singleLevel
  else if (out.perBud) out.level = lowestBud(out.left, out.right)
  else out.level = known(input.bluez) ? input.bluez : -1
  out.charging = out.leftCharging || out.rightCharging || out.singleCharging
  return out
}

// The earbud that will die first; one resting in the case reports nothing
// and must not drag the figure to zero.
function lowestBud(left, right) {
  if (left >= 0 && right >= 0) return Math.min(left, right)
  return left >= 0 ? left : right
}

// ---- Words.  `s` below is a headset summary:
//      { hasDevice, connected, name, level, left, right, caseLevel, single, singleLevel }

function percentLabel(level) {
  return known(level) ? level + "%" : "--"
}

function barText(level, showPercentage) {
  return known(level) && showPercentage ? HEADPHONES_GLYPH + " " + level + "%" : HEADPHONES_GLYPH
}

function singleOf(s) {
  return s && s.single === true && known(s.singleLevel) ? s.singleLevel : -1
}

function statusLine(s) {
  if (!s.hasDevice) return "no headphones paired"
  if (!s.connected) return "not connected"
  var reported = singleOf(s) >= 0 || s.left >= 0 || s.right >= 0 || s.level >= 0
  if (!reported) return "connected · battery not reported"
  return s.charging ? "connected · charging" : "connected"
}

function tooltip(s) {
  if (!s.hasDevice) return "No headphones paired"
  if (!s.connected) return s.name + " · not connected"
  if (singleOf(s) >= 0) return s.name + " · " + singleOf(s) + "%"
  var parts = []
  // With no bud reporting, the set's figure is what the bar shows, so it
  // leads here too.
  if (s.left < 0 && s.right < 0 && s.level >= 0) parts.push(s.level + "%")
  if (s.left >= 0) parts.push("L " + s.left + "%")
  if (s.right >= 0) parts.push("R " + s.right + "%")
  if (s.caseLevel >= 0) parts.push("case " + s.caseLevel + "%")
  return s.name + " · " + (parts.length > 0 ? parts.join(" · ") : "battery unknown")
}

function lowBody(s) {
  if (singleOf(s) >= 0) return singleOf(s) + "% left"
  var parts = []
  if (s.left >= 0) parts.push("L " + s.left + "%")
  if (s.right >= 0) parts.push("R " + s.right + "%")
  if (parts.length > 0) return parts.join(" · ") + " left"
  return s.level >= 0 ? s.level + "% left" : "battery low"
}

// ---- When to warn.
//
// memory  { source, level, primed }   kept per headset by the caller
// input   { enabled, connected, level, source, charging, threshold, notified }
// returns { memory, notified, warn }
//
// One warning per drain. The flag clears once the level is 5 points above
// the threshold, or charging, so a level wobbling across the line does not
// warn twice. A level from a different source than the last one is compared
// with nothing: Fast Pair's lowest bud and BlueZ's rounded figure are
// different measurements, and one after the other is not a drop. The first
// level the shell sees is a baseline (primed): earbuds that were already low
// before a restart are not news. A connect the caller witnessed sets primed
// itself, so the first level after it does count.
function lowStep(memory, input) {
  var mem = { source: memory.source || "", level: typeof memory.level === "number" ? memory.level : -1,
              primed: memory.primed === true }
  var result = { memory: mem, notified: input.notified === true, warn: false }
  if (!input.enabled) return result
  if (!input.connected) {
    result.memory = { source: "", level: -1, primed: mem.primed }
    result.notified = false
    return result
  }
  if (!known(input.level)) return result
  var previous = input.source === mem.source ? mem.level : -1
  mem.source = input.source
  mem.level = input.level
  if (input.charging || input.level >= input.threshold + 5) result.notified = false
  if (input.charging) return result
  if (!mem.primed) {
    mem.primed = true
    return result
  }
  if (input.level > input.threshold || result.notified) return result
  if (previous >= 0 && input.level >= previous) return result
  result.notified = true
  result.warn = true
  return result
}
