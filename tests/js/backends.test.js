// lib/backends.js: which bridge controls a device, what it is started with,
// and what the mode controls look like on it. Many cases read the SDP record
// a real device printed (docs/captures/*-bluetoothctl.txt), so a new row's
// claim is checked against the devices that work today.
//
//   deno test --allow-read tests/js/

import { assertEquals } from "jsr:@std/assert@1";
import { load, namesOf } from "./load.js";

const Backends = load("backends.js");
const Devices = load("devices.js");
const UUID = Backends.UUID;
const BLE_ADDRESS = "48:B4:41:00:00:01";
const SPP = "00001101-0000-1000-8000-00805f9b34fb";
const ROOT = new URL("../../", import.meta.url);

const captured = (file) =>
  Devices.uuidsFromInfo(Deno.readTextFileSync(new URL("docs/captures/" + file, ROOT)));
// The same record, as printed, reversed and upper-cased: none of it may change the pick.
const variants = (ids) => [ids, [...ids].reverse(), ids.map((id) => id.toUpperCase())];
const rowOf = (id) => Backends.ROWS.find((r) => r.id === id);
const labelsOf = (buttons) => buttons.map((b) => b.label);

Deno.test("backends.js: every top-level name is reachable", () => {
  const names = namesOf("backends.js");
  for (const name of ["UUID", "ROWS", "pick", "brandOf", "isBle", "serviceUuid", "bridgeArgs",
                      "modesOffered", "modeButtons", "parseSupport", "supportVerdict", "BASE_MODES"]) {
    assertEquals(names.includes(name), true, name);
  }
  assertEquals(names.filter((name) => Backends[name] === undefined), []);
});

// ---- ROWS: the brand table the shell reads instead of a chain of ifs.

Deno.test("every backend row names a bridge that exists, with a test file and a pin", () => {
  const allowedArgs = ["address", "uuid", "name", "bleAddress", "modelId"];
  for (const row of Backends.ROWS) {
    assertEquals(typeof row.id, "string", "id");
    assertEquals(typeof row.brand, "string", row.id + " brand");
    // A row is named after its bridge, except a second row for a bridge that
    // must be started differently for one model (redmi-buds-3-pro, which needs
    // the name the Buds 5's row does not pass): it is named after that model.
    assertEquals(row.id === row.brand || row.id.startsWith(row.brand + "-"), true, row.id + ": brand name");
    assertEquals(Backends.brandOf(row.id), row.brand, row.id);
    assertEquals(Deno.statSync(new URL("bridges/brands/" + row.brand + ".py", ROOT)).isFile, true,
      row.id + " bridge file");
    assertEquals(Deno.statSync(new URL("tests/test_" + row.brand + ".py", ROOT)).isFile, true,
      row.id + " test file");
    const pins = [...Deno.readDirSync(new URL("tests/pins/" + row.brand + "/", ROOT))]
      .filter((entry) => entry.name.endsWith(".json"));
    assertEquals(pins.length > 0, true, row.id + " has a pin");
    assertEquals(Array.isArray(row.args), true, row.id + " args");
    assertEquals(row.args.filter((arg) => !allowedArgs.includes(arg)), [], row.id + " args");
    const claim = row.claim;
    assertEquals(!!claim.fastPair || (claim.uuids || []).length > 0 || !!claim.uuidPrefix, true,
      row.id + " claims something");
    // A name alone claims nothing: it must come with a service.
    if (claim.names) assertEquals((claim.uuids || []).length > 0, true, row.id + " names need uuids");
  }
  const ids = Backends.ROWS.map((r) => r.id);
  assertEquals(new Set(ids).size, ids.length, "row ids are unique");
});

Deno.test("no two backend rows claim the same thing, and each row's claim picks that row", () => {
  const seen = new Map();
  for (const row of Backends.ROWS) {
    const claim = row.claim;
    for (const id of claim.uuids || []) {
      assertEquals(id, id.toLowerCase(), row.id + " UUIDs are written lowercase");
      // A UUID with no name is the brand's own; with names, the pair is.
      for (const name of claim.names || [""]) {
        const key = name + "|" + id;
        assertEquals(seen.has(key), false, key + " claimed by " + seen.get(key) + " and " + row.id);
        seen.set(key, row.id);
        // Alone, and next to a Fast Pair address, which must not outrank it.
        assertEquals(Backends.pick([id], "", name), row.id, row.id + " " + id);
        assertEquals(Backends.pick([id], BLE_ADDRESS, name), row.id, row.id + " " + id + " + BLE");
      }
      if (claim.names) {
        // The service without the name is not this row.
        assertEquals(Backends.pick([id], ""), "", row.id + " " + id + " without its name");
        assertEquals(Backends.pick([id], BLE_ADDRESS), "jbl", row.id + " " + id + " without its name");
      }
    }
    if (claim.uuidPrefix) {
      assertEquals(Backends.pick([claim.uuidPrefix + "d1402"], ""), row.id);
      assertEquals(Backends.pick([claim.uuidPrefix + "d1402"], BLE_ADDRESS), row.id);
    }
  }
  // A brand's own UUID is never also a named row's service.
  for (const row of Backends.ROWS.filter((r) => !r.claim.names)) {
    for (const id of row.claim.uuids || []) {
      for (const other of Backends.ROWS.filter((r) => r.claim.names)) {
        assertEquals(other.claim.uuids.includes(id), false, id + ": " + row.id + " and " + other.id);
      }
    }
  }
  assertEquals(Backends.pick([], BLE_ADDRESS), "jbl");
  assertEquals(Backends.pick([], ""), "");
});

Deno.test("the Fast Pair row is last and the only BLE one", () => {
  const last = Backends.ROWS[Backends.ROWS.length - 1];
  assertEquals(last.id, "jbl");
  for (const row of Backends.ROWS) {
    assertEquals(Backends.isBle(row.id), row.id === "jbl", row.id);
    assertEquals(!!row.claim.fastPair, row.id === "jbl", row.id);
  }
  for (const id of ["", "nonesuch", null, undefined]) assertEquals(Backends.isBle(id), false, String(id));
});

