// lib/battery.js: where a headset's levels come from, how they read as text
// and glyphs, and when a low one is worth a notification. The cases here are
// the ones that bite: a bud that reports nothing, a reader line that is not
// JSON, a set with one battery, and a level that changes source.
//
//   deno test --allow-read tests/js/

import { assertEquals } from "jsr:@std/assert@1";
import { load, namesOf } from "./load.js";

const Battery = load("battery.js");

Deno.test("battery.js: every top-level name is reachable", () => {
  const names = namesOf("battery.js");
  for (const name of ["snapshot", "lowStep", "tooltip", "statusLine", "mergeReading", "readingAddress",
                      "LEVEL_GLYPHS", "CHARGING_GLYPHS", "HEADPHONES_GLYPH", "UNKNOWN_GLYPH"]) {
    assertEquals(names.includes(name), true, name);
  }
  assertEquals(Battery.UNKNOWN_GLYPH, "\u{f0091}");
  assertEquals(Battery.HEADPHONES_GLYPH, "\u{f02cb}");
  assertEquals(Battery.LEVEL_GLYPHS.length, 10);
  assertEquals(Battery.CHARGING_GLYPHS.length, 10);
  assertEquals(names.filter((name) => Battery[name] === undefined), []);
});

const buds = (extra = {}) => ({
  name: "JBL TUNE230NC TWS",
  address: "AA:BB:CC:DD:EE:FF",
  paired: true,
  connected: true,
  batteryAvailable: true,
  battery: 0.2,
  ...extra,
});

// ---- Single values.

Deno.test("bluezLevel needs a connection and a reported battery", () => {
  assertEquals(Battery.bluezLevel(buds()), 20);
  assertEquals(Battery.bluezLevel(buds({ connected: false })), -1);
  assertEquals(Battery.bluezLevel(buds({ batteryAvailable: false })), -1);
  assertEquals(Battery.bluezLevel(null), -1);
  // Out-of-range input is clamped rather than trusted.
  assertEquals(Battery.bluezLevel(buds({ battery: 1.4 })), 100);
  assertEquals(Battery.bluezLevel(buds({ battery: -0.2 })), 0);
  assertEquals(Battery.bluezLevel(buds({ battery: 0.555 })), 56);
  // BatteryAvailable without a Percentage yet: unknown, not zero.
  assertEquals(Battery.bluezLevel({ connected: true, batteryAvailable: true }), -1);
});

Deno.test("lowestBud ignores a bud that reports nothing", () => {
  assertEquals(Battery.lowestBud(80, 40), 40);
  assertEquals(Battery.lowestBud(-1, 40), 40);
  assertEquals(Battery.lowestBud(80, -1), 80);
  assertEquals(Battery.lowestBud(-1, -1), -1);
  // The point of the function: a bud in the case must not drag the bar to zero.
  assertEquals(Battery.lowestBud(-1, 90), 90);
  assertEquals(Battery.lowestBud(0, 90), 0);
});

Deno.test("levelGlyph walks the ramp and switches on charging", () => {
  assertEquals(Battery.levelGlyph(-1, false), "");
  assertEquals(Battery.levelGlyph(0, false), "\u{f007a}");
  assertEquals(Battery.levelGlyph(9, false), "\u{f007a}");
  assertEquals(Battery.levelGlyph(10, false), "\u{f007b}");
  assertEquals(Battery.levelGlyph(100, false), "\u{f0079}");
  assertEquals(Battery.levelGlyph(50, true), "\u{f0089}");
  // 100 must not index past the end of the ramp.
  assertEquals(Battery.levelGlyph(100, true), "\u{f0085}");
  // A missing level is not a level: no glyph, and no NaN index into the ramp.
  assertEquals(Battery.levelGlyph(undefined, false), "");
  assertEquals(Battery.levelGlyph(NaN, true), "");
  assertEquals(Battery.levelGlyph("50", false), "");
});

