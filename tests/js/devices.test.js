// lib/devices.js: which Bluetooth devices are followed, in what order, and
// how a headset is found again. The cases here are the ones that bite: a
// device list where the wrong device is easy to pick, one set listed on two
// adapters, and a name that tries to be markup.
//
//   deno test --allow-read tests/js/

import { assertEquals } from "jsr:@std/assert@1";
import { load, namesOf } from "./load.js";

const Devices = load("devices.js");

Deno.test("devices.js: every top-level name is reachable", () => {
  const names = namesOf("devices.js");
  for (const name of ["label", "isAudio", "matches", "rank", "followAddresses", "byAddress",
                      "orderHeadsets", "find", "readerAddresses", "uuidsFromInfo", "AUDIO_WORDS"]) {
    assertEquals(names.includes(name), true, name);
  }
  assertEquals(names.filter((name) => Devices[name] === undefined), []);
});

const buds = (extra = {}) => ({
  name: "JBL TUNE230NC TWS",
  address: "AA:BB:CC:DD:EE:FF",
  icon: "audio-headset",
  paired: true,
  connected: true,
  batteryAvailable: true,
  battery: 0.2,
  ...extra,
});

const mouse = (extra = {}) => ({
  name: "MX Master 4",
  address: "AA:BB:CC:DD:EE:FF",
  icon: "input-mouse",
  paired: true,
  connected: true,
  batteryAvailable: true,
  battery: 0.9,
  ...extra,
});

const labels = (list) => list.map((device) => Devices.label(device));

Deno.test("label prefers the name and falls back to the address", () => {
  assertEquals(Devices.label(buds()), "JBL TUNE230NC TWS");
  assertEquals(Devices.label({ name: "", deviceName: "", address: "AA:BB:CC:DD:EE:FF" }), "AA:BB:CC:DD:EE:FF");
  // With no user-set alias the device-reported name carries it.
  assertEquals(Devices.label({ name: "", deviceName: "Sony WH", address: "B0:11:22:33:44:55" }), "Sony WH");
  // A name of blanks is no name.
  assertEquals(Devices.label({ name: "   ", deviceName: "Sony WH" }), "Sony WH");
  assertEquals(Devices.label(null), "");
});

Deno.test("a device-authored name can never read as rich text", () => {
  assertEquals(Devices.label({ name: '<img src="http://evil/x.png">' }), "‹img src=\"http://evil/x.png\"›");
  assertEquals(Devices.label({ name: "", deviceName: "<b>Buds</b>" }), "‹b›Buds‹/b›");
  assertEquals(Devices.label({ name: "JBL <3" }), "JBL ‹3");
});

Deno.test("isAudio falls back to the name when BlueZ sends no icon", () => {
  assertEquals(Devices.isAudio({ icon: "", name: "Pixel Buds Pro" }), true);
  assertEquals(Devices.isAudio({ icon: "", name: "MX Master 4" }), false);
  assertEquals(Devices.isAudio({ icon: "audio-card", name: "Speaker" }), true);
  assertEquals(Devices.isAudio(null), false);
});

Deno.test("an empty filter accepts audio devices only", () => {
  assertEquals(Devices.matches(buds(), ""), true);
  assertEquals(Devices.matches(mouse(), ""), false);
  assertEquals(Devices.matches(mouse(), "   "), false);
  // A named filter is matched against the name and the address, case-insensitively.
  assertEquals(Devices.matches(buds(), "jbl"), true);
  assertEquals(Devices.matches(buds(), "aa:bb"), true);
  assertEquals(Devices.matches(buds(), "sony"), false);
  // ...and it overrides the audio check, so a filter can name anything.
  assertEquals(Devices.matches(mouse(), "MX Master"), true);
});

// The old pickDevice is gone; the best candidate is rank()[0].
Deno.test("the best candidate is the connected earbuds over a paired mouse", () => {
  assertEquals(labels(Devices.rank([mouse(), buds()], "")), ["JBL TUNE230NC TWS"]);
});