Deno.test("the Moto row claims moto buds+ by name and its profile UUID, ahead of the JBL row", () => {
  const moto = rowOf("moto");
  assertEquals(moto.brand, "moto");
  assertEquals(moto.claim.names, ["moto buds+"]);
  assertEquals(moto.claim.uuids, ["fc9d9fe0-4899-11ee-be56-0242ac120002"]);
  assertEquals(UUID.moto, "fc9d9fe0-4899-11ee-be56-0242ac120002");
  const index = (id) => Backends.ROWS.findIndex((r) => r.id === id);
  assertEquals(index("moto") < index("jbl"), true);
  const ids = ["0000110b-0000-1000-8000-00805f9b34fb", UUID.moto];
  for (const uuids of variants(ids)) {
    assertEquals(Backends.pick(uuids, "", "moto buds+"), "moto");
    // Its Fast Pair address does not hand it to the JBL bridge.
    assertEquals(Backends.pick(uuids, BLE_ADDRESS, "moto buds+"), "moto");
  }
  assertEquals(Backends.pick(ids, ""), "");
  assertEquals(Backends.pick(ids, BLE_ADDRESS), "jbl");
  assertEquals(Backends.pick(["0000110b-0000-1000-8000-00805f9b34fb"], "", "moto buds+"), "");
  assertEquals(Backends.isBle("moto"), false);
  assertEquals(Backends.hasAmbientDial("moto"), false);
  assertEquals(Backends.bridgeArgs("moto", { address: "54:2F:04:11:D5:E8", name: "moto buds+" }),
    ["54:2F:04:11:D5:E8", "moto buds+"]);
});

Deno.test("pick chooses the protocol the device advertises", () => {
  const sony = ["0000110b-0000-1000-8000-00805f9b34fb", UUID.sonyV2];
  // The SDP record is the stronger statement: a Sony headset that also happens
  // to have a Fast Pair address is still driven over its own channel.
  assertEquals(Backends.pick(sony, ""), "sony");
  assertEquals(Backends.pick(sony, BLE_ADDRESS), "sony");
  // bluetoothctl prints them lowercase, but nothing guarantees it.
  assertEquals(Backends.pick([UUID.sonyV2.toUpperCase()], ""), "sony");
  assertEquals(Backends.pick([" " + UUID.sonyV2 + " "], ""), "sony");
  // CSR GAIA in the SDP record is Xiaomi / QCC on SPP, and beats a BLE address
  // the same way Sony does: the UUID is the device's own claim.
  const xiaomi = [SPP, UUID.gaia];
  assertEquals(Backends.pick(xiaomi, ""), "xiaomi");
  assertEquals(Backends.pick(xiaomi, BLE_ADDRESS), "xiaomi");
  assertEquals(Backends.pick([UUID.gaia.toUpperCase()], ""), "xiaomi");
  // Soundcore vendor UUID in the SDP record picks the soundcore backend.
  const soundcore = [SPP, "0cf12d31-fac3-4553-bd80-d6832e7d1402"];
  assertEquals(Backends.pick(soundcore, ""), "soundcore");
  assertEquals(Backends.pick(soundcore, BLE_ADDRESS), "soundcore");
  assertEquals(Backends.pick(["0CF12D31-FAC3-4553-BD80-D6832E7D1402"], ""), "soundcore");
  // Samsung's own UUID gets its own bridge instead of falling through to JBL.
  const samsung = [SPP, UUID.samsung];
  assertEquals(Backends.pick(samsung, ""), "samsung");
  assertEquals(Backends.pick(samsung, BLE_ADDRESS), "samsung");
  assertEquals(Backends.pick([UUID.samsung.toUpperCase()], ""), "samsung");
  // Sony still wins when both vendor UUIDs are listed.
  assertEquals(Backends.pick([UUID.gaia, UUID.sonyV2], ""), "sony");
  // No vendor UUID: the BLE address from the Message Stream is what is left.
  assertEquals(Backends.pick([], BLE_ADDRESS), "jbl");
  assertEquals(Backends.pick(null, BLE_ADDRESS), "jbl");
  // Nothing to reach the device with.
  assertEquals(Backends.pick([], ""), "");
  assertEquals(Backends.pick(null, ""), "");
  assertEquals(Backends.pick([], "   "), "");
  assertEquals(Backends.pick([null, undefined], ""), "");
});

Deno.test("either Sony MDR UUID reaches the sony bridge", () => {
  const v1 = ["0000110b-0000-1000-8000-00805f9b34fb", UUID.sonyV1];
  assertEquals(Backends.pick(v1, ""), "sony");
  assertEquals(Backends.pick([UUID.sonyV1.toUpperCase()], ""), "sony");
  assertEquals(Backends.pick(v1, BLE_ADDRESS), "sony");
});

Deno.test("serviceUuid hands the Sony bridge the generation to register", () => {
  assertEquals(Backends.serviceUuid("sony", [UUID.sonyV2]), UUID.sonyV2);
  assertEquals(Backends.serviceUuid("sony", [UUID.sonyV1]), UUID.sonyV1);
  // Case, blanks and neighbours in the record change nothing.
  assertEquals(
    Backends.serviceUuid("sony", ["0000110b-0000-1000-8000-00805f9b34fb", UUID.sonyV1.toUpperCase()]),
    UUID.sonyV1,
  );
  assertEquals(Backends.serviceUuid("sony", [" " + UUID.sonyV1.toUpperCase()]), UUID.sonyV1);
  // Both listed: v2 is what the working Sonys are connected on, whichever
  // order the record prints them in.
  assertEquals(Backends.serviceUuid("sony", [UUID.sonyV1, UUID.sonyV2]), UUID.sonyV2);
  assertEquals(Backends.serviceUuid("sony", [UUID.sonyV2, UUID.sonyV1]), UUID.sonyV2);
  // Not a Sony, and nothing at all.
  assertEquals(Backends.serviceUuid("sony", [UUID.gaia]), "");
  assertEquals(Backends.serviceUuid("sony", []), "");
  assertEquals(Backends.serviceUuid("sony", null), "");
});

// Whenever pick says "sony", there is a UUID to hand the bridge: the headset
// passes one without checking, and an empty one would send the bridge an
// argument it rejects.
Deno.test("a sony device always has a UUID to register", () => {
  for (const uuid of [UUID.sonyV2, UUID.sonyV1]) {
    assertEquals(Backends.pick([uuid], ""), "sony");
    assertEquals(Backends.serviceUuid("sony", [uuid]) !== "", true);
  }
});

Deno.test("serviceUuid hands the Soundcore bridge the model's own vendor UUID", () => {
  const q30 = "0cf12d31-fac3-4553-bd80-d6832e7b302a";
  assertEquals(rowOf("soundcore").args, ["address", "uuid"]);
  assertEquals(Backends.serviceUuid("soundcore", [SPP, q30]), q30);
  assertEquals(Backends.serviceUuid("soundcore", [SPP, q30.toUpperCase()]), q30);
  assertEquals(Backends.serviceUuid("soundcore", [" " + q30 + " "]), q30);
  assertEquals(Backends.serviceUuid("soundcore", ["0cf12d31-fac3-4553-bd80-d6832e7d1402"]),
    "0cf12d31-fac3-4553-bd80-d6832e7d1402");
  // No UUID with the brand's prefix: nothing to pass.
  assertEquals(Backends.serviceUuid("soundcore", [SPP]), "");
  assertEquals(Backends.serviceUuid("soundcore", []), "");
  assertEquals(Backends.serviceUuid("soundcore", null), "");
  assertEquals(Backends.bridgeArgs("soundcore", { address: "88:0E:85:5F:64:B4", uuid: q30 }),
    ["88:0E:85:5F:64:B4", q30]);
});