Deno.test("percent treats absent, out-of-range and non-numeric alike", () => {
  // What the old readerLevel did with one key of a reader line.
  assertEquals(Battery.percent(90), 90);
  assertEquals(Battery.percent(0), 0);
  assertEquals(Battery.percent(100), 100);
  assertEquals(Battery.percent(-1), -1);
  assertEquals(Battery.percent(undefined), -1);
  assertEquals(Battery.percent(null), -1);
  assertEquals(Battery.percent("90"), -1);
  assertEquals(Battery.percent(NaN), -1);
  // A reader level above 100 is not a level.
  assertEquals(Battery.percent(101), -1);
  assertEquals(Battery.percent(126), -1);
  assertEquals(Battery.percent(55.4), 55);
});

// ---- Words.

Deno.test("statusLine states the connection and leaves the levels to the rows", () => {
  const base = { hasDevice: true, connected: true, name: "JBL", level: 40 };
  assertEquals(Battery.statusLine({ ...base, left: 55, right: 40 }), "connected");
  assertEquals(Battery.statusLine({ ...base, left: 55, right: 40, charging: true }), "connected · charging");
  assertEquals(Battery.statusLine({ ...base, left: -1, right: -1, level: 35 }), "connected");
  assertEquals(Battery.statusLine({ ...base, single: true, singleLevel: 97, level: 97 }), "connected");
  assertEquals(Battery.statusLine({ ...base, left: -1, right: -1, level: -1 }), "connected · battery not reported");
  assertEquals(Battery.statusLine({ hasDevice: true, connected: false }), "not connected");
  assertEquals(Battery.statusLine({ hasDevice: false }), "no headphones paired");
});

Deno.test("tooltip lists only the components that reported", () => {
  const base = { hasDevice: true, connected: true, name: "JBL" };
  assertEquals(Battery.tooltip({ ...base, left: 90, right: 100, caseLevel: 78, level: 90 }),
    "JBL · L 90% · R 100% · case 78%");
  assertEquals(Battery.tooltip({ ...base, left: 90, right: -1, caseLevel: -1, level: 90 }), "JBL · L 90%");
  assertEquals(Battery.tooltip({ ...base, left: -1, right: -1, caseLevel: -1, level: 20 }), "JBL · 20%");
  assertEquals(Battery.tooltip({ ...base, left: -1, right: -1, caseLevel: -1, level: -1 }), "JBL · battery unknown");
  assertEquals(Battery.tooltip({ hasDevice: true, connected: false, name: "JBL" }), "JBL · not connected");
  assertEquals(Battery.tooltip({ hasDevice: false }), "No headphones paired");
});

Deno.test("tooltip leads with the set level when only the case reported", () => {
  // With both buds silent the bar shows the level, and the tooltip used to
  // name the case alone, so the two disagreed on screen.
  const state = { hasDevice: true, connected: true, name: "JBL", left: -1, right: -1, caseLevel: 78, level: 35 };
  assertEquals(Battery.tooltip(state), "JBL · 35% · case 78%");
  assertEquals(Battery.statusLine(state), "connected");
});

Deno.test("a headset with one battery is never given a side", () => {
  // The WH-CH720N sends a one-byte battery payload: one figure for the whole
  // headset, with no left, no right and no case. It used to arrive as `left`
  // and read "L 100%", which claims a right earbud that does not exist.
  const state = {
    hasDevice: true, connected: true, name: "WH-CH720N",
    left: -1, right: -1, caseLevel: -1, level: 100,
    single: true, singleLevel: 100,
  };
  assertEquals(Battery.singleOf(state), 100);
  assertEquals(Battery.statusLine(state), "connected");
  assertEquals(Battery.tooltip(state), "WH-CH720N · 100%");
  assertEquals(Battery.lowBody({ ...state, level: 15, singleLevel: 15 }), "15% left");
});

Deno.test("singleOf answers only for a set that says it has one battery", () => {
  // A pair of earbuds carries no `single`, and a single headset that has not
  // reported yet carries -1; both mean "no single figure", so callers can ask
  // one question rather than checking a flag and a number.
  assertEquals(Battery.singleOf({ single: true, singleLevel: -1 }), -1);
  assertEquals(Battery.singleOf({ single: false, singleLevel: 90 }), -1);
  assertEquals(Battery.singleOf({ singleLevel: 90 }), -1);
  assertEquals(Battery.singleOf({ single: true, singleLevel: "90" }), -1);
  assertEquals(Battery.singleOf(null), -1);
  // With the flag off, the per-earbud tooltip wording is untouched.
  assertEquals(Battery.tooltip({
    hasDevice: true, connected: true, name: "JBL",
    left: 55, right: 40, level: 40, single: false, singleLevel: -1,
  }), "JBL · L 55% · R 40%");
});