Deno.test("the best candidate is connected over the last used", () => {
  const sony = buds({ name: "WH-CH720N", address: "B0:11:22:33:44:55", connected: false });
  assertEquals(Devices.label(Devices.rank([sony, buds()], "", "B0:11:22:33:44:55")[0]), "JBL TUNE230NC TWS");
});

Deno.test("with nothing connected, the last used device ranks first", () => {
  const sony = buds({ name: "WH-CH720N", address: "B0:11:22:33:44:55", connected: false });
  const jbl = buds({ connected: false });
  assertEquals(Devices.label(Devices.rank([jbl, sony], "", "B0:11:22:33:44:55")[0]), "WH-CH720N");
  // Whatever case the remembered address was written in.
  assertEquals(Devices.label(Devices.rank([jbl, sony], "", "b0:11:22:33:44:55")[0]), "WH-CH720N");
});

Deno.test("rank ignores devices that are neither paired nor connected", () => {
  assertEquals(Devices.rank([buds({ paired: false, connected: false })], ""), []);
  // Connected but not paired still counts.
  assertEquals(labels(Devices.rank([buds({ paired: false })], "")), ["JBL TUNE230NC TWS"]);
  assertEquals(Devices.rank([null], ""), []);
  assertEquals(Devices.rank([], ""), []);
  assertEquals(Devices.rank(null, ""), []);
});

Deno.test("rank puts the connected first and orders the rest by name", () => {
  const sony = buds({ name: "WH-CH720N", address: "B0:11:22:33:44:55" });
  const jbl = buds();
  // Both connected and both reporting: the tie goes to the name, so the icons
  // keep their places in the bar between one reading and the next.
  assertEquals(labels(Devices.rank([sony, jbl], "")), ["JBL TUNE230NC TWS", "WH-CH720N"]);
  // ...and the set used last breaks it first, which is what the bar reads as
  // "the one you just put on".
  assertEquals(labels(Devices.rank([jbl, sony], "", "B0:11:22:33:44:55")), ["WH-CH720N", "JBL TUNE230NC TWS"]);
  // Connected still beats everything, whatever it is called.
  assertEquals(labels(Devices.rank([jbl, buds({ name: "AAA", connected: false })], "")), ["JBL TUNE230NC TWS", "AAA"]);
  // A reported battery beats none, between two that are connected.
  assertEquals(labels(Devices.rank([buds({ name: "AAA", batteryAvailable: false }), sony], "")), ["WH-CH720N", "AAA"]);
  // Same name, same points: the order they were listed in.
  const twin = buds({ address: "11:11:11:11:11:11" });
  assertEquals(Devices.rank([jbl, twin], "").map((d) => d.address), ["AA:BB:CC:DD:EE:FF", "11:11:11:11:11:11"]);
  assertEquals(Devices.rank([twin, jbl], "").map((d) => d.address), ["11:11:11:11:11:11", "AA:BB:CC:DD:EE:FF"]);
  assertEquals(Devices.rank([mouse()], ""), []);
});

Deno.test("followAddresses names every connected match", () => {
  const sony = buds({ name: "WH-CH720N", address: "B0:11:22:33:44:55" });
  const jbl = buds();
  // Two headsets on one desk are two followers, not a competition.
  assertEquals(Devices.followAddresses([sony, jbl], ""), ["AA:BB:CC:DD:EE:FF", "B0:11:22:33:44:55"]);
  // A filter still applies, and a mouse is never audio.
  assertEquals(Devices.followAddresses([sony, jbl, mouse()], "jbl"), ["AA:BB:CC:DD:EE:FF"]);
  // With nothing connected it falls back to the one device that would have been
  // picked, so the widget can name the earbuds it is waiting for.
  const parked = [
    buds({ connected: false }),
    buds({ name: "WH-CH720N", address: "B0:11:22:33:44:55", connected: false }),
  ];
  assertEquals(Devices.followAddresses(parked, "", "B0:11:22:33:44:55"), ["B0:11:22:33:44:55"]);
  // A connected device beats that fallback outright: the paired one is left out.
  assertEquals(Devices.followAddresses([parked[1], jbl], "", "B0:11:22:33:44:55"), ["AA:BB:CC:DD:EE:FF"]);
  assertEquals(Devices.followAddresses([mouse()], ""), []);
  assertEquals(Devices.followAddresses([], ""), []);
  assertEquals(Devices.followAddresses(null, ""), []);
});

