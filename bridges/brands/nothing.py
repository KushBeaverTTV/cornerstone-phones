"""Nothing and CMF (NT Link): battery, Off / ANC / Ambient, ANC strength, low latency.

    cornerstone-bridge nothing <classic-address> [<reported-name>]

Nothing serves the Nothing X protocol on its own RFCOMM channel and
advertises the NT Link UUID (aeac4a03-dff5-498f-843a-34487cf133eb) in its SDP
record, which is how the shell picks this brand. The channel is dialled by
number. Which number depends on the model, and the reported name picks it
before anything is dialled (CHANNELS below): the Nothing earbuds and
Headphone (1) keep 15, CMF Headphone Pro 28, CMF Buds 2 16. A name with no
row tries 15 and then 28; a caller that passes no name keeps 15 alone.

Seen on an Ear (a) (pull request #2), a CMF Headphone Pro
(docs/captures/nothing-headphone-pro.txt) and a CMF Buds 2
(docs/captures/nothing-cmf-buds-2.txt); PROTOCOL.md has the long version.
Every frame, both ways:

    55  ctrl u16 LE  cmd u16 LE  length u16 LE  op u8  payload  [crc u16 LE]

    ctrl    0x0160 on everything we send; bit 0x20 says a CRC follows.
            Some events come as 0x0300, with no CRC
    cmd     low byte the command, high byte the direction:
            C0 get, F0 set, 40 answer, 70 ack, E0 unsolicited
    op      an operation id the device echoes; we always send 1
    crc     CRC-16/MODBUS over every byte before it

    06      device info. Asked first: a fresh session may ignore what follows
            until it has been. The answer is not read, it only unlocks the rest
    07      battery get; answered 40 07, announced E0 01. A count, then
            <component> <level> pairs: 2 left, 3 right, 4 case, 6 the one
            battery of a headphone. Bit 7 of a level is charging
    1E      noise control get; answered 40 1E, announced E0 03.
            payload[1] is the mode: 1 high, 2 mid, 3 low, 4 adaptive (all ANC),
            5 off, 7 transparency. Newer firmware adds a (02, strength, 00)
            triplet, which then names the strength
    0F      noise control set, payload 01 <mode> 00; acked 70 0F
    41      low latency get; answered 01 on, anything else off
    40      low latency set, payload 01 on, 02 off; acked 70 40

The panel's words are not Nothing's. Transparency is `ambient`, and the four
ANC strengths are one `anc` mode with an `ancLevel`; `set anc` asks for the
strength last seen (adaptive when none has been), because the device keeps the
strength in the mode byte and has no plain "on".

A set is acked, not answered, so the state is asked again shortly after. Touch
controls are not reliably announced on this channel, so the mode is also
polled, and the battery on every fifth poll.

The case reports only while its lid is open. Its last reading is kept on
disk for six hours ($XDG_STATE_HOME/cornerstone-phones/nothing-case.json, the
file the old bridge kept) and shown with `caseStale` once the case has gone
quiet, so the panel can dim it rather than drop it.
"""
import json
import os
import struct
import time

from core import codec
from core.headset import SILENT, Headset, SetupError, address, choice, switch
from core.links import Rfcomm

# ---- Which channel, by the reported name: case-insensitive, outer space
#      and the optional "Nothing " prefix ignored. A refusal never moves a
#      known model to another channel; the retries stay on its own.

CHANNELS = {
    "ear (a)": (15,),
    "ear": (15,),
    "ear (2)": (15,),
    "ear (stick)": (15,),
    "headphone (1)": (15,),
    "cmf headphone pro": (28,),
    "cmf buds 2": (16,),
    "buds 2": (16,),
}
UNKNOWN = (15, 28)
NAMELESS = (15,)


def channels_for(name):
    key = (name or "").strip().casefold()
    if not key:
        return NAMELESS
    if key.startswith("nothing "):
        key = key[len("nothing "):].strip()
    return CHANNELS.get(key, UNKNOWN)


# ---- Framing

SOF = 0x55
CTRL = 0x0160
CRC_BIT = 0x20
HEADER = 8          # SOF, ctrl, cmd, length, op

GET, SET, ANSWER, ACK, EVENT = 0xC0, 0xF0, 0x40, 0x70, 0xE0

DEVICE_INFO = 0x06
BATTERY = 0x07
BATTERY_EVENT = 0x01
NOISE_GET = 0x1E
NOISE_SET = 0x0F
NOISE_EVENT = 0x03
LATENCY_GET = 0x41
LATENCY_SET = 0x40

# ---- The panel's vocabulary

AVAILABLE = ["off", "anc", "ambient"]
LEVELS = ["low", "mid", "high", "adaptive"]       # weakest first, as the panel orders them
DEFAULT_LEVEL = "adaptive"
LEVEL_BYTES = {"high": 0x01, "mid": 0x02, "low": 0x03, "adaptive": 0x04}
LEVEL_NAMES = {byte: name for name, byte in LEVEL_BYTES.items()}
OFF, TRANSPARENCY = 0x05, 0x07
MODE_BYTES = {"off": OFF, "ambient": TRANSPARENCY}
STRENGTH_KIND = 0x02
COMPONENTS = {2: "left", 3: "right", 4: "case", 6: "headset"}
CHARGING_BIT = 0x80

