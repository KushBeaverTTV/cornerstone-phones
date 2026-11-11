.pragma library
// Small, pure helpers for values that arrive as strings, and for text that a
// device wrote and the panel shows. No QML here, so tests/js runs it as is.

function str(value) {
  return value === undefined || value === null ? "" : String(value)
}

// `omarchy bar set <id> <key> <value>` stores a JSON string unless told
// otherwise, so a setting can arrive as "false" or "20". In QML a non-empty
// string is truthy, which would flip a boolean setting; read it instead.
function asBool(value, fallback) {
  if (typeof value === "boolean") return value
  if (typeof value === "number") return value !== 0
  var word = str(value).trim().toLowerCase()
  if (word === "true" || word === "1" || word === "yes" || word === "on") return true
  if (word === "false" || word === "0" || word === "no" || word === "off") return false
  return fallback
}

// A whole number from a setting. Blank is the fallback, not zero: Number(" ")
// is 0, which for the threshold would mean "never warn".
function asInt(value, fallback) {
  if (typeof value === "number") return isFinite(value) ? Math.round(value) : fallback
  var text = str(value).trim()
  if (text === "") return fallback
  var number = Number(text)
  return isFinite(number) ? Math.round(number) : fallback
}

// A device names itself, and Qt's default text format renders anything that
// looks like a tag. Every device-authored string goes through here before it
// reaches a Text, a tooltip or a notification: the angle brackets become
// lookalikes, so the string can never be read as markup.
function plainText(value) {
  return str(value).replace(/</g, "‹").replace(/>/g, "›")
}

// A whole number between low and high. Anything that is not a number lands
// on low: a dial with nothing to stand on is at its bottom.
function clamp(value, low, high) {
  var number = Math.round(Number(value))
  if (!isFinite(number)) return low
  return Math.min(high, Math.max(low, number))
}

// One line of JSON that must be an object; anything else is null. Arrays are
// refused too: their indices would merge in as keys "0", "1".
function parseObject(raw) {
  var parsed
  try {
    parsed = JSON.parse(str(raw).trim() || "null")
  } catch (e) {
    return null
  }
  return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null
}

// What a helper that died without explaining itself left on stderr, cut to
// one line for the panel: the last non-empty one, which for a Python
// traceback is the exception itself.
function shortError(text, fallback) {
  var lines = str(text).split("\n")
  for (var i = lines.length - 1; i >= 0; i--) {
    var line = lines[i].trim()
    if (line === "") continue
    line = plainText(line)
    return line.length > 160 ? line.substring(0, 157) + "..." : line
  }
  return fallback
}

// A shallow copy of an object with one key set (or removed, for undefined).
// QML only notices a var property change when the object itself is new.
function withKey(object, key, value) {
  var next = {}
  for (var name in object) next[name] = object[name]
  if (value === undefined) delete next[key]
  else next[key] = value
  return next
}
