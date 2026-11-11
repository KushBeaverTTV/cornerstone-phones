// lib/text.js: values that arrive as strings, and text a device wrote.
//
//   deno test --allow-read tests/js/

import { assertEquals, assertThrows } from "jsr:@std/assert@1";
import { load, namesOf, topLevelNames, translate } from "./load.js";

const Text = load("text.js");

Deno.test("text.js: every top-level name is reachable", () => {
  const names = namesOf("text.js");
  for (const name of ["str", "asBool", "asInt", "plainText", "clamp", "parseObject", "shortError", "withKey"]) {
    assertEquals(names.includes(name), true, name);
  }
  assertEquals(names.filter((name) => Text[name] === undefined), []);
});

Deno.test("the loader strips QML directives, injects imports and shares one copy per file", () => {
  const { body, imports } = translate('.pragma library\n.import "text.js" as Text\nvar X = 1\n');
  assertEquals(imports, [{ file: "text.js", as: "Text" }]);
  // Blanked, not removed, so line numbers in an error still match the file.
  assertEquals(body, "\n\nvar X = 1\n");
  assertThrows(() => translate('.import QtQuick 2.0 as Q\n'), Error, "does not know");
  assertEquals(topLevelNames("function a() {}\nvar B = 2\n  function nested() {}\n"), ["a", "B"]);
  assertEquals(load("text.js") === Text, true);
  // devices.js calls Text.plainText through the injected import.
  assertEquals(load("devices.js").label({ name: "<b>" }), "‹b›");
});

Deno.test("shortError keeps the last line a helper said, trimmed", () => {
  assertEquals(
    Text.shortError("Traceback:\n  File x\nModuleNotFoundError: no dbus\n", "fell over"),
    "ModuleNotFoundError: no dbus",
  );
  // Nothing said at all is what the fallback is for.
  assertEquals(Text.shortError("", "fell over"), "fell over");
  assertEquals(Text.shortError("   \n\n", "fell over"), "fell over");
  assertEquals(Text.shortError(null, "fell over"), "fell over");
  // A panel is not a log: a wall of text is cut and marked as cut.
  const long = Text.shortError("x".repeat(400), "fell over");
  assertEquals(long.length, 160);
  assertEquals(long.slice(-3), "...");
  assertEquals(Text.shortError("x".repeat(160), "fell over"), "x".repeat(160));
});

Deno.test("shortError neutralises markup a helper quoted", () => {
  assertEquals(Text.shortError("boom: <img src=x>\n", "fallback"), "boom: ‹img src=x›");
});

Deno.test("plainText turns angle brackets into lookalikes", () => {
  assertEquals(Text.plainText('<img src="x">'), '‹img src="x"›');
  assertEquals(Text.plainText("JBL <3"), "JBL ‹3");
  assertEquals(Text.plainText(null), "");
  assertEquals(Text.plainText(undefined), "");
  assertEquals(Text.plainText(7), "7");
});

Deno.test("asBool accepts the strings `omarchy bar set` writes", () => {
  assertEquals(Text.asBool(false, true), false);
  assertEquals(Text.asBool(true, false), true);
  // The case this exists for: a JSON string, which is truthy in QML.
  assertEquals(Text.asBool("false", true), false);
  assertEquals(Text.asBool("off", true), false);
  assertEquals(Text.asBool("0", true), false);
  assertEquals(Text.asBool("no", true), false);
  assertEquals(Text.asBool("true", false), true);
  assertEquals(Text.asBool("on", false), true);
  assertEquals(Text.asBool("yes", false), true);
  assertEquals(Text.asBool(" TRUE ", false), true);
  assertEquals(Text.asBool(0, true), false);
  assertEquals(Text.asBool(1, false), true);
  // Anything unrecognised keeps the default rather than guessing.
  assertEquals(Text.asBool("maybe", true), true);
  assertEquals(Text.asBool(undefined, true), true);
  assertEquals(Text.asBool(null, false), false);
  assertEquals(Text.asBool("", false), false);
});

Deno.test("asInt parses numbers written as strings", () => {
  assertEquals(Text.asInt(15, 20), 15);
  assertEquals(Text.asInt("15", 20), 15);
  assertEquals(Text.asInt("15.6", 20), 16);
  assertEquals(Text.asInt("nonsense", 20), 20);
  assertEquals(Text.asInt(undefined, 20), 20);
  assertEquals(Text.asInt("", 20), 20);
  // Number("  ") is 0, which as a low-battery threshold would mean "never warn".
  assertEquals(Text.asInt("  ", 20), 20);
  assertEquals(Text.asInt(NaN, 20), 20);
  assertEquals(Text.asInt(Infinity, 20), 20);
});

Deno.test("clamp keeps a stepped dial inside its range", () => {
  assertEquals(Text.clamp(12, 0, 20), 12);
  // A step off either end stops there rather than asking for 21 or -1.
  assertEquals(Text.clamp(21, 0, 20), 20);
  assertEquals(Text.clamp(-1, 0, 20), 0);
  // The slider hands over a real, and the dial has whole stops only.
  assertEquals(Text.clamp(12.4, 0, 20), 12);
  assertEquals(Text.clamp(12.5, 0, 20), 13);
  assertEquals(Text.clamp("8", 0, 20), 8);
  assertEquals(Text.clamp("nonsense", 0, 20), 0);
  assertEquals(Text.clamp(undefined, 0, 20), 0);
  assertEquals(Text.clamp(undefined, 1, 7), 1);
});

Deno.test("parseObject takes one JSON object and refuses everything else", () => {
  assertEquals(Text.parseObject('{"a":1}'), { a: 1 });
  assertEquals(Text.parseObject('  {"a":1}\n'), { a: 1 });
  for (const raw of ["", "   ", null, undefined, "null", "42", '"x"', "[7,8]", "[]", "not json", "{"]) {
    assertEquals(Text.parseObject(raw), null, String(raw));
  }
});

Deno.test("withKey returns a new object with one key set or removed", () => {
  const before = { a: 1, b: 2 };
  const after = Text.withKey(before, "b", 3);
  assertEquals(after, { a: 1, b: 3 });
  assertEquals(after === before, false);
  assertEquals(before, { a: 1, b: 2 });
  assertEquals(Text.withKey(before, "b", undefined), { a: 1 });
  assertEquals(Text.withKey(null, "x", false), { x: false });
});
