"""JBL Tour Pro 3 over Airoha RACE: Off / ANC / Ambient / TalkThru, levels, wear.

    cornerstone-bridge airoha <classic-address> <reported-name>

The earbuds' record lists the vendor UUID 00000000-0000-0000-0099-aabbccddeeff,
an RFCOMM channel speaking Airoha's RACE framing. Every frame sent here is one
the JBL Headphones app sent, from a phone-side HCI snoop log, replayed from a
PC; see PROTOCOL.md, "JBL Tour Pro 3", and docs/captures/jbl-tour-pro-3*.txt.

    05 <type> <length u16 LE> <command u16 LE> <payload>
    type    5a command · 5b reply · 5c / 5d notification
    length  counts the command and the payload

    get mode    05 5a 04 00 01 09 05 00
    reply       05 5b 09 00 01 09 05 00 00 <mode> <level> ..
    event       05 5d 08 00 01 09 05 00 00 <mode> <level> ..   (any change, unprompted)
                mode 00 off, 01 anc, 0a ambient, 09 talkthru
                level: the mode's own level, 1-7; read in Ambient and ANC
    set off     05 5a 04 00 06 0e 00 0b
    set anc     05 5a 06 00 06 0e 00 0a 01 00
    set ambient 05 5a 06 00 06 0e 00 0a 0a 04
    set talkthru 05 5a 06 00 06 0e 00 0a 09 04
    set level   05 5a 06 00 06 0e 00 1e <level> 00   (acts on the mode the earbuds are in)
    get ambient 05 5a 04 00 01 09 07 00 -> 05 5b 07 00 01 09 07 00 00 <level> 00
    wear        05 5c 05 00 0c 00 00 <ear> <state>    ear 00/01, state 01 in, 00 out

A set is acknowledged by an echo that is not read; the event after it is.
The earbuds do not announce their mode on connect, so it is asked once, and
the first answer is what makes the row live: nothing is printed or sent before
it. The 1e frame is the Ambient level in Ambient and the ANC strength in ANC,
so `level <n>` is written only in Ambient and `strength <n>` only in ANC, and
each is confirmed by asking 0.3 s later (the level get, or the mode get whose
answer carries the strength), never printed for having been sent.

Wear is announced per ear and never on connect: `worn` is absent until an ear
has spoken, true while any ear is in, false once both are known and out.
No battery: the Fast Pair stream carries it.
"""
from core.headset import SILENT, Headset, SetupError, address, choice, number
from core.links import Profile

UUID = "00000000-0000-0000-0099-aabbccddeeff"

# Exact reported names. The same UUID on another maker's earbuds says nothing
# about what these frames mean there.
MODELS = {"JBL Tour Pro 3": {}}

AVAILABLE = ["off", "anc", "ambient", "talkthru"]

START = 0x05
COMMAND, REPLY, NOTICE, EVENT = 0x5A, 0x5B, 0x5C, 0x5D
TYPES = (COMMAND, REPLY, NOTICE, EVENT)
HEADER = 4
MIN_LENGTH = 2           # the command id alone
MAX_LENGTH = 512         # the largest seen is 0x3c; more is line noise

GET_MODE = bytes.fromhex("055a040001090500")
GET_AMBIENT_LEVEL = bytes.fromhex("055a040001090700")
SET_MODE = {
    "off":      bytes.fromhex("055a0400060e000b"),
    "anc":      bytes.fromhex("055a0600060e000a0100"),
    "ambient":  bytes.fromhex("055a0600060e000a0a04"),
    "talkthru": bytes.fromhex("055a0600060e000a0904"),
}
MODE_COMMAND = b"\x01\x09"
MODE_QUERY = b"\x05\x00\x00"           # payload ahead of the mode byte
LEVEL_ANSWER = bytes.fromhex("055b07000109070000")   # ahead of the level byte
WEAR_NOTICE = bytes.fromhex("055c05000c0000")
MODE_BYTES = {0x00: "off", 0x01: "anc", 0x0A: "ambient", 0x09: "talkthru"}
LEVELS = (1, 7)

ANSWER_MS = 5000
READBACK_MS = 300


def set_level(level):
    return bytes.fromhex("055a0600060e001e") + bytes([level, 0])


def frames(buffer):
    """Whole RACE frames off the front of a bytearray.

    Anything that cannot start a frame (not 05, an unknown type, a length
    outside what a frame can be) is skipped one byte at a time, so noise is
    never waited on and a bad length cannot swallow the frame after it.
    """
    out = []
    while buffer:
        if buffer[0] != START:
            del buffer[0]
            continue
        if len(buffer) < 2:
            break
        if buffer[1] not in TYPES:
            del buffer[0]
            continue
        if len(buffer) < HEADER:
            break
        length = buffer[2] | buffer[3] << 8
        if not MIN_LENGTH <= length <= MAX_LENGTH:
            del buffer[0]
            continue
        if len(buffer) < HEADER + length:
            break
        out.append(bytes(buffer[:HEADER + length]))
        del buffer[:HEADER + length]
    return out