// What the follower once decided by comparing the backend with a brand's name
// now comes from the rows. Each answer is the one the old check gave.
Deno.test("the rows answer what the follower once asked by brand name", () => {
  const withUuid = ["sony", "soundcore"];
  for (const row of Backends.ROWS) {
    assertEquals(Backends.hasAmbientDial(row.id), ["sony", "soundcore", "airoha"].includes(row.id), row.id);
    const command = { sony: "voice", soundcore: "wind" }[row.id] || "";
    assertEquals(Backends.ambientRange(row.id).command, command, row.id);
    // The airoha row has a dial and no switch: an empty key and label.
    const key = row.id === "soundcore" ? "Wind" : row.id === "airoha" ? "" : "Voice";
    assertEquals(Backends.ambientRange(row.id).key, key, row.id);
    assertEquals(Backends.strengthRange(row.id), row.id === "airoha" ? { min: 1, max: 7 } : null, row.id);
    // Only the rows that take a "uuid" argument are ever handed one.
    assertEquals(row.args.includes("uuid"), withUuid.includes(row.id), row.id);
    if (!withUuid.includes(row.id)) {
      assertEquals(Backends.serviceUuid(row.id, [UUID.sonyV2, ...(row.claim.uuids || [])]), "", row.id);
    }
  }
  for (const id of ["", "nonesuch", null]) {
    assertEquals(Backends.isBle(id), false);
    assertEquals(Backends.hasAmbientDial(id), false);
    assertEquals(Backends.ambientRange(id).command, "");
    assertEquals(Backends.strengthRange(id), null);
    assertEquals(Backends.serviceUuid(id, [UUID.sonyV2]), "");
  }
  assertEquals(Backends.serviceUuid("sony", [UUID.sonyV1, UUID.sonyV2]), UUID.sonyV2);
  assertEquals(Backends.serviceUuid("sony", [" " + UUID.sonyV1.toUpperCase()]), UUID.sonyV1);
  assertEquals(Backends.serviceUuid("sony", [UUID.gaia]), "");
});

Deno.test("brandOf names the row's bridge module, and nothing for a backend with no row", () => {
  assertEquals(Backends.brandOf("sony"), "sony");
  assertEquals(Backends.brandOf("jbl"), "jbl");
  assertEquals(Backends.brandOf("huawei"), "huawei");
  assertEquals(Backends.brandOf("redmi"), "redmi");
  // A second row for one model starts the same bridge.
  assertEquals(Backends.brandOf("redmi-buds-3-pro"), "redmi");
  assertEquals(Backends.brandOf(""), "");
  assertEquals(Backends.brandOf("nope"), "");
  assertEquals(Backends.brandOf(null), "");
});

Deno.test("bridgeArgs is what each bridge was always sent", () => {
  const known = {
    address: "94:DB:56:D0:F0:F0",
    uuid: UUID.sonyV1,
    name: "WH-1000XM4",
    bleAddress: BLE_ADDRESS,
    modelId: "0x1234",
  };
  // sony: address, the MDR generation, the reported name.
  assertEquals(Backends.bridgeArgs("sony", known), ["94:DB:56:D0:F0:F0", UUID.sonyV1, "WH-1000XM4"]);
  // No name known yet is "", the nameless caller the sony bridge treats as the
  // old one, never a missing argument.
  assertEquals(Backends.bridgeArgs("sony", { address: "94:DB:56:D0:F0:F0", uuid: UUID.sonyV2 }),
    ["94:DB:56:D0:F0:F0", UUID.sonyV2, ""]);
  // Nothing receives the reported name for channel selection.
  assertEquals(Backends.bridgeArgs("nothing", { address: known.address, name: "CMF Headphone Pro" }),
    [known.address, "CMF Headphone Pro"]);
  assertEquals(Backends.bridgeArgs("nothing", { address: known.address, name: "Nothing Ear (a)" }),
    [known.address, "Nothing Ear (a)"]);
  assertEquals(Backends.bridgeArgs("nothing", { address: known.address }), [known.address, ""]);
  // These classic bridges: the address alone.
  for (const id of ["samsung", "xiaomi", "oppo", "bose", "redmi"]) {
    assertEquals(Backends.bridgeArgs(id, known), ["94:DB:56:D0:F0:F0"], id);
  }
  // soundcore: the address and its own vendor UUID (here the Sony one known holds).
  assertEquals(Backends.bridgeArgs("soundcore", known), ["94:DB:56:D0:F0:F0", UUID.sonyV1]);
  assertEquals(Backends.bridgeArgs("soundcore", { address: known.address }), [known.address, ""]);
  // jbl: the BLE address and the Fast Pair model.
  assertEquals(Backends.bridgeArgs("jbl", known), [BLE_ADDRESS, "0x1234"]);
  assertEquals(Backends.bridgeArgs("jbl", { bleAddress: BLE_ADDRESS }), [BLE_ADDRESS, ""]);
  assertEquals(Backends.bridgeArgs("jbl", null), ["", ""]);
  assertEquals(Backends.bridgeArgs("", known), []);
  assertEquals(Backends.bridgeArgs("nope", known), []);
});

Deno.test("ambientRange is Soundcore's dial on Soundcore and Sony's everywhere else", () => {
  assertEquals(Backends.ambientRange("soundcore"),
    { min: 1, max: 5, voice: "Wind noise reduction", command: "wind", key: "Wind" });
  assertEquals(Backends.ambientRange("sony"),
    { min: 0, max: 20, voice: "Focus on voice", command: "voice", key: "Voice" });
  for (const id of ["jbl", "nothing", "xiaomi", "samsung", "oppo", "moto", "", "nope"]) {
    assertEquals(Backends.ambientRange(id),
      { min: 0, max: 20, voice: "Focus on voice", command: "", key: "Voice" }, id);
  }
});

Deno.test("pick chooses nothing from the NT Link UUID", () => {
  const nothing = ["0000110b-0000-1000-8000-00805f9b34fb", UUID.nothing];
  assertEquals(Backends.pick(nothing, ""), "nothing");
  // A Fast Pair address does not outrank the device's own record.
  assertEquals(Backends.pick(nothing, BLE_ADDRESS), "nothing");
  assertEquals(Backends.pick([UUID.nothing.toUpperCase()], ""), "nothing");
  // Sony still wins; GAIA loses to Nothing the same way it loses to Sony.
  assertEquals(Backends.pick([UUID.nothing, UUID.sonyV2], ""), "sony");
  assertEquals(Backends.pick([UUID.gaia, UUID.nothing], ""), "nothing");
  for (const id of ["nothing", "sony", "samsung", "xiaomi", "soundcore"]) assertEquals(Backends.isBle(id), false, id);
  assertEquals(Backends.isBle("jbl"), true);
});