Deno.test("lowBody says which bud is low", () => {
  assertEquals(Battery.lowBody({ left: 15, right: 8, level: 8 }), "L 15% · R 8% left");
  assertEquals(Battery.lowBody({ left: -1, right: 8, level: 8 }), "R 8% left");
  assertEquals(Battery.lowBody({ left: -1, right: -1, level: 20 }), "20% left");
  assertEquals(Battery.lowBody({ left: -1, right: -1, level: -1 }), "battery low");
});

Deno.test("barText writes a percentage only when asked and known", () => {
  assertEquals(Battery.barText(40, true), "\u{f02cb} 40%");
  assertEquals(Battery.barText(40, false), "\u{f02cb}");
  assertEquals(Battery.barText(-1, true), "\u{f02cb}");
  assertEquals(Battery.barText(0, true), "\u{f02cb} 0%");
  assertEquals(Battery.percentLabel(-1), "--");
  assertEquals(Battery.percentLabel(undefined), "--");
  assertEquals(Battery.percentLabel(0), "0%");
  assertEquals(Battery.percentLabel(85), "85%");
});

// ---- The Fast Pair reader's lines.

Deno.test("mergeReading keeps values the newer line omits", () => {
  const first = Battery.mergeReading({}, '{"stream":true,"modelId":"71f20a","left":90}');
  assertEquals(first.modelId, "71f20a");
  // A later battery line carries no modelId; it must survive.
  const second = Battery.mergeReading(first, '{"stream":true,"left":80,"right":70}');
  assertEquals(second.modelId, "71f20a");
  assertEquals(second.left, 80);
  assertEquals(second.right, 70);
  // A new object every time: QML only notices a var that is new.
  assertEquals(second === first, false);
  assertEquals(first.left, 90);
});

Deno.test("mergeReading takes the single-battery line as the reader writes it", () => {
  const state = Battery.mergeReading({},
    '{"stream":true,"single":true,"battery":100,"batteryCharging":false,' +
      '"left":-1,"right":-1,"case":-1,"modelId":"0e4c0a"}');
  assertEquals(state.single, true);
  assertEquals(Battery.percent(state.battery), 100);
  // The three component keys stay unknown: the headset measured one battery.
  assertEquals(Battery.percent(state.left), -1);
  assertEquals(Battery.percent(state.right), -1);
  assertEquals(Battery.percent(state.case), -1);
  const shot = Battery.snapshot({ reading: state, streamLive: true, bluez: 90 });
  assertEquals([shot.single, shot.singleLevel, shot.left, shot.right, shot.caseLevel, shot.level],
    [true, 100, -1, -1, -1, 100]);
});

Deno.test("mergeReading carries the address along with the reading", () => {
  const state = Battery.mergeReading({}, '{"address":"A0:11:22:33:44:55","stream":true,"left":90,"right":80}');
  assertEquals(state.address, "A0:11:22:33:44:55");
  // The service routed by it already, so it is only ever the same address; it
  // survives the error line like the other identifying keys.
  const down = Battery.mergeReading(state,
    '{"address":"A0:11:22:33:44:55","stream":false,"error":"message stream ended"}');
  assertEquals(down.address, "A0:11:22:33:44:55");
  assertEquals(down.left, 90);
  assertEquals(down.stream, false);
});

Deno.test("mergeReading survives junk without losing what it had", () => {
  const state = Battery.mergeReading({ left: 90, stream: true }, "not json at all");
  assertEquals(state.left, 90);
  assertEquals(state.stream, true);
  assertEquals(Battery.mergeReading({ left: 90 }, "").left, 90);
  assertEquals(Battery.mergeReading({ left: 90 }, null).left, 90);
  // A JSON array is valid JSON; merging it would add keys "0" and "1".
  const array = Battery.mergeReading({ left: 90 }, "[7,8]");
  assertEquals(array.left, 90);
  assertEquals(Object.keys(array), ["left"]);
  assertEquals(Battery.mergeReading({ left: 90 }, "42").left, 90);
  assertEquals(Object.keys(Battery.mergeReading({ left: 90 }, "null")), ["left"]);
  assertEquals(Battery.mergeReading(null, '{"left":5}'), { left: 5 });
});

