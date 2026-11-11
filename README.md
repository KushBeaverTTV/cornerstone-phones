# Cornerstone Phones

Headphone battery and noise-cancelling control in the Omarchy bar, part of the
Cornerstone desktop by Kush Beaver Productionz.

- One icon per connected set, filling with its battery: the left half for the
  left earbud, the right half for the right one, both for a headset.
- Click it for left, right and case levels, and Off / ANC / Ambient on
  headphones that allow it, with the extra controls some models have (an
  Ambient level, an ANC strength, low latency, more modes).
- Follows changes made on the headphones themselves, warns once when a
  battery runs low, and pauses media when the headphones disconnect or come
  off your ears.

## What it talks to

Each brand has its own protocol, spoken over the headphones' own Bluetooth
channel by a small module in `bridges/brands/`:

| brand | battery | modes |
|:--|:--|:--|
| Motorola Moto Buds+ | left, right, case | Off, ANC, Ambient |
| Sony (MDR v1 and v2) | Fast Pair or BlueZ | Off, ANC, Ambient, Ambient level, focus on voice |
| Samsung Galaxy Buds2 | left, right, case | Off, ANC, Ambient |
| Nothing and CMF | left, right, case | Off, ANC grades, Transparency, low latency |
| Soundcore | Fast Pair | Off, ANC, Ambient, level, wind noise reduction |
| Xiaomi Buds 5 Pro | Fast Pair | Off, ANC, Ambient |
| Redmi Buds 5, Buds 3 Pro | left, right, case | Off, ANC, Transparency |
| OPPO Enco Air3 Pro | left, right, case | Off, ANC, Transparency |
| Bose QC45, QC35 | headset | the modes each model has |
| TOZO NC9 Pro | left, right, case | six modes |
| HUAWEI FreeBuds SE 2 | left, right, case | none (battery only) |
| EarFun Air Pro 4 | Fast Pair | Off, ANC, Transparency |
| JBL Tour Pro 3 | Fast Pair | Off, ANC, Ambient, TalkThru, level, strength |
| other JBL earbuds | Fast Pair | Off, ANC, Ambient, TalkThru |

Anything else with Google Fast Pair gets left, right and case; anything with
a BlueZ battery figure gets that one number.

## For scripts

```
omarchy-shell cornerstone-phones status
omarchy-shell cornerstone-phones modeFor moto
omarchy-shell cornerstone-phones setModeFor anc moto
```

Every question has a plain form (about the first icon) and a `...For` form
that takes part of a name, an address or a brand. `Service.qml` lists them.

## Layout

| path | what it is |
|:--|:--|
| `Service.qml` | the session's one copy: headsets, the Fast Pair reader, caches, notifications, IPC |
| `Headset.qml` | one followed device: its bridge, its battery, its warning |
| `Panel.qml`, `parts/` | the bar icons and the popup |
| `lib/` | pure JavaScript: device picking, the brand table, battery logic, text |
| `bridges/` | the bridge core, one module per brand, the Fast Pair reader (see BRIDGE.md) |
| `tests/` | Python tests with frozen sessions per model (`tests/pins`), JavaScript tests in `tests/js` |
| `tools/check` | every test and check in one command |
| `tools/trace` | run a bridge by hand with every frame shown, for a new device |
| `tools/live-check` | exercise a connected headset's modes through the running bar |

Protocol notes for each brand are in `PROTOCOL.md` and `docs/captures`.

## Credits

Cornerstone Phones began as a fork of Omaphones by Jacek Becela (MIT, see
`LICENSE`), and was then rewritten as its own code. The protocol knowledge in
`PROTOCOL.md`, the captures and the frozen test sessions come from that
project and the owners who recorded their headphones for it.