Deno.test("pick chooses oppo from the HeyMelody UUID", () => {
  const oppo = [SPP, UUID.oppo];
  assertEquals(Backends.pick(oppo, ""), "oppo");
  // A Fast Pair address does not outrank the device's own record.
  assertEquals(Backends.pick(oppo, BLE_ADDRESS), "oppo");
  assertEquals(Backends.pick([UUID.oppo.toUpperCase()], ""), "oppo");
  // Every earlier backend wins over it; it wins over the JBL fallback.
  assertEquals(Backends.pick([UUID.oppo, UUID.sonyV2], ""), "sony");
  assertEquals(Backends.pick([UUID.samsung, UUID.oppo], ""), "samsung");
  assertEquals(Backends.pick([UUID.nothing, UUID.oppo], ""), "nothing");
  assertEquals(Backends.pick([UUID.gaia, UUID.oppo], ""), "xiaomi");
  assertEquals(Backends.pick([UUID.oppo, UUID.soundcorePrefix + "d1402"], ""), "soundcore");
  assertEquals(Backends.isBle("oppo"), false);
});

// The SDP records of devices that work today. The Xiaomi list is the one
// PROTOCOL.md prints from bluetoothctl; Samsung, Nothing Ear (a) and the
// WH-1000XM4 are put together from the UUIDs PROTOCOL.md names plus the
// standard audio profiles. A new brand's claim must leave each of these where
// it is. Add your device's list, the real one from `bluetoothctl info`, with
// your row.
const DEVICES = [
  {
    model: "Xiaomi Buds 5 Pro",
    backend: "xiaomi",
    uuids: [
      "00001100-d102-11e1-9b23-00025b00a5a5",
      "00001101-0000-1000-8000-00805f9b34fb",
      "00000837-d103-0004-bf7f-2942153d354b",
      "2587db3c-ce70-4fc9-935f-777ab4188fd7",
    ],
    bleAddress: "",
  },
  {
    model: "Samsung Galaxy Buds2",
    backend: "samsung",
    uuids: [
      "0000110b-0000-1000-8000-00805f9b34fb",
      "0000111e-0000-1000-8000-00805f9b34fb",
      "df21fe2c-2515-4fdb-8886-f12c4d67927c",
      "2e73a4ad-332d-41fc-90e2-16bef06523f2",
    ],
    bleAddress: BLE_ADDRESS,
  },
  {
    model: "Nothing Ear (a)",
    backend: "nothing",
    uuids: [
      "0000110b-0000-1000-8000-00805f9b34fb",
      "df21fe2c-2515-4fdb-8886-f12c4d67927c",
      "aeac4a03-dff5-498f-843a-34487cf133eb",
    ],
    bleAddress: BLE_ADDRESS,
  },
  {
    // The real list from `bluetoothctl info`. Two vendor UUIDs (0000fdb3-...
    // and the 66666666-... placeholder Nothing ships) that no row claims,
    // alongside NT Link; the standard audio profiles fill the rest. Same
    // backend as the earbuds, a different RFCOMM channel.
    model: "CMF Headphone Pro",
    backend: "nothing",
    uuids: [
      "00001000-0000-1000-8000-00805f9b34fb",
      "00001101-0000-1000-8000-00805f9b34fb",
      "0000110b-0000-1000-8000-00805f9b34fb",
      "0000110c-0000-1000-8000-00805f9b34fb",
      "0000110d-0000-1000-8000-00805f9b34fb",
      "0000110e-0000-1000-8000-00805f9b34fb",
      "0000110f-0000-1000-8000-00805f9b34fb",
      "0000111e-0000-1000-8000-00805f9b34fb",
      "00001200-0000-1000-8000-00805f9b34fb",
      "00001203-0000-1000-8000-00805f9b34fb",
      "00001800-0000-1000-8000-00805f9b34fb",
      "00001801-0000-1000-8000-00805f9b34fb",
      "0000fdb3-3817-47d9-a10a-1f6656618f8e",
      "66666666-6666-6666-6666-666666666666",
      "aeac4a03-dff5-498f-843a-34487cf133eb",
      "df21fe2c-2515-4fdb-8886-f12c4d67927c",
    ],
    bleAddress: BLE_ADDRESS,
  },
  {
    model: "Bose QC45",
    backend: "bose",
    // The real list from `bluetoothctl info`. BMAP is served on a raw RFCOMM
    // channel the record does not name; the two vendor UUIDs are what claims
    // the device.
    uuids: [
      "00000000-deca-fade-deca-deafdecacaff",
      "00001101-0000-1000-8000-00805f9b34fb",
      "00001108-0000-1000-8000-00805f9b34fb",
      "0000110a-0000-1000-8000-00805f9b34fb",
      "0000110b-0000-1000-8000-00805f9b34fb",
      "0000110c-0000-1000-8000-00805f9b34fb",
      "0000110d-0000-1000-8000-00805f9b34fb",
      "0000110e-0000-1000-8000-00805f9b34fb",
      "0000110f-0000-1000-8000-00805f9b34fb",
      "0000111e-0000-1000-8000-00805f9b34fb",
      "0000112e-0000-1000-8000-00805f9b34fb",
      "00001130-0000-1000-8000-00805f9b34fb",
      "00001131-0000-1000-8000-00805f9b34fb",
      "00001200-0000-1000-8000-00805f9b34fb",
      "9b26d8c0-a8ed-440b-95b0-c4714a518bcc",
    ],
    bleAddress: BLE_ADDRESS,
  },
  {
    model: "Bose QC35",
    backend: "bose",
    // The real list from docs/captures/bose-qc35.txt. Only the deca-fade
    // placeholder claims it; unlike the QC45 it serves no 9b26d8c0 SPP UUID.
    uuids: [
      "00000000-deca-fade-deca-deafdecacaff",
      "00001101-0000-1000-8000-00805f9b34fb",
      "00001108-0000-1000-8000-00805f9b34fb",
      "0000110a-0000-1000-8000-00805f9b34fb",
      "0000110b-0000-1000-8000-00805f9b34fb",
      "0000110c-0000-1000-8000-00805f9b34fb",
      "0000110d-0000-1000-8000-00805f9b34fb",
      "0000110e-0000-1000-8000-00805f9b34fb",
      "0000110f-0000-1000-8000-00805f9b34fb",
      "0000111e-0000-1000-8000-00805f9b34fb",
      "0000112e-0000-1000-8000-00805f9b34fb",
      "00001130-0000-1000-8000-00805f9b34fb",
      "00001131-0000-1000-8000-00805f9b34fb",
      "00001200-0000-1000-8000-00805f9b34fb",
    ],
    bleAddress: BLE_ADDRESS,
  },
  {
    model: "Sony WH-1000XM4",
    backend: "sony",
    uuids: [
      "0000110b-0000-1000-8000-00805f9b34fb",
      "df21fe2c-2515-4fdb-8886-f12c4d67927c",
      "96cc203e-5068-46ad-b32d-e316f5e069ba",
    ],
    bleAddress: BLE_ADDRESS,
  },
];

