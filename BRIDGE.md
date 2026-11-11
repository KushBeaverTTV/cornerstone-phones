# Bridges

A bridge is the process that holds one link to one headset and reports what
it says. There is one program for all of them:

```
bridges/cornerstone-bridge <brand> <arguments>
bridges/cornerstone-bridge <brand> --help
```

and one Fast Pair reader for every headset at once:

```
bridges/cornerstone-fastpair <address>...
```

`Headset.qml` starts the bridge its device needs (the row in
`lib/backends.js` says which brand and which arguments), writes commands to
its stdin, reads its stdout line by line, and acts on how it exits.

## Layout

| path | what it is |
|:--|:--|
| `bridges/core/headset.py` | `Headset`, the base class a brand subclasses; exit codes; argument helpers |
| `bridges/core/links.py` | what a brand can ask to have opened: `Profile`, `Rfcomm`, `Child`, `Gatt` |
| `bridges/core/bluez.py` | those links, live, on BlueZ and GLib |
| `bridges/core/runtime.py` | the loop: arguments, stdin, stdout, timers, signals, tracing |
| `bridges/core/codec.py` | the checksums the protocols use |
| `bridges/brands/<brand>.py` | one protocol each; defines `HEADSET` |
| `bridges/fastpair.py` | the Fast Pair Message Stream reader |

A brand module never touches D-Bus, GLib, sockets or stdout. It describes its
link, says what to send when the link opens, reads what arrives, and answers
commands, all through the methods on `Headset`. That is what makes every
brand testable with no Bluetooth at all (`tests/harness.py`).

## Links

- **Profile**: an RFCOMM channel BlueZ connects through an `org.bluez.Profile1`
  registered at `/io/cornerstone/phones/<brand>`, role client, no
  authorisation. `ConnectProfile` is asynchronous with a long timeout, and a
  failed first attempt (usually `br-connection-busy`) is retried.
- **Rfcomm**: a channel dialled by number. Several numbers can be tried in
  order, and each can be confirmed by a probe before it is trusted.
- **Child**: a helper program spoken to by lines (JBL's `btgatt-client`).
- **Gatt**: a BLE characteristic pair through BlueZ (TOZO).

## stdout: one JSON object per line

Each line is the headset's whole state as the bridge knows it, written when
something changed. Keys:

| key | type | meaning |
|:--|:--|:--|
| `modes` | bool | always present. `true`: the device answered. `false`: something is wrong, and `error` says what |
| `error` | string | with `modes: false`, one sentence for the panel |
| `mode` | string | `off`, `anc`, `ambient`, `talkthru`, or one of a row's `extraModes` (TOZO: `wind`, `leisure`, `adaptive`) |
| `available` | list | the modes this device has. Absent means the four base modes (JBL's fixed slots). Empty means none: a battery-only device |
| `level` | int | the Ambient dial, in the row's `ambient` range |
| `voice` | bool | the switch beside the dial (Sony: focus on voice; Soundcore: wind noise reduction) |
| `strength` | int | numeric ANC strength while in ANC, in the row's `strength` range |
| `ancLevel` | string | named ANC grade: `low`, `mid`, `high`, `adaptive`, with `ancLevels` listing those offered |
| `latency` | bool | the low-latency switch |
| `worn` | bool | a wear sensor, sent only once the device has said |
| `battery` | object | `left`, `right`, `case`, `headset` (0-100, only parts that report), `charging` (list of parts), `caseStale` (the case value is the last one seen) |

Keys a line leaves out keep their last value in the shell, but every bridge
here sends its whole state. A bridge reports what the device said, never what
was asked for: after a set, the device's answer is the truth.

## stdin: one command per line

```
set <mode>
level <n>              Ambient dial
level low|mid|high|adaptive   named ANC grade (turns ANC on at it)
strength <n>           numeric ANC strength, in ANC only
voice on|off           Sony's switch beside the dial
wind on|off            Soundcore's switch beside the dial
latency on|off
```

A command for something the device does not have is ignored, and nothing is
sent before the device has answered at least once.

## Exit codes

| code | meaning | what the shell does |
|:--|:--|:--|
| 0 | asked to stop (signal, or stdin closed) | starts again after the current pause |
| 1 | the link failed, says nothing about the device | waits longer each time (10 s doubling to 5 min) |
| 3 | linked, asked, never answered: the device lacks this | parks it for the session (by address, or by Fast Pair model on JBL) |
| 4 | bad arguments or something missing on this machine | waits longer each time, error shown |

An exception inside a brand's code ends the run with exit 1 and the error on
stdout, rather than leaving a bridge that holds the channel and says nothing.

## Lifecycle

- One process holds a device's link; the panel talks to it through the pipe.
- The bridge dies with the shell (`PR_SET_PDEATHSIG`), because an orphan would
  keep the channel and every later bridge would get nothing.
- A closed stdout is the shell going away: exit 0 quietly.
- Nothing is written inside the plugin folder (`sys.dont_write_bytecode`):
  a new file there makes the shell reload the plugin.

## Tracing a new device

```
tools/trace <brand> <arguments>
```

runs a bridge in the terminal with `CORNERSTONE_TRACE=1`: every frame in and
out goes to stderr as hex, and lines typed are sent as commands.

## Tests

`tests/test_<brand>.py` plays each pin in `tests/pins/<brand>/` against the
brand module on a fake host: what the device said, what the bridge wrote and
printed, step by step. A pin is a model owner's frozen session and is never
edited; `tools/check` refuses a branch that changes one. Pins recorded before
the rewrite still name the old `<brand>-bridge` script in their `bridge`
field; they pass unchanged against the new modules.