Deno.test("readingAddress routes a line to the device it is about", () => {
  // One reader serves every device, so every line for a device names it.
  assertEquals(Battery.readingAddress('{"address":"A0:11:22:33:44:55","stream":true,"left":90}'), "A0:11:22:33:44:55");
  // Whatever case it arrives in; the service compares it with BlueZ's.
  assertEquals(Battery.readingAddress('{"address":" a0:11:22:33:44:55 ","stream":false,"error":"x"}'), "A0:11:22:33:44:55");
  // No address is the reader talking about itself, which is every device's
  // problem and no device's line.
  assertEquals(Battery.readingAddress('{"stream":false,"error":"no system bus"}'), "");
  assertEquals(Battery.readingAddress('{"stream":true}'), "");
  // Junk is unroutable in exactly the same way.
  for (const raw of ["not json at all", "", null, "[7,8]", "42", "null"]) {
    assertEquals(Battery.readingAddress(raw), "", String(raw));
  }
  // An address that is not a string is not an address.
  assertEquals(Battery.readingAddress('{"address":7,"stream":true}'), "");
});

Deno.test("carriesLevels is true only for a line with a level key", () => {
  assertEquals(Battery.carriesLevels('{"stream":true,"left":90}'), true);
  assertEquals(Battery.carriesLevels('{"stream":true,"right":-1}'), true);
  assertEquals(Battery.carriesLevels('{"stream":true,"single":true,"battery":100}'), true);
  // The stream-down line, a model line and junk are not fresh readings.
  assertEquals(Battery.carriesLevels('{"address":"A0:11:22:33:44:55","stream":false,"error":"x"}'), false);
  assertEquals(Battery.carriesLevels('{"stream":true,"modelId":"71f20a"}'), false);
  for (const raw of ["", null, "junk", "[1]"]) assertEquals(Battery.carriesLevels(raw), false, String(raw));
});

Deno.test("streamHasLevels counts any part, or the one battery of a single set", () => {
  assertEquals(Battery.streamHasLevels({ left: 90 }), true);
  assertEquals(Battery.streamHasLevels({ left: -1, right: -1, case: 40 }), true);
  assertEquals(Battery.streamHasLevels({ left: -1, right: -1, case: -1 }), false);
  assertEquals(Battery.streamHasLevels({}), false);
  assertEquals(Battery.streamHasLevels({ single: true, battery: 60 }), true);
  // A single set's battery is the only key that counts for it.
  assertEquals(Battery.streamHasLevels({ single: true, battery: -1, left: 80 }), false);
});

// ---- The bridge's battery object.

Deno.test("bridge battery reads what the line carried and nothing more", () => {
  const line = { modes: true, battery: { left: 85, right: 15, case: 85, charging: ["case"], caseStale: true } };
  assertEquals(Battery.bridgeLevel(line, "left"), 85);
  assertEquals(Battery.bridgeLevel(line, "right"), 15);
  assertEquals(Battery.bridgeLevel(line, "case"), 85);
  // Not mentioned is not reported.
  assertEquals(Battery.bridgeLevel(line, "headset"), -1);
  assertEquals(Battery.bridgeCharging(line, "case"), true);
  assertEquals(Battery.bridgeCharging(line, "left"), false);
  assertEquals(Battery.snapshot({ bridgeState: line, bridgeLive: true }).caseStale, true);
  // Out-of-range and non-numbers are "not reported", not clamped.
  assertEquals(Battery.bridgeLevel({ battery: { left: 130 } }, "left"), -1);
  assertEquals(Battery.bridgeLevel({ battery: { left: "85" } }, "left"), -1);
  assertEquals(Battery.bridgeLevel({ battery: { left: -1 } }, "left"), -1);
  // A line with no battery, an array, or no line at all.
  assertEquals(Battery.bridgeLevel({ modes: true, mode: "anc" }, "left"), -1);
  assertEquals(Battery.bridgeLevel({ battery: [85] }, "0"), -1);
  assertEquals(Battery.bridgeLevel(null, "left"), -1);
  assertEquals(Battery.bridgeCharging(null, "left"), false);
  assertEquals(Battery.bridgeCharging({ battery: { charging: "left" } }, "left"), false);
  assertEquals(Battery.snapshot({ bridgeState: { battery: { case: 50 } }, bridgeLive: true }).caseStale, false);
  assertEquals(Battery.snapshot({ bridgeState: null, bridgeLive: true }).caseStale, false);
});