Deno.test("the devices that work today still get their backend", () => {
  for (const device of DEVICES) {
    assertEquals(Backends.pick(device.uuids, device.bleAddress), device.backend, device.model);
    assertEquals(Backends.pick(device.uuids, device.bleAddress, device.model), device.backend, device.model);
  }
});

Deno.test("no named row takes another device's claim", () => {
  // Every reported name a row asks for, given to every device that works
  // today: the device's own record still decides.
  const names = Backends.ROWS.flatMap((r) => r.claim.names || []);
  assertEquals(names.length > 0, true);
  for (const name of names) {
    for (const device of DEVICES) {
      assertEquals(Backends.pick(device.uuids, device.bleAddress, name), device.backend, device.model + " / " + name);
    }
  }
});

// ---- Captured records: complete bluetoothctl output, not a guessed UUID list.

const canonical = JSON.parse(Deno.readTextFileSync(new URL("tests/fixtures/canonical.json", ROOT)));

// The Life Q30's own record, as bluetoothctl printed it: the complete list,
// not a hand-picked pair. Its vendor UUID carries the brand prefix with the
// model's own tail, and nothing else in the record may take the device first.
Deno.test("captured Life Q30 record selects the soundcore bridge and hands it its UUID", () => {
  const ids = captured("soundcore-life-q30-bluetoothctl.txt");
  assertEquals(ids.length, 8, "complete captured UUID list");
  for (const uuids of variants(ids)) {
    assertEquals(Backends.pick(uuids, ""), "soundcore");
    assertEquals(Backends.pick(uuids, BLE_ADDRESS), "soundcore");
    assertEquals(Backends.serviceUuid("soundcore", uuids), "0cf12d31-fac3-4553-bd80-d6832e7b302a");
  }
  const uuid = Backends.serviceUuid("soundcore", ids);
  assertEquals(Backends.bridgeArgs("soundcore", { address: "88:0E:85:5F:64:B4", uuid }),
    ["88:0E:85:5F:64:B4", "0cf12d31-fac3-4553-bd80-d6832e7b302a"]);
});

Deno.test("captured QCY H3 record selects no control backend", () => {
  const ids = captured("qcy-h3-bluetoothctl.txt");
  assertEquals(ids.length > 0, true);
  assertEquals(Backends.pick(ids, ""), "");
});

Deno.test("canonical Sony and JBL SDP records select their own bridge", () => {
  assertEquals(Object.keys(canonical).sort(), ["jbl", "sony"]);
  for (const [brand, fixture] of Object.entries(canonical)) {
    const record = Deno.readTextFileSync(new URL(fixture.uuid_capture, ROOT));
    const ids = Devices.uuidsFromInfo(record);
    const expectedCount = brand === "sony" ? 30 : 16;
    assertEquals(ids.length, expectedCount, brand + ": complete captured UUID list");
    const ble = fixture.gfps.ble.hex.split(" ").slice(4).join(":").toUpperCase();
    for (const uuids of variants(ids)) assertEquals(Backends.pick(uuids, ble), brand);
    assertEquals(Backends.pick(ids, ""), brand === "sony" ? "sony" : "");
    assertEquals(Backends.bridgeArgs(brand, {
      address: fixture.address, uuid: Backends.serviceUuid(brand, ids), name: "WH-CH720N",
      bleAddress: ble, modelId: "71f20a",
    }), brand === "sony"
      ? [fixture.address, UUID.sonyV2, "WH-CH720N"]
      : [ble, "71f20a"]);
  }
});

Deno.test("CMF Buds 2 complete owner SDP selects Nothing and passes reported name", () => {
  const ids = captured("nothing-cmf-buds-2-bluetoothctl.txt");
  assertEquals(ids.length, 18);
  for (const uuids of variants(ids)) {
    for (const ble of ["", BLE_ADDRESS]) assertEquals(Backends.pick(uuids, ble), "nothing");
  }
  assertEquals(Backends.bridgeArgs("nothing", { address: "3C:B0:ED:D0:AC:0B", name: "CMF Buds 2" }),
    ["3C:B0:ED:D0:AC:0B", "CMF Buds 2"]);
});

Deno.test("WH-CH520 owner SDP retains Sony routing for battery-only handling", () => {
  const ids = captured("sony-wh-ch520-bluetoothctl.txt");
  assertEquals(ids.length, 13);
  for (const uuids of variants(ids)) {
    assertEquals(Backends.pick(uuids, ""), "sony");
    assertEquals(Backends.pick(uuids, "44:51:D3:80:51:63"), "sony");
    assertEquals(Backends.serviceUuid("sony", uuids), UUID.sonyV2);
  }
  assertEquals(Backends.bridgeArgs("sony", { address: "E8:9E:13:CF:9A:71", uuid: UUID.sonyV2, name: "WH-CH520" }),
    ["E8:9E:13:CF:9A:71", UUID.sonyV2, "WH-CH520"]);
});

Deno.test("NC9 Pro routing requires its reported name and observed service", () => {
  const ids = captured("tozo-nc9-pro-bluetoothctl.txt");
  assertEquals(ids.length, 9);
  for (const uuids of variants(ids)) assertEquals(Backends.pick(uuids, "", "TOZO NC9 Pro"), "tozo");
  assertEquals(Backends.pick(ids, "", "Someone else's SPP headphones"), "");
  assertEquals(Backends.pick(ids, "", "TOZO NC9 Pro Box"), "");
  assertEquals(Backends.pick(ids, ""), "");
  assertEquals(Backends.pick([], "", "TOZO NC9 Pro"), "");
  assertEquals(Backends.pick([UUID.sonyV2, ...ids], "", "TOZO NC9 Pro"), "sony");
  assertEquals(Backends.pick([UUID.tozo], "", "TOZO NC9 Pro"), "tozo");
  assertEquals(Backends.bridgeArgs("tozo", { address: "94:4B:F8:C1:5F:98", name: "TOZO NC9 Pro" }),
    ["94:4B:F8:C1:5F:98", "TOZO NC9 Pro"]);
});