Deno.test("byAddress is how a follower stays pinned to one set", () => {
  const sony = buds({ name: "WH-CH720N", address: "B0:11:22:33:44:55" });
  const jbl = buds();
  assertEquals(Devices.label(Devices.byAddress([jbl, sony], "B0:11:22:33:44:55")), "WH-CH720N");
  // Whatever cased the address, it is the same device.
  assertEquals(Devices.label(Devices.byAddress([jbl, sony], "b0:11:22:33:44:55")), "WH-CH720N");
  assertEquals(Devices.label(Devices.byAddress([jbl, sony], " B0:11:22:33:44:55 ")), "WH-CH720N");
  assertEquals(Devices.byAddress([jbl], "B0:11:22:33:44:55"), null);
  assertEquals(Devices.byAddress([jbl], ""), null);
  assertEquals(Devices.byAddress([{ address: "" }], ""), null);
  assertEquals(Devices.byAddress([null, jbl], "AA:BB:CC:DD:EE:FF"), jbl);
  assertEquals(Devices.byAddress(null, "AA:BB:CC:DD:EE:FF"), null);
});

Deno.test("the same earbuds listed on two adapters are the connected one, whichever comes first", () => {
  const paired = { address: "AA:BB:CC:00:00:01", name: "Redmi Buds 5", connected: false, adapter: "dongle" };
  const connected = { address: "AA:BB:CC:00:00:01", name: "Redmi Buds 5", connected: true, adapter: "internal" };
  assertEquals(Devices.byAddress([paired, connected], "aa:bb:cc:00:00:01"), connected);
  assertEquals(Devices.byAddress([connected, paired], "AA:BB:CC:00:00:01"), connected);
  // Neither connected: the first, as before.
  const other = { address: "AA:BB:CC:00:00:01", connected: false, adapter: "other" };
  assertEquals(Devices.byAddress([paired, other], "AA:BB:CC:00:00:01"), paired);
  assertEquals(Devices.byAddress([paired], "AA:BB:CC:00:00:02"), null);
  assertEquals(Devices.byAddress([], "AA:BB:CC:00:00:01"), null);
  assertEquals(Devices.byAddress([paired], ""), null);
});

Deno.test("orderHeadsets orders the icons the way the bar draws them", () => {
  const headset = (name, address, connected = true) => ({ name, address, connected });
  const jbl = headset("JBL TUNE230NC TWS", "AA:BB:CC:DD:EE:FF");
  const sony = headset("WH-CH720N", "B0:11:22:33:44:55");
  const names = (list) => list.map((h) => h.name);
  assertEquals(names(Devices.orderHeadsets([sony, jbl], "")), ["JBL TUNE230NC TWS", "WH-CH720N"]);
  assertEquals(names(Devices.orderHeadsets([jbl, sony], "b0:11:22:33:44:55")), ["WH-CH720N", "JBL TUNE230NC TWS"]);
  // A device in its case sinks below one that is here, whatever its name, and
  // being the set used last does not lift it above a connected one.
  const parked = headset("AAA", "11:22:33:44:55:66", false);
  assertEquals(names(Devices.orderHeadsets([parked, jbl], "")), ["JBL TUNE230NC TWS", "AAA"]);
  assertEquals(names(Devices.orderHeadsets([parked, jbl], "11:22:33:44:55:66")), ["JBL TUNE230NC TWS", "AAA"]);
  // Name case does not reorder them.
  assertEquals(names(Devices.orderHeadsets([headset("beta", "1"), headset("Alpha", "2")], "")), ["Alpha", "beta"]);
  assertEquals(names(Devices.orderHeadsets([null, jbl], "")), ["JBL TUNE230NC TWS"]);
  assertEquals(Devices.orderHeadsets([], ""), []);
  assertEquals(Devices.orderHeadsets(null, ""), []);
});