// ---- snapshot: one picture from the stream, the bridge and BlueZ.

const liveStream = (extra = {}) => ({ stream: true, left: 80, right: 60, case: 40, ...extra });
const bridgeLine = (battery) => ({ modes: true, battery });

Deno.test("snapshot: a live stream beats the bridge, and the bridge beats BlueZ", () => {
  const reading = liveStream({ leftCharging: true });
  const bridgeState = bridgeLine({ left: 30, right: 20, case: 10 });
  const both = Battery.snapshot({ reading, streamLive: true, bridgeState, bridgeLive: true, bluez: 70 });
  assertEquals(both.source, "stream");
  assertEquals([both.left, both.right, both.caseLevel], [80, 60, 40]);
  assertEquals([both.leftCharging, both.rightCharging, both.caseCharging], [true, false, false]);
  assertEquals(both.perBud, true);
  assertEquals(both.single, false);
  assertEquals(both.level, 60);
  assertEquals(both.charging, true);

  const bridge = Battery.snapshot({ reading, streamLive: false, bridgeState, bridgeLive: true, bluez: 70 });
  assertEquals(bridge.source, "bridge");
  assertEquals([bridge.left, bridge.right, bridge.caseLevel, bridge.level], [30, 20, 10, 20]);

  const bluez = Battery.snapshot({ reading, streamLive: false, bridgeState, bridgeLive: false, bluez: 70 });
  assertEquals(bluez.source, "bluez");
  assertEquals([bluez.left, bluez.right, bluez.caseLevel, bluez.perBud, bluez.level], [-1, -1, -1, false, 70]);

  const nothing = Battery.snapshot({});
  assertEquals(nothing.source, "");
  assertEquals(nothing.level, -1);
  assertEquals(nothing.charging, false);
  assertEquals(Battery.snapshot({ bluez: -1 }).source, "");
});

Deno.test("snapshot: a dead reader makes the stream not count", () => {
  // The reading keeps stream:true from its last line, but the reader is gone.
  const reading = liveStream();
  const bridgeState = bridgeLine({ left: 30, right: 20 });
  assertEquals(Battery.snapshot({ reading, streamLive: false, bridgeState, bridgeLive: true }).source, "bridge");
  assertEquals(Battery.snapshot({ reading, streamLive: false, bluez: 50 }).source, "bluez");
  assertEquals(Battery.snapshot({ reading, bluez: 50 }).source, "bluez");
  // And the reader saying the stream is down is the same.
  const down = liveStream({ stream: false });
  assertEquals(Battery.snapshot({ reading: down, streamLive: true, bridgeState, bridgeLive: true }).source, "bridge");
});

Deno.test("snapshot: a bridge with no figure, or not live, is not a source", () => {
  const empty = Battery.snapshot({ bridgeState: { modes: true, mode: "anc" }, bridgeLive: true, bluez: 40 });
  assertEquals(empty.source, "bluez");
  assertEquals(empty.level, 40);
  const silent = Battery.snapshot({ bridgeState: bridgeLine({ left: -1, right: -1 }), bridgeLive: true, bluez: 40 });
  assertEquals(silent.source, "bluez");
  // A case alone is a figure; the bar then carries BlueZ's.
  const caseOnly = Battery.snapshot({ bridgeState: bridgeLine({ case: 70 }), bridgeLive: true, bluez: 40 });
  assertEquals(caseOnly.source, "bridge");
  assertEquals([caseOnly.caseLevel, caseOnly.perBud, caseOnly.level], [70, false, 40]);
  const stale = Battery.snapshot({ bridgeState: bridgeLine({ left: 50 }), bridgeLive: false, bluez: 40 });
  assertEquals(stale.source, "bluez");
});