Deno.test("FreeBuds SE 2 routing requires its reported name and its SPP service", () => {
  const ids = captured("huawei-freebuds-se-2-bluetoothctl.txt");
  assertEquals(ids.length, 12, "complete captured UUID list");
  const name = "HUAWEI FreeBuds SE 2";
  for (const uuids of variants(ids)) assertEquals(Backends.pick(uuids, "", name), "huawei");
  // The record alone claims nothing: plain SPP is on half the headsets here.
  assertEquals(Backends.pick(ids, ""), "");
  assertEquals(Backends.pick(ids, "", "HUAWEI FreeBuds SE"), "");
  assertEquals(Backends.pick([], "", name), "");
  // Every brand with a claim of its own still wins.
  assertEquals(Backends.pick([UUID.sonyV2, ...ids], "", name), "sony");
  // The TOZO row is untouched by the shared SPP UUID.
  assertEquals(Backends.pick(ids, "", "TOZO NC9 Pro"), "tozo");
  assertEquals(Backends.isBle("huawei"), false);
  assertEquals(Backends.brandOf("huawei"), "huawei");
  assertEquals(Backends.bridgeArgs("huawei", { address: "AC:33:28:8C:A2:CD", name }), ["AC:33:28:8C:A2:CD", name]);
  // Battery only: the empty list hides the mode row.
  assertEquals(Backends.modesOffered({ available: [] }, "huawei"), []);
  assertEquals(Backends.modeButtons([], "huawei"), []);
});

// ---- Listening modes.

Deno.test("modesOffered defaults to the JBL four and filters what Sony names", () => {
  // The JBL bridge names none: its protocol has one fixed set of slots.
  assertEquals(Backends.modesOffered({ modes: true, mode: "anc" }, "jbl"), ["off", "anc", "ambient", "talkthru"]);
  assertEquals(Backends.modesOffered({}), ["off", "anc", "ambient", "talkthru"]);
  assertEquals(Backends.modesOffered(null), ["off", "anc", "ambient", "talkthru"]);
  // A list that is not a list is the same as none.
  assertEquals(Backends.modesOffered({ available: "off" }), ["off", "anc", "ambient", "talkthru"]);
  // A Sony over-ear, in the panel's fixed order rather than the line's.
  assertEquals(Backends.modesOffered({ available: ["ambient", "off", "anc"] }, "sony"), ["off", "anc", "ambient"]);
  // Names this backend should not send are dropped, not drawn.
  assertEquals(Backends.modesOffered({ available: ["off", "wind"] }, "sony"), ["off"]);
  assertEquals(Backends.modesOffered({ available: ["off", "wind"] }), ["off"]);
  // An explicit empty list means "offers nothing", which hides the row: very
  // different from a line that never mentioned the subject.
  assertEquals(Backends.modesOffered({ available: [] }, "sony"), []);
  // The default is a copy, not the table itself.
  const offered = Backends.modesOffered({});
  offered.push("x");
  assertEquals(Backends.BASE_MODES, ["off", "anc", "ambient", "talkthru"]);
});

Deno.test("TOZO six modes are explicit and keep the existing default four", () => {
  const modes = ["off", "anc", "ambient", "wind", "leisure", "adaptive"];
  assertEquals(Backends.modesOffered({ available: modes }, "tozo"), modes);
  assertEquals(Backends.modesOffered({ available: [...modes].reverse() }, "tozo"), modes);
  assertEquals(Backends.modesOffered({}, "tozo"), ["off", "anc", "ambient", "talkthru"]);
  assertEquals(labelsOf(Backends.modeButtons(modes, "tozo")),
    ["Normal", "ANC", "Transparency", "Wind Noise", "Leisure", "Adaptive"]);
  assertEquals(labelsOf(Backends.modeButtons(Backends.modesOffered({}), "jbl")), ["Off", "ANC", "Ambient", "TalkThru"]);
  const keys = Backends.modeButtons(modes, "tozo").map((b) => b.key);
  assertEquals(new Set(keys).size, 6);
  // A renamed button keeps its tooltip and its key.
  assertEquals(Backends.modeButtons(["off"], "tozo"), [{ value: "off", key: "o", label: "Normal", tooltip: "No noise control" }]);
});

Deno.test("extra modes come from the backend row, and only that row", () => {
  const six = ["off", "anc", "ambient", "wind", "leisure", "adaptive"];
  const base = Backends.BASE_MODES;
  const defaults = { off: "Off", anc: "ANC", ambient: "Ambient", talkthru: "TalkThru" };
  for (const row of Backends.ROWS.filter((r) => r.id !== "tozo")) {
    assertEquals(Backends.modeNames(row.id), base, row.id);
    assertEquals(Backends.modesOffered({ available: six }, row.id), ["off", "anc", "ambient"], row.id);
    // The four default names, except where the row renames one through
    // modeLabels (Redmi's Transparency).
    assertEquals(labelsOf(Backends.modeButtons(base, row.id)),
      base.map((m) => (row.modeLabels || {})[m] || defaults[m]), row.id);
  }
  assertEquals(Backends.modeNames("tozo"), ["off", "anc", "ambient", "talkthru", "wind", "leisure", "adaptive"]);
  assertEquals(labelsOf(Backends.modeButtons(base, "redmi")), ["Off", "ANC", "Transparency", "TalkThru"]);
  assertEquals(Backends.modesOffered({ available: six }, ""), ["off", "anc", "ambient"]);
  assertEquals(Backends.modesOffered({}, "tozo"), base);
  assertEquals(Backends.allModeNames(), [...base, "wind", "leisure", "adaptive"]);
  // Every name a button exists for is one some backend may report.
  assertEquals(Backends.MODE_BUTTONS.map((b) => b.value), Backends.allModeNames());
});

// ---- ANC strength in named grades.

Deno.test("ancLevelsOffered is empty unless the bridge graded the noise cancelling", () => {
  // The JBL and Sony bridges never mention strengths: no row.
  assertEquals(Backends.ancLevelsOffered({ modes: true, mode: "anc" }), []);
  assertEquals(Backends.ancLevelsOffered({}), []);
  assertEquals(Backends.ancLevelsOffered(null), []);
  // The Nothing bridge's four, in the panel's order rather than the line's.
  assertEquals(Backends.ancLevelsOffered({ ancLevels: ["adaptive", "high", "low", "mid"] }),
    ["low", "mid", "high", "adaptive"]);
  assertEquals(Backends.ancLevelsOffered({ ancLevels: ["high", "wind"] }), ["high"]);
  assertEquals(Backends.ancLevelsOffered({ ancLevels: [] }), []);
  assertEquals(Backends.ancLevelButtons(["high", "low"]).map((b) => b.label), ["Low", "High"]);
  assertEquals(Backends.ancLevelButtons([]), []);
});