# ---- Timing

INFO_MS = 1500          # the device-info answer gets this long before the real gets go anyway
SILENCE_MS = 6500       # from the channel opening: no mode by now, the device is silent
POLL_MS = 3000          # the mode is asked again this often
BATTERY_EVERY = 5       # and the battery on every fifth poll
AFTER_SET_MS = 800      # a set nobody acked is read back after this
AFTER_ACK_MS = 400      # an acked one sooner


def frame(command, direction, payload=b"", op=1):
    body = struct.pack("<BHBBHB", SOF, CTRL, command & 0xFF, direction & 0xFF,
                       len(payload), op & 0xFF) + bytes(payload)
    return body + struct.pack("<H", codec.crc16_modbus(body))


def unframe(buffer):
    """Whole frames off the front of a bytearray, as [(command, direction, payload)].

    Bytes before a 55 are noise and go. A frame whose CRC fails is dropped
    whole and the search goes on after it.
    """
    out = []
    while True:
        start = buffer.find(SOF)
        if start < 0:
            buffer.clear()
            return out
        del buffer[:start]
        if len(buffer) < HEADER:
            return out
        ctrl, command, direction, size = struct.unpack_from("<HBBH", buffer, 1)
        trailer = 2 if ctrl & CRC_BIT else 0
        total = HEADER + size + trailer
        if len(buffer) < total:
            return out
        raw = bytes(buffer[:total])
        del buffer[:total]
        if trailer and struct.unpack_from("<H", raw, HEADER + size)[0] != codec.crc16_modbus(raw[:HEADER + size]):
            continue
        out.append((command, direction, raw[HEADER:HEADER + size]))


def noise_state(payload):
    """(mode, level) in the panel's words. Either may be None: a mode byte we
    do not know is not forced into a mode, and off or ambient without a
    strength triplet says nothing about the strength."""
    if len(payload) < 2:
        return None, None
    level = None
    for at in range(3, len(payload) - 1, 3):
        if payload[at] == STRENGTH_KIND and payload[at + 1] in LEVEL_NAMES:
            level = LEVEL_NAMES[payload[at + 1]]
    byte = payload[1]
    if byte == TRANSPARENCY:
        return "ambient", level
    if byte in (0x00, OFF):
        return "off", level
    if byte in LEVEL_NAMES:
        return "anc", level or LEVEL_NAMES[byte]
    return None, level


def battery_state(payload):
    """({"left": 85, ...}, [charging part names]) for the components present.
    An unknown component, or a level over 100, is skipped, not clamped."""
    levels, charging = {}, []
    if not payload:
        return levels, charging
    for index in range(payload[0]):
        at = 1 + 2 * index
        if at + 1 >= len(payload):
            break
        part = COMPONENTS.get(payload[at])
        value = payload[at + 1] & 0x7F
        if part is None or value > 100:
            continue
        levels[part] = value
        if payload[at + 1] & CHARGING_BIT:
            charging.append(part)
    return levels, charging


# ---- The case's last reading, kept across runs

def state_directory():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "cornerstone-phones")