Deno.test("snapshot: a single-battery headset has one figure and no sides", () => {
  const stream = Battery.snapshot({
    reading: { stream: true, single: true, battery: 64, batteryCharging: true, left: -1, right: -1, case: -1 },
    streamLive: true, bluez: 60,
  });
  assertEquals(stream.source, "stream");
  assertEquals([stream.single, stream.singleLevel, stream.singleCharging], [true, 64, true]);
  assertEquals([stream.left, stream.right, stream.caseLevel, stream.perBud], [-1, -1, -1, false]);
  assertEquals(stream.level, 64);
  assertEquals(stream.charging, true);

  const bridge = Battery.snapshot({
    bridgeState: bridgeLine({ headset: 45, charging: ["headset"] }), bridgeLive: true, bluez: 40,
  });
  assertEquals(bridge.source, "bridge");
  assertEquals([bridge.single, bridge.singleLevel, bridge.singleCharging, bridge.level], [true, 45, true, 45]);
  assertEquals([bridge.left, bridge.right, bridge.perBud], [-1, -1, false]);

  // A headset figure wins over parts the same line also carried.
  const mixed = Battery.snapshot({ bridgeState: bridgeLine({ headset: 45, left: 10 }), bridgeLive: true });
  assertEquals([mixed.single, mixed.left, mixed.level], [true, -1, 45]);

  // One battery that has not reported yet: BlueZ's figure carries the bar.
  const pending = Battery.snapshot({ reading: { stream: true, single: true, battery: -1 }, streamLive: true, bluez: 60 });
  assertEquals([pending.source, pending.single, pending.singleLevel, pending.level], ["stream", true, -1, 60]);
  // Above 100 is not a level either.
  const junk = Battery.snapshot({ reading: { stream: true, single: true, battery: 126 }, streamLive: true, bluez: 60 });
  assertEquals([junk.singleLevel, junk.level], [-1, 60]);
});

Deno.test("snapshot: a Redmi-style stream with no levels gives way to the bridge", () => {
  // The Buds 5 open the Fast Pair stream and never send a battery over it.
  const reading = { stream: true, modelId: "abc123", left: -1, right: -1, case: -1 };
  const bridgeState = bridgeLine({ left: 70, right: 65, case: 90, caseStale: true });
  const redmi = Battery.snapshot({
    reading, streamLive: true, streamNeedsLevels: true, bridgeState, bridgeLive: true, bluez: 60,
  });
  assertEquals(redmi.source, "bridge");
  assertEquals([redmi.left, redmi.right, redmi.caseLevel, redmi.caseStale, redmi.level], [70, 65, 90, true, 65]);
  // With nothing behind it, BlueZ.
  const alone = Battery.snapshot({ reading, streamLive: true, streamNeedsLevels: true, bluez: 60 });
  assertEquals([alone.source, alone.level], ["bluez", 60]);
  // Once that stream does carry a level, it is the stream again.
  const levelled = Battery.snapshot({
    reading: { ...reading, left: 50 }, streamLive: true, streamNeedsLevels: true, bridgeState, bridgeLive: true,
  });
  assertEquals([levelled.source, levelled.left, levelled.level], ["stream", 50, 50]);
  // Without the flag an empty stream still stands in front of the bridge,
  // and the bar falls back to BlueZ's figure.
  const other = Battery.snapshot({ reading, streamLive: true, bridgeState, bridgeLive: true, bluez: 60 });
  assertEquals([other.source, other.left, other.level], ["stream", -1, 60]);
});

Deno.test("snapshot: caseStale only ever comes from the bridge", () => {
  const stream = Battery.snapshot({ reading: liveStream({ caseStale: true }), streamLive: true });
  assertEquals(stream.caseStale, false);
  const bridge = Battery.snapshot({ bridgeState: bridgeLine({ left: 50, case: 30, caseStale: true }), bridgeLive: true });
  assertEquals(bridge.caseStale, true);
  // Only a literal true counts.
  const truthy = Battery.snapshot({ bridgeState: bridgeLine({ left: 50, caseStale: 1 }), bridgeLive: true });
  assertEquals(truthy.caseStale, false);
  // The stream winning hides the bridge's flag along with its levels.
  const hidden = Battery.snapshot({
    reading: liveStream(), streamLive: true,
    bridgeState: bridgeLine({ left: 50, caseStale: true }), bridgeLive: true,
  });
  assertEquals(hidden.caseStale, false);
  assertEquals(Battery.snapshot({ bluez: 50 }).caseStale, false);
});