def in_range(value):
    return value if LEVELS[0] <= value <= LEVELS[1] else None


def read_mode(frame):
    """(mode, level or None) from a mode reply or event, else None.

    Only a 5b or 5d for command 0901 whose payload opens 05 00 00 counts: an
    echo of our own get, a write acknowledgement (0e06) and every other
    notification say nothing about the mode.
    """
    if len(frame) < 10 or frame[1] not in (REPLY, EVENT):
        return None
    if frame[4:6] != MODE_COMMAND or frame[6:9] != MODE_QUERY:
        return None
    mode = MODE_BYTES.get(frame[9])
    if mode is None:
        return None
    return mode, (in_range(frame[10]) if len(frame) > 10 else None)


def read_ambient_level(frame):
    """The level from the answer to the Ambient level get, that shape only."""
    if len(frame) != 11 or frame[:9] != LEVEL_ANSWER:
        return None
    return in_range(frame[9])


def read_wear(frame):
    """(ear, in the ear) from a wear notice, that exact shape only."""
    if len(frame) != 9 or frame[:7] != WEAR_NOTICE:
        return None
    ear, state = frame[7], frame[8]
    if ear not in (0, 1) or state not in (0, 1):
        return None
    return ear, state == 1


class Airoha(Headset):
    brand = "airoha"
    label = "Airoha"
    usage = "<classic-address> <reported-name>"

    @classmethod
    def parse(cls, args):
        if len(args) != 2:
            raise SetupError("usage: cornerstone-bridge airoha " + cls.usage)
        if args[1] not in MODELS:
            raise SetupError("no Airoha model row for %r" % args[1])
        return {"address": address(args[0])}

    def __init__(self, host, address):
        super().__init__(host)
        self.address = address
        self.buffer = bytearray()
        self.mode = None
        self.level = None          # Ambient's
        self.strength = None       # ANC's
        self.ears = {}
        self.deadline = None
        self.readback = None

    def links(self):
        return [Profile(self.address, UUID)]

    def opened(self, link):
        self.send(GET_MODE)
        self.deadline = self.later(ANSWER_MS, self.give_up_if_silent)

    def give_up_if_silent(self):
        self.deadline = None
        if self.mode is None:
            self.fail(SILENT, "the earbuds did not answer for their mode")

    # ---- what arrives

    def received(self, data, link):
        self.buffer += data
        for frame in frames(self.buffer):
            if self.finished:
                return
            self.frame(frame)

    def frame(self, frame):
        wear = read_wear(frame)
        if wear is not None:
            ear, worn = wear
            self.ears[ear] = worn
            self.publish()
            return
        level = read_ambient_level(frame)
        if level is not None:
            self.level = level
            self.publish()
            return
        reading = read_mode(frame)
        if reading is None:
            return
        mode, level = reading
        if self.mode is None:
            self.cancel(self.deadline)
            self.deadline = None
        self.mode = mode
        if level is not None and mode == "ambient":
            self.level = level
        elif level is not None and mode == "anc":
            self.strength = level
        self.publish()

    def worn(self):
        if any(self.ears.values()):
            return True
        if len(self.ears) == 2:
            return False
        return None

    def publish(self):
        if self.mode is None:
            return
        line = {"modes": True, "mode": self.mode, "available": list(AVAILABLE)}
        # Each figure only on its own mode's line: on another it would say
        # more than the earbuds did.
        if self.mode == "ambient" and self.level is not None:
            line["level"] = self.level
        if self.mode == "anc" and self.strength is not None:
            line["strength"] = self.strength
        worn = self.worn()
        if worn is not None:
            line["worn"] = worn
        self.report(line)

    # ---- the panel

    def command(self, verb, arg):
        if self.mode is None:
            return
        if verb == "set":
            mode = choice(arg, SET_MODE)
            if mode is not None:
                self.send(SET_MODE[mode])
        elif verb == "level" and self.mode == "ambient":
            self.set_level(number(arg, *LEVELS), GET_AMBIENT_LEVEL)
        elif verb == "strength" and self.mode == "anc":
            self.set_level(number(arg, *LEVELS), GET_MODE)

    def set_level(self, level, confirm):
        if level is None:
            return
        self.send(set_level(level))
        # The echo carries no level: ask, once, after the last of a burst.
        self.cancel(self.readback)
        self.readback = self.later(READBACK_MS, self.read_back, confirm)

    def read_back(self, frame):
        self.readback = None
        self.send(frame)


HEADSET = Airoha