Deno.test("ancLevelOf names only a strength the panel can draw", () => {
  assertEquals(Backends.ancLevelOf({ ancLevel: "high" }), "high");
  assertEquals(Backends.ancLevelOf({ ancLevel: "adaptive" }), "adaptive");
  assertEquals(Backends.ancLevelOf({ ancLevel: "loud" }), "");
  assertEquals(Backends.ancLevelOf({}), "");
  assertEquals(Backends.ancLevelOf(null), "");
});

// ---- The JBL bridge's record of which Fast Pair models answered.

Deno.test("parseSupport reads anything that is not an object as no record", () => {
  assertEquals(Backends.parseSupport(""), {});
  assertEquals(Backends.parseSupport("not json"), {});
  assertEquals(Backends.parseSupport("null"), {});
  assertEquals(Backends.parseSupport("[]"), {});
  assertEquals(Backends.parseSupport(null), {});
  assertEquals(Backends.parseSupport('{"71f20a":{"supported":true}}'), { "71f20a": { supported: true } });
});

Deno.test("supportVerdict only retires a model the bridge gave up on", () => {
  // Misses alone are still "not settled": the bridge writes supported:false
  // itself once it has heard silence often enough.
  assertEquals(Backends.supportVerdict({ "x": { misses: 2 } }, "x"), -1);
  assertEquals(Backends.supportVerdict({ "x": { supported: false } }, "x"), 0);
  assertEquals(Backends.supportVerdict({ "x": { supported: true } }, "x"), 1);
  // A malformed entry, an empty record and a missing model id all cost a probe.
  assertEquals(Backends.supportVerdict({ "x": true }, "x"), -1);
  assertEquals(Backends.supportVerdict({ "x": { supported: "true" } }, "x"), -1);
  assertEquals(Backends.supportVerdict({}, ""), -1);
  assertEquals(Backends.supportVerdict({}, "x"), -1);
  assertEquals(Backends.supportVerdict(null, "x"), -1);
});

// ---- Redmi Buds 5: its SDP record, and what must not be taken for it.

const REDMI_BUDS_5_UUIDS = [
  "00000001-0000-1000-8000-00805f9b34fb",
  "00001101-0000-1000-8000-00805f9b34fb",
  "00001108-0000-1000-8000-00805f9b34fb",
  "0000110b-0000-1000-8000-00805f9b34fb",
  "0000110c-0000-1000-8000-00805f9b34fb",
  "0000110e-0000-1000-8000-00805f9b34fb",
  "0000110f-0000-1000-8000-00805f9b34fb",
  "0000111e-0000-1000-8000-00805f9b34fb",
  "00001203-0000-1000-8000-00805f9b34fb",
  "0000fd2d-0000-1000-8000-00805f9b34fb",
  "66666666-6666-6666-6666-666666666666",
  "99999999-9999-9999-9999-999999999999",
  "df21fe2c-2515-4fdb-8886-f12c4d67927c",
];

Deno.test("the Redmi Buds 5 list is the one the capture printed", () => {
  assertEquals(captured("redmi-buds-5-bluetoothctl.txt"), REDMI_BUDS_5_UUIDS);
});

Deno.test("Redmi Buds 5 is picked by its name and the 0xFD2D service, with or without a Fast Pair address", () => {
  assertEquals(Backends.pick(REDMI_BUDS_5_UUIDS, "", "Redmi Buds 5"), "redmi");
  assertEquals(Backends.pick(REDMI_BUDS_5_UUIDS, BLE_ADDRESS, "Redmi Buds 5"), "redmi");
  assertEquals(Backends.brandOf("redmi"), "redmi");
  assertEquals(Backends.bridgeArgs("redmi", { address: "AA:BB:CC:00:00:01", name: "Redmi Buds 5" }),
    ["AA:BB:CC:00:00:01"]);
  assertEquals(Backends.isBle("redmi"), false);
});

Deno.test("only the Redmi row lets an empty Fast Pair stream give way to the bridge", () => {
  for (const row of Backends.ROWS) {
    assertEquals(Backends.fastPairWithoutBattery(row.id), row.id === "redmi", row.id);
  }
  assertEquals(Backends.fastPairWithoutBattery(""), false);
  assertEquals(Backends.fastPairWithoutBattery("nonesuch"), false);
});

Deno.test("neither the Redmi name nor its service alone is the claim", () => {
  const withoutService = REDMI_BUDS_5_UUIDS.filter((id) => !id.startsWith("0000fd2d"));
  assertEquals(Backends.pick(withoutService, "", "Redmi Buds 5"), "");
  assertEquals(Backends.pick(REDMI_BUDS_5_UUIDS, "", "Some Other Buds"), "");
  assertEquals(Backends.pick(REDMI_BUDS_5_UUIDS, "", ""), "");
});

Deno.test("the Redmi row takes no other device's claim, and every other claim still wins its own", () => {
  assertEquals(Backends.pick([SPP], "", "HUAWEI FreeBuds SE 2"), "huawei");
  // The Buds 5 Pro advertise CSR GAIA, and stay on the xiaomi row even if they
  // also list 0xFD2D.
  assertEquals(Backends.pick([rowOf("xiaomi").claim.uuids[0], UUID.redmi], "", "Redmi Buds 5"), "xiaomi");
  // A Fast Pair address alone, as for any other headset, is still the JBL row.
  assertEquals(Backends.pick([], BLE_ADDRESS, "Redmi Buds 5"), "jbl");
});

// ---- EarFun Air Pro 4: docs/captures/earfun-air-pro-4-bluetoothctl.txt.

const EARFUN_NAME = "EarFun Air Pro 4";

Deno.test("EarFun Air Pro 4 is picked by its name and SPP, with or without its Fast Pair address", () => {
  const ids = captured("earfun-air-pro-4-bluetoothctl.txt");
  assertEquals(ids.length, 12, "complete captured UUID list");
  for (const uuids of variants(ids)) {
    assertEquals(Backends.pick(uuids, "", EARFUN_NAME), "earfun");
    // The address its Fast Pair stream announced: the JBL row would take it.
    assertEquals(Backends.pick(uuids, "50:07:3D:6E:6E:42", EARFUN_NAME), "earfun");
  }
  assertEquals(Backends.brandOf("earfun"), "earfun");
  assertEquals(Backends.isBle("earfun"), false);
  assertEquals(Backends.bridgeArgs("earfun", { address: "AA:BB:CC:00:00:02", name: EARFUN_NAME }),
    ["AA:BB:CC:00:00:02", EARFUN_NAME]);
  assertEquals(Backends.fastPairWithoutBattery("earfun"), false);
  assertEquals(Backends.hasAmbientDial("earfun"), false);
  assertEquals(labelsOf(Backends.modeButtons(["off", "anc", "ambient"], "earfun")), ["Off", "ANC", "Ambient"]);
});