Deno.test("snapshot: level is the single figure, else the lowest bud, else BlueZ", () => {
  const level = (input) => Battery.snapshot(input).level;
  assertEquals(level({ reading: { stream: true, single: true, battery: 90 }, streamLive: true, bluez: 10 }), 90);
  assertEquals(level({ reading: { stream: true, left: 80, right: 60 }, streamLive: true, bluez: 10 }), 60);
  // A bud in the case reports nothing and does not drag the figure to zero.
  assertEquals(level({ reading: { stream: true, left: -1, right: 60 }, streamLive: true, bluez: 10 }), 60);
  assertEquals(level({ reading: { stream: true, left: 0, right: 60 }, streamLive: true, bluez: 10 }), 0);
  assertEquals(level({ reading: { stream: true, left: -1, right: -1, case: 50 }, streamLive: true, bluez: 10 }), 10);
  assertEquals(level({ bluez: 0 }), 0);
  assertEquals(level({ bluez: -1 }), -1);
  assertEquals(level({ bluez: undefined }), -1);
});

Deno.test("snapshot: charging is a bud or the headset, not the case", () => {
  const charging = (reading) => Battery.snapshot({ reading: { stream: true, ...reading }, streamLive: true }).charging;
  assertEquals(charging({ left: 50, right: 50, caseCharging: true }), false);
  assertEquals(charging({ left: 50, right: 50, rightCharging: true }), true);
  // Only a literal true counts.
  assertEquals(charging({ left: 50, leftCharging: "yes" }), false);
  const bridge = Battery.snapshot({ bridgeState: bridgeLine({ left: 50, case: 50, charging: ["case"] }), bridgeLive: true });
  assertEquals([bridge.caseCharging, bridge.charging], [true, false]);
});

// ---- lowStep: when a low battery is worth a notification.

const THRESHOLD = 20;

// Runs a sequence of steps through lowStep, carrying memory and the notified
// flag the way Headset.qml does, and returns which steps warned.
function drain(steps, start = {}) {
  let memory = start.memory || {};
  let notified = start.notified === true;
  const warned = [];
  for (const step of steps) {
    const result = Battery.lowStep(memory, {
      enabled: true, connected: true, source: "bud", charging: false, threshold: THRESHOLD,
      ...step, notified,
    });
    memory = result.memory;
    notified = result.notified;
    warned.push(result.warn);
  }
  return { warned, memory, notified };
}

const primed = { source: "", level: -1, primed: true };

Deno.test("lowStep: the first level of a session is a baseline, not news", () => {
  // Earbuds already low when the shell starts do not warn.
  const first = Battery.lowStep({}, {
    enabled: true, connected: true, level: 10, source: "bud", charging: false, threshold: THRESHOLD, notified: false,
  });
  assertEquals(first.warn, false);
  assertEquals(first.notified, false);
  assertEquals(first.memory, { source: "bud", level: 10, primed: true });
  // The next drop after that baseline does.
  assertEquals(drain([{ level: 10 }, { level: 9 }]).warned, [false, true]);
  // A flat or rising level after the baseline does not.
  assertEquals(drain([{ level: 10 }, { level: 10 }, { level: 11 }]).warned, [false, false, false]);
});

Deno.test("lowStep: a drop to or below the threshold warns", () => {
  assertEquals(drain([{ level: 30 }, { level: 25 }, { level: 20 }]).warned, [false, false, true]);
  assertEquals(drain([{ level: 30 }, { level: 15 }]).warned, [false, true]);
  // Above it, nothing, however fast it falls.
  assertEquals(drain([{ level: 90 }, { level: 21 }]).warned, [false, false]);
});

Deno.test("lowStep: one warning per drain", () => {
  const run = drain([{ level: 30 }, { level: 20 }, { level: 18 }, { level: 10 }, { level: 2 }]);
  assertEquals(run.warned, [false, true, false, false, false]);
  assertEquals(run.notified, true);
});