Deno.test("find answers for the device an IPC caller named", () => {
  const followed = [
    { name: "JBL TUNE230NC TWS", address: "AA:BB:CC:DD:EE:FF" },
    { name: "WH-CH720N", address: "B0:11:22:33:44:55", backend: "sony" },
    { name: "Xiaomi Buds 5 Pro", address: "64:8F:DB:87:06:CB", backend: "xiaomi" },
  ];
  // The brand is often only in the backend: a Sony calls itself "WH-CH720N".
  assertEquals(Devices.find(followed, "sony"), 1);
  assertEquals(Devices.find(followed, "xiaomi"), 2);
  assertEquals(Devices.find(followed, "buds 5"), 2);
  // Nothing named is the first device, which is what a no-argument call means.
  assertEquals(Devices.find(followed, ""), 0);
  assertEquals(Devices.find(followed, "  "), 0);
  assertEquals(Devices.find(followed, undefined), 0);
  assertEquals(Devices.find(followed, "jbl"), 0);
  assertEquals(Devices.find(followed, "ch720"), 1);
  // The address works as well as the name, and case is nobody's business.
  assertEquals(Devices.find(followed, "b0:11:22"), 1);
  assertEquals(Devices.find(followed, "WH-ch720n"), 1);
  assertEquals(Devices.find(followed, "airpods"), -1);
  assertEquals(Devices.find([null, followed[1]], "sony"), 1);
  assertEquals(Devices.find([], ""), -1);
  assertEquals(Devices.find(null, "jbl"), -1);
});

Deno.test("readerAddresses names the devices the one reader is started for", () => {
  const connected = { address: "A0:11:22:33:44:55", connected: true };
  const parked = { address: "B0:11:22:33:44:55", connected: false };
  assertEquals(Devices.readerAddresses([connected, parked]), ["A0:11:22:33:44:55"]);
  // Uppercased, so the address on a line and the address in the follow list are
  // written the same way.
  assertEquals(Devices.readerAddresses([{ address: "b0:11:22:33:44:55", connected: true }]), ["B0:11:22:33:44:55"]);
  // A device in its case has no channel to open, and asking BlueZ for one would
  // connect it behind its owner's back.
  assertEquals(Devices.readerAddresses([parked]), []);
  assertEquals(Devices.readerAddresses([{ address: "", connected: true }]), []);
  assertEquals(Devices.readerAddresses([connected, connected]), ["A0:11:22:33:44:55"]);
  assertEquals(Devices.readerAddresses([connected, { address: "a0:11:22:33:44:55", connected: true }]),
    ["A0:11:22:33:44:55"]);
  assertEquals(Devices.readerAddresses([null]), []);
  assertEquals(Devices.readerAddresses([]), []);
  assertEquals(Devices.readerAddresses(null), []);
});

Deno.test("uuidsFromInfo reads the UUID lines and nothing else", () => {
  const info = [
    "Device B0:11:22:33:44:55 (public)",
    "\tName: WH-CH720N",
    "\tIcon: audio-headset",
    "\tUUID: Headset                   (00001108-0000-1000-8000-00805f9b34fb)",
    "\tUUID: Vendor specific           (956C7B26-D49A-4BA8-B03F-B17D393CB6E2)",
    "\tModalias: usb:v054Cp0EAFd0112",
    "\tBattery Percentage: 0x64 (100)",
  ].join("\n");
  assertEquals(Devices.uuidsFromInfo(info), [
    "00001108-0000-1000-8000-00805f9b34fb",
    "956c7b26-d49a-4ba8-b03f-b17d393cb6e2",
  ]);
  // A failed probe, and a device that lists none, both read as "nothing seen".
  assertEquals(Devices.uuidsFromInfo(""), []);
  assertEquals(Devices.uuidsFromInfo(null), []);
  assertEquals(Devices.uuidsFromInfo("Device 00:11:22:33:44:55 not available"), []);
});