Deno.test("neither the EarFun name nor its record alone is the claim", () => {
  const ids = captured("earfun-air-pro-4-bluetoothctl.txt");
  const withoutSpp = ids.filter((id) => id !== SPP);
  assertEquals(Backends.pick(withoutSpp, "", EARFUN_NAME), "");
  assertEquals(Backends.pick(ids, "", ""), "");
  assertEquals(Backends.pick(ids, "", "EarFun Air Pro 3"), "");
  assertEquals(Backends.pick(ids, "", "EarFun Free Pro 3"), "");
  // Without the name, the record goes where it went before this row.
  assertEquals(Backends.pick(ids, "50:07:3D:6E:6E:42"), "jbl");
  assertEquals(Backends.pick(withoutSpp, "50:07:3D:6E:6E:42", EARFUN_NAME), "jbl");
});

Deno.test("the EarFun row takes no other device's claim", () => {
  for (const device of DEVICES) {
    assertEquals(Backends.pick(device.uuids, device.bleAddress, EARFUN_NAME), device.backend, device.model);
  }
  assertEquals(Backends.pick([SPP], "", "HUAWEI FreeBuds SE 2"), "huawei");
  assertEquals(Backends.pick([SPP], "", "TOZO NC9 Pro"), "tozo");
  assertEquals(Backends.pick(REDMI_BUDS_5_UUIDS, BLE_ADDRESS, "Redmi Buds 5"), "redmi");
});

// ---- Redmi Buds 3 Pro: docs/captures/redmi-buds-3-pro-bluetoothctl.txt.

const REDMI_BUDS_3_PRO = "Redmi Buds 3 Pro";

Deno.test("Redmi Buds 3 Pro are picked by their name and 0xFD2D, and the bridge gets the name", () => {
  const ids = captured("redmi-buds-3-pro-bluetoothctl.txt");
  assertEquals(ids.length, 12, "complete captured UUID list");
  for (const uuids of variants(ids)) assertEquals(Backends.pick(uuids, "", REDMI_BUDS_3_PRO), "redmi-buds-3-pro");
  assertEquals(Backends.brandOf("redmi-buds-3-pro"), "redmi");
  assertEquals(Backends.isBle("redmi-buds-3-pro"), false);
  assertEquals(Backends.bridgeArgs("redmi-buds-3-pro", { address: "AA:BB:CC:00:00:03", name: REDMI_BUDS_3_PRO }),
    ["AA:BB:CC:00:00:03", REDMI_BUDS_3_PRO]);
  assertEquals(Backends.fastPairWithoutBattery("redmi-buds-3-pro"), false);
  assertEquals(Backends.hasAmbientDial("redmi-buds-3-pro"), false);
  assertEquals(labelsOf(Backends.modeButtons(["off", "anc", "ambient"], "redmi-buds-3-pro")),
    ["Off", "ANC", "Transparency"]);
});

Deno.test("neither the Buds 3 Pro name nor their record alone is the claim", () => {
  const ids = captured("redmi-buds-3-pro-bluetoothctl.txt");
  const withoutService = ids.filter((id) => !id.startsWith("0000fd2d"));
  assertEquals(Backends.pick(withoutService, "", REDMI_BUDS_3_PRO), "");
  assertEquals(Backends.pick(ids, "", ""), "");
  assertEquals(Backends.pick(ids, "", "Redmi Buds 3"), "");
  assertEquals(Backends.pick(ids, "", "Redmi Buds 5"), "redmi");
});

Deno.test("the Buds 3 Pro row takes no other device's claim, and the Buds 5 keep theirs", () => {
  for (const device of DEVICES) {
    assertEquals(Backends.pick(device.uuids, device.bleAddress, REDMI_BUDS_3_PRO), device.backend, device.model);
  }
  assertEquals(Backends.pick(REDMI_BUDS_5_UUIDS, "", "Redmi Buds 5"), "redmi");
  assertEquals(Backends.bridgeArgs("redmi", { address: "AA:BB:CC:00:00:01", name: "Redmi Buds 5" }),
    ["AA:BB:CC:00:00:01"]);
  // The Buds 5 record with this name: the same service, so the row that names it.
  assertEquals(Backends.pick(REDMI_BUDS_5_UUIDS, "", REDMI_BUDS_3_PRO), "redmi-buds-3-pro");
});

// ---- JBL Tour Pro 3: docs/captures/jbl-tour-pro-3-bluetoothctl.txt.

const TOUR_PRO_3 = "JBL Tour Pro 3";

Deno.test("JBL Tour Pro 3 is picked by its name and the Airoha RACE UUID, with or without its Fast Pair address", () => {
  const ids = captured("jbl-tour-pro-3-bluetoothctl.txt");
  assertEquals(ids.length, 15, "complete captured UUID list");
  for (const uuids of variants(ids)) {
    assertEquals(Backends.pick(uuids, "", TOUR_PRO_3), "airoha");
    // The address its Fast Pair stream announced: the JBL row would take it.
    assertEquals(Backends.pick(uuids, "CE:6D:DD:59:9A:F2", TOUR_PRO_3), "airoha");
  }
  assertEquals(Backends.brandOf("airoha"), "airoha");
  assertEquals(Backends.isBle("airoha"), false);
  assertEquals(Backends.bridgeArgs("airoha", { address: "AA:BB:CC:00:00:04", name: TOUR_PRO_3 }),
    ["AA:BB:CC:00:00:04", TOUR_PRO_3]);
  assertEquals(Backends.fastPairWithoutBattery("airoha"), false);
  assertEquals(Backends.hasAmbientDial("airoha"), true);
  assertEquals(Backends.ambientRange("airoha"), { min: 1, max: 7, voice: "", command: "", key: "" });
  assertEquals(Backends.strengthRange("airoha"), { min: 1, max: 7 });
});

Deno.test("neither the Tour Pro 3 name nor the Airoha UUID alone is the claim", () => {
  const ids = captured("jbl-tour-pro-3-bluetoothctl.txt");
  const withoutRace = ids.filter((id) => id !== UUID.airoha);
  assertEquals(withoutRace.length, ids.length - 1);
  assertEquals(Backends.pick(withoutRace, "", TOUR_PRO_3), "");
  assertEquals(Backends.pick(ids, "", ""), "");
  assertEquals(Backends.pick(ids, "", "JBL Tour Pro 2"), "");
  // Without the name, the record goes where it went before this row.
  assertEquals(Backends.pick(ids, "CE:6D:DD:59:9A:F2"), "jbl");
  assertEquals(Backends.pick(withoutRace, "CE:6D:DD:59:9A:F2", TOUR_PRO_3), "jbl");
});

Deno.test("the Tour Pro 3 row takes no other device's claim", () => {
  for (const device of DEVICES) {
    assertEquals(Backends.pick(device.uuids, device.bleAddress, TOUR_PRO_3), device.backend, device.model);
  }
});