class CaseMemory:
    """The case's last level per address, in one small JSON file.

        {"3C:B0:ED:AF:7C:30": {"level": 85, "saved": 1789000000}}

    `saved` is wall-clock seconds, because the reading has to outlive the
    process. A reading older than six hours is not recalled. A file that
    cannot be read or written costs a dimmed row, never the run.
    """

    MAX_AGE_S = 6 * 3600
    # The case repeats its level every few seconds while open; the file is
    # rewritten for a new level, or to refresh the stamp after this long.
    REFRESH_S = 600

    def __init__(self, path=None, clock=time.time):
        self.path = path or os.path.join(state_directory(), "nothing-case.json")
        self.clock = clock
        self._written = {}

    def _load(self):
        try:
            with open(self.path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def recall(self, key):
        entry = self._load().get(key.upper())
        if not isinstance(entry, dict):
            return None
        level, saved = entry.get("level"), entry.get("saved")
        if type(level) is not int or not isinstance(saved, (int, float)) or isinstance(saved, bool):
            return None
        if not 0 <= level <= 100 or self.clock() - saved > self.MAX_AGE_S:
            return None
        return level

    def remember(self, key, level):
        key = key.upper()
        stamp = self.clock()
        last = self._written.get(key)
        if last is not None and last[0] == level and stamp - last[1] < self.REFRESH_S:
            return
        data = self._load()
        data[key] = {"level": level, "saved": int(stamp)}
        temporary = "%s.%d.tmp" % (self.path, os.getpid())
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(temporary, "w") as handle:
                json.dump(data, handle, separators=(",", ":"), sort_keys=True)
            os.replace(temporary, self.path)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return
        self._written[key] = (level, stamp)


class Nothing(Headset):
    brand = "nothing"
    label = "Nothing"
    usage = "<classic-address> [<reported-name>]"

    @classmethod
    def parse(cls, args):
        if not 1 <= len(args) <= 2:
            raise SetupError("usage: cornerstone-bridge nothing " + cls.usage)
        name = args[1] if len(args) == 2 else ""
        return {"address": address(args[0].strip()), "channels": channels_for(name)}

    def __init__(self, host, address, channels=NAMELESS, case_memory=None):
        super().__init__(host)
        self.address = address
        self.channels = tuple(channels)
        self.memory = case_memory if case_memory is not None else CaseMemory()
        self.buffer = bytearray()

        self.mode = None
        self.level = None
        self.latency = None
        self.levels = {}
        self.charging = []
        self.case = self.memory.recall(address)
        self.case_live = False

        self.asked = False
        self.polls = 0
        self.info_wait = None
        self.silence = None
        self.readback_timer = None

    def links(self):
        return [Rfcomm(self.address, self.channels, attempts=6, retry_ms=1500, timeout_s=5.0)]

    # ---- asking

    def get(self, command):
        self.send(frame(command, GET))

    def opened(self, link):
        self.get(DEVICE_INFO)
        self.info_wait = self.later(INFO_MS, self.ask)
        self.silence = self.later(SILENCE_MS, self.give_up_if_silent)

    def ask(self):
        """The real gets, once: on the device-info answer, or when it is late."""
        if self.asked:
            return
        self.asked = True
        self.cancel(self.info_wait)
        self.info_wait = None
        self.get(BATTERY)
        self.get(NOISE_GET)
        self.get(LATENCY_GET)
        self.every(POLL_MS, self.poll)

    def poll(self):
        self.polls += 1
        self.get(NOISE_GET)
        if self.polls % BATTERY_EVERY == 0:
            self.get(BATTERY)

    def read_back(self, ms):
        """Ask for the state again in ms; a later call replaces an earlier one."""
        self.cancel(self.readback_timer)
        self.readback_timer = self.later(ms, self.readback)

    def readback(self):
        self.readback_timer = None
        self.get(NOISE_GET)
        self.get(LATENCY_GET)

    def give_up_if_silent(self):
        if self.mode is None:
            self.fail(SILENT, "the device did not answer the noise-control query")

    # ---- hearing

    def received(self, data, link):
        self.buffer += data
        for command, direction, payload in unframe(self.buffer):
            if self.finished:
                return
            if direction in (ANSWER, ACK, EVENT):
                self.heard(command, direction, payload)

    def heard(self, command, direction, payload):
        if command == DEVICE_INFO:
            self.ask()
        elif command in (NOISE_GET, NOISE_EVENT) and direction != ACK:
            mode, level = noise_state(payload)
            if mode is not None:
                if self.mode is None:
                    self.cancel(self.silence)
                self.mode = mode
                self.level = level or self.level
                self.publish()
        elif command in (BATTERY, BATTERY_EVENT) and direction != ACK:
            levels, charging = battery_state(payload)
            if levels:
                self.battery(levels, charging)
        elif command == LATENCY_GET and direction == ANSWER:
            self.latency = payload[:1] == b"\x01"
            self.publish()
        elif command in (NOISE_SET, LATENCY_SET):
            # The ack carries no state; what the device now is, a read says.
            self.read_back(AFTER_ACK_MS)

    def battery(self, levels, charging):
        self.levels, self.charging = levels, charging
        if "case" in levels:
            self.case, self.case_live = levels["case"], True
            self.memory.remember(self.address, levels["case"])
        else:
            self.case_live = False
        self.publish()

    def publish(self):
        if self.mode is None:
            return
        battery = dict(self.levels)
        charging = list(self.charging)
        if self.case is not None and "case" not in battery:
            battery["case"] = self.case
        battery["charging"] = charging
        battery["caseStale"] = "case" in battery and not self.case_live
        line = {
            "modes": True,
            "mode": self.mode,
            "available": list(AVAILABLE),
            "ancLevel": self.level or DEFAULT_LEVEL,
            "ancLevels": list(LEVELS),
            "battery": battery,
        }
        if self.latency is not None:
            line["latency"] = self.latency
        self.report(line)

    # ---- the panel

    def command(self, verb, arg):
        if self.mode is None:
            return
        if verb == "set":
            mode = choice(arg, AVAILABLE)
            if mode is None:
                return
            byte = LEVEL_BYTES[self.level or DEFAULT_LEVEL] if mode == "anc" else MODE_BYTES[mode]
            self.send(frame(NOISE_SET, SET, bytes([0x01, byte, 0x00])))
        elif verb == "level":
            level = choice(arg, LEVEL_BYTES)
            if level is None:
                return
            # Kept at once: a later report of a mode without a strength
            # triplet shows the strength asked for (tests/pins/nothing/ear-a.json).
            self.level = level
            self.send(frame(NOISE_SET, SET, bytes([0x01, LEVEL_BYTES[level], 0x00])))
        elif verb == "latency":
            on = switch(arg)
            if on is None or self.latency is None:
                return
            self.send(frame(LATENCY_SET, SET, b"\x01" if on else b"\x02"))
        else:
            return
        self.read_back(AFTER_SET_MS)


HEADSET = Nothing