Deno.test("lowStep: the warning re-arms only at threshold + 5, or on charging", () => {
  // Wobbling across the line is the same drain.
  assertEquals(drain([{ level: 30 }, { level: 20 }, { level: 24 }, { level: 19 }]).warned,
    [false, true, false, false]);
  // Back to threshold + 5 clears it, and the next drop warns again.
  const climbed = drain([{ level: 30 }, { level: 20 }, { level: 25 }, { level: 20 }]);
  assertEquals(climbed.warned, [false, true, false, true]);
  // Charging clears it at any level, and never warns itself.
  const charged = drain([{ level: 30 }, { level: 20 }, { level: 15, charging: true }, { level: 15 }, { level: 14 }]);
  assertEquals(charged.warned, [false, true, false, false, true]);
  const plugged = Battery.lowStep({ source: "bud", level: 20, primed: true }, {
    enabled: true, connected: true, level: 12, source: "bud", charging: true, threshold: THRESHOLD, notified: true,
  });
  assertEquals([plugged.warn, plugged.notified], [false, false]);
});

Deno.test("lowStep: a disconnect clears notified and history but keeps primed", () => {
  const out = Battery.lowStep({ source: "bud", level: 12, primed: true }, {
    enabled: true, connected: false, level: 12, source: "bud", charging: false, threshold: THRESHOLD, notified: true,
  });
  assertEquals(out.warn, false);
  assertEquals(out.notified, false);
  assertEquals(out.memory, { source: "", level: -1, primed: true });
  // Never primed stays never primed.
  const fresh = Battery.lowStep({}, { enabled: true, connected: false, threshold: THRESHOLD, notified: false });
  assertEquals(fresh.memory, { source: "", level: -1, primed: false });
});

Deno.test("lowStep: a witnessed connect is primed by the caller, so its first level counts", () => {
  // Headset.qml sets primed itself when it saw the set disconnected and then
  // connect: that first level is news.
  assertEquals(drain([{ level: 12 }], { memory: primed }).warned, [true]);
  assertEquals(drain([{ level: 40 }], { memory: primed }).warned, [false]);
  // After a disconnect the memory is primed, so a reconnect at a low level warns.
  const before = drain([{ level: 30 }, { level: 25 }]);
  const gone = Battery.lowStep(before.memory, {
    enabled: true, connected: false, threshold: THRESHOLD, notified: before.notified,
  });
  assertEquals(drain([{ level: 12 }], { memory: gone.memory, notified: gone.notified }).warned, [true]);
});

Deno.test("lowStep: a source change is not a drop", () => {
  // Mid-session: BlueZ's rounded figure, then the stream's lowest bud. The
  // two are different measurements, so the change is not a drop.
  assertEquals(drain([{ level: 30, source: "bluez" }, { level: 30, source: "bluez" }, { level: 15, source: "bud" }])
    .warned, [false, false, false]);
  // Within the new source, the next drop is one.
  assertEquals(drain([{ level: 30, source: "bluez" }, { level: 15, source: "bud" }, { level: 14, source: "bud" }])
    .warned[2], true);
});

Deno.test("lowStep: a restart with low earbuds stays quiet when the stream follows BlueZ", () => {
  // At shell start BlueZ answers first (baseline), then the reader's stream
  // arrives with the buds' own figure. The earbuds were already low before
  // the restart, which the comment above lowStep says is not news.
  assertEquals(drain([{ level: 10, source: "bluez" }, { level: 12, source: "bud" }]).warned, [false, false]);
});

Deno.test("lowStep: off, or no level, changes nothing", () => {
  const memory = { source: "bud", level: 30, primed: true };
  const off = Battery.lowStep(memory, {
    enabled: false, connected: true, level: 5, source: "bud", charging: false, threshold: THRESHOLD, notified: true,
  });
  assertEquals([off.warn, off.notified], [false, true]);
  assertEquals(off.memory, memory);
  // A reader restarting drops the level to -1 for a second; the warning state
  // survives the gap rather than re-arming.
  const gap = Battery.lowStep(memory, {
    enabled: true, connected: true, level: -1, source: "bud", charging: false, threshold: THRESHOLD, notified: true,
  });
  assertEquals([gap.warn, gap.notified], [false, true]);
  assertEquals(gap.memory, memory);
  // A memory written badly is read as an empty one.
  const odd = Battery.lowStep({ level: "30", primed: "yes" }, {
    enabled: true, connected: true, level: 5, source: "bud", charging: false, threshold: THRESHOLD, notified: false,
  });
  assertEquals([odd.warn, odd.memory.primed], [false, true]);
});
