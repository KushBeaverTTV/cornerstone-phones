"""Bose BMAP headsets (QC45, QC35): battery and the listening mode.

    cornerstone-bridge bose <classic-address>

BMAP rides on an RFCOMM channel the headset's service record never names, so
the channel is dialled by number: 8 (confirmed on a QC45 and a QC35), then 2
and 9 (from an external implementation, untried on either). A channel that
merely opens proves nothing, several accept and stay silent, so each one is
asked GET [0.1] and counts only once it answers with a version string. The
headsets ignore everything until they have seen that GET, which is why the
probe and the init are the same frame. A run where channels opened but none
answered is exit 3; one where none even opened is exit 1.

Every frame, both ways:

    fblock u8  function u8  flags u8  length u8  payload

    flags   (device id << 6) | (port << 4) | operator; ours are all 0 but the
            operator. SET 0, GET 1, SETGET 2, STATUS 3, ERROR 4, START 5,
            RESULT 6, PROCESSING 7

    [0.1]   GET is the init; STATUS answers a version, "1.1.0" (QC45),
            "1.0.4" (QC35)
    [2.2]   GET the battery; STATUS's first byte is the level. The QC45
            answers four bytes (5a ff ff 00), the QC35 one; the rest is not
            read, charging is not established on either
    [31.3]  the QC45's audio mode. GET -> STATUS idx: 0 Quiet (anc), 1 Aware
            (ambient); 2 and 3 are custom slots and are never named.
            START idx, 0 switches, answered by an empty PROCESSING; only the
            GET after it says what the headset now is
    [1.6]   the QC35's noise cancelling. GET -> STATUS value, mask: the value
            set now, and a bitmask of the values this headset takes (0b1011
            seen; it refused 2, the bit left clear, with an ERROR). 0 is off,
            1 the weaker strength and 3 the stronger, as the owner ranked
            them by ear; published third-party tables say the reverse.
            SETGET value is answered with the resulting STATUS

Which of the two questions a headset gets is the headset's to say, not its
name's (a Bose name is whatever its owner typed into the app). Every headset
is asked [31.3] first; only one that answers it with an ERROR before ever
answering it with a STATUS moves to [1.6], and stays there for the run. A
QC45 never gets that far, so its wire is the same frame for frame.

The headsets never speak unasked about a change (the QC35 sends [5.1] and
[4.2] frames after the init whose meaning is unknown; the framer steps over
them), so the current question is polled every four seconds, the battery
every fourth poll, and once more two seconds after every set.

Confirmed on a QC45 (docs/captures/bose-qc45.txt, bose-qc45-session.txt) and
a QC35 (docs/captures/bose-qc35.txt); PROTOCOL.md has the long version.
"""
import re

from core.headset import SILENT, Headset, SetupError, address, choice
from core.links import Rfcomm

HEADER = 4
SET, GET, SETGET, STATUS, ERROR, START, RESULT, PROCESSING = range(8)

VERSION = (0, 1)
BATTERY = (2, 2)
NOISE = (1, 6)
AUDIO_MODE = (31, 3)

VERSION_TEXT = re.compile(rb"[0-9]+\.[0-9]+\.[0-9]+")

# Tried in this order on every attempt. 8 is the one both captures used.
CHANNELS = (8, 2, 9)

# The QC45's two preset audio modes, by index. The custom slots 2 and 3 hold
# whatever the owner's app stored in them, so they get no name the panel
# could act on wrongly.
AUDIO_MODES = {0: "anc", 1: "ambient"}

# The QC35's [1.6] values. 0 is off, not a strength. The strengths are listed
# weakest first, the order the panel draws its row in.
NOISE_OFF = 0x00
STRENGTHS = ((0x01, "low"), (0x03, "high"))
STRENGTH_NAMES = dict(STRENGTHS)
STRENGTH_VALUES = {name: value for value, name in STRENGTHS}

NO_CHANNEL = "the headset answered on no Bose BMAP channel"
NO_MODE = "the headset did not answer the listening-mode query"

PROBE_TIMEOUT_S = 1.0     # one channel's chance to answer the init
CONNECT_TIMEOUT_S = 10.0
CONNECT_RETRY_MS = 1500   # BlueZ may still be settling the ACL just after connect
CONNECT_ATTEMPTS = 10

ANSWER_MS = 10000         # no mode by now: linked but silent
POLL_MS = 4000
BATTERY_EVERY = 4         # polls
READBACK_MS = 2000        # after a set; the poll would get there too, later


def bmap(block, operator, payload=b""):
    fblock, function = block
    return bytes([fblock, function, operator & 0x0F, len(payload)]) + bytes(payload)


INIT = bmap(VERSION, GET)
ASK_BATTERY = bmap(BATTERY, GET)
ASK_AUDIO_MODE = bmap(AUDIO_MODE, GET)
ASK_NOISE = bmap(NOISE, GET)


def start_audio_mode(index):
    """START [31.3]: switch to this index, no voice prompt."""
    return bmap(AUDIO_MODE, START, bytes([index, 0x00]))


def set_noise(value):
    return bmap(NOISE, SETGET, bytes([value]))


def _walk(data):
    """(block, operator, payload, end) for each whole frame at the front of data.

    The length byte is the only framing BMAP has, so a frame whose payload
    has not all arrived ends the walk and waits for the rest.
    """
    start = 0
    while len(data) - start >= HEADER:
        end = start + HEADER + data[start + 3]
        if end > len(data):
            return
        yield ((data[start], data[start + 1]), data[start + 2] & 0x0F,
               bytes(data[start + HEADER:end]), end)
        start = end


def take_frames(buffer):
    """Whole frames off the front of a bytearray, as [(block, operator, payload)]."""
    out, used = [], 0
    for block, operator, payload, end in _walk(buffer):
        out.append((block, operator, payload))
        used = end
    del buffer[:used]
    return out


def init_answered(raw):
    """links.Rfcomm's accepts: how many bytes of raw end with the init's answer.

    Only a [0.1] STATUS whose payload is a version string counts; an echo of
    the GET, an ERROR or anything else is not BMAP answering. Frames before
    it are consumed with it, and 0 means not yet.
    """
    for block, operator, payload, end in _walk(raw):
        if block == VERSION and operator == STATUS and VERSION_TEXT.fullmatch(payload):
            return end
    return 0


def battery_level(payload):
    """The first byte of a [2.2] STATUS as a percentage, or None."""
    if not payload or payload[0] > 100:
        return None
    return payload[0]


def noise_reading(payload):
    """(value, the values the mask names) from a [1.6] STATUS payload.

    No mask byte gives None for the set, and the caller falls back to the
    values it can name rather than guessing which the headset takes.
    """
    if not payload:
        return None, None
    if len(payload) < 2:
        return payload[0], None
    return payload[0], [bit for bit in range(8) if payload[1] & (1 << bit)]


class AudioModes:
    """The QC45's question: which preset audio mode [31.3] is on."""

    query = ASK_AUDIO_MODE

    def __init__(self):
        self.mode = None
        self.answered = False   # any STATUS at all, an unnamed slot included

    def refused(self, block, operator):
        """An ERROR before any STATUS: this headset has no audio modes.

        An ERROR after one (a refused START, say) is not that: no QC45
        capture shows one, so it must not move a QC45 off its question.
        """
        return block == AUDIO_MODE and operator == ERROR and not self.answered

    def read(self, block, operator, payload):
        if block != AUDIO_MODE or operator != STATUS:
            return False
        self.answered = True
        mode = AUDIO_MODES.get(payload[0]) if len(payload) == 1 else None
        if mode is None:
            return False
        self.mode = mode
        return True

    def fields(self):
        return {"mode": self.mode, "available": list(AUDIO_MODES.values())}

    def frame(self, verb, arg):
        if verb != "set":
            return None
        index = {name: index for index, name in AUDIO_MODES.items()}.get(arg)
        return None if index is None else start_audio_mode(index)


class NoiseCancelling:
    """The QC35's question: [1.6], off or one of two strengths.

    The two strengths are one mode graded twice, so the line carries the
    panel's strength row (ancLevel, ancLevels) the way Nothing's does.
    """

    query = ASK_NOISE

    def __init__(self):
        self.mode = None
        self.strength = None    # the last one seen, kept across off
        self.takes = None       # the values the mask named, once one has

    def refused(self, block, operator):
        return False

    def read(self, block, operator, payload):
        if block != NOISE or operator != STATUS:
            return False        # an ERROR is a refused set, and nothing moved
        value, takes = noise_reading(payload)
        if value == NOISE_OFF:
            self.mode = "off"
        elif value in STRENGTH_NAMES:
            self.mode, self.strength = "anc", STRENGTH_NAMES[value]
        else:
            return False        # value 2, or none: not forced into a name
        if takes is not None:
            self.takes = takes
        return True

    def strengths(self):
        return [name for value, name in STRENGTHS
                if self.takes is None or value in self.takes]

    def available(self):
        out = []
        if self.takes is None or NOISE_OFF in self.takes:
            out.append("off")
        if self.strengths():
            out.append("anc")
        return out

    def fields(self):
        strengths = self.strengths()
        # Off the whole run so far: show the strongest, which is also what
        # "set anc" would ask for. That default is the panel's, not the
        # headset's.
        shown = self.strength or (strengths[-1] if strengths else "")
        return {"mode": self.mode, "available": self.available(),
                "ancLevel": shown, "ancLevels": strengths}

    def frame(self, verb, arg):
        strengths = self.strengths()
        if verb == "set" and arg == "off" and "off" in self.available():
            return set_noise(NOISE_OFF)
        if verb == "set" and arg == "anc" and strengths:
            wanted = self.strength if self.strength in strengths else strengths[-1]
            return set_noise(STRENGTH_VALUES[wanted])
        if verb == "level" and choice(arg, strengths):
            return set_noise(STRENGTH_VALUES[arg])
        return None


class Bose(Headset):
    brand = "bose"
    label = "Bose"
    usage = "<classic-address>"

    @classmethod
    def parse(cls, args):
        if len(args) != 1:
            raise SetupError("usage: cornerstone-bridge bose " + cls.usage)
        return {"address": address(args[0])}

    def __init__(self, host, address):
        super().__init__(host)
        self.address = address
        self.buffer = bytearray()
        self.control = AudioModes()
        self.battery = None
        self.polls = 0
        self.readback = None

    def links(self):
        return [Rfcomm(self.address, CHANNELS, attempts=CONNECT_ATTEMPTS,
                       retry_ms=CONNECT_RETRY_MS, timeout_s=CONNECT_TIMEOUT_S,
                       probe=INIT, accepts=init_answered,
                       probe_timeout_s=PROBE_TIMEOUT_S)]

    def unavailable(self, link, message, code):
        if code == SILENT:
            message = NO_CHANNEL
        super().unavailable(link, message, code)

    @property
    def mode(self):
        return self.control.mode

    def opened(self, link):
        # The init was answered while the channel was confirmed.
        self.send(ASK_BATTERY)
        self.send(self.control.query)
        self.later(ANSWER_MS, self.give_up_if_silent)
        self.every(POLL_MS, self.poll)

    def poll(self):
        self.polls += 1
        self.send(self.control.query)
        if self.polls % BATTERY_EVERY == 0:
            self.send(ASK_BATTERY)

    def read_back(self):
        self.readback = None
        self.send(self.control.query)

    def give_up_if_silent(self):
        if self.mode is None:
            self.fail(SILENT, NO_MODE)

    def received(self, data, link):
        self.buffer += data
        for block, operator, payload in take_frames(self.buffer):
            if self.finished:
                return
            self.frame(block, operator, payload)

    def frame(self, block, operator, payload):
        if block == BATTERY:
            level = battery_level(payload) if operator == STATUS else None
            if level is not None:
                self.battery = level
                self.publish()
        elif self.control.refused(block, operator):
            self.control = NoiseCancelling()
            self.send(self.control.query)
        elif self.control.read(block, operator, payload):
            self.publish()

    def publish(self):
        if self.mode is None:
            return
        line = {"modes": True}
        line.update(self.control.fields())
        if self.battery is not None:
            line["battery"] = {"headset": self.battery, "charging": []}
        self.report(line)

    def command(self, verb, arg):
        if self.mode is None:
            return
        frame = self.control.frame(verb, arg)
        if frame is None:
            return
        self.send(frame)
        self.cancel(self.readback)
        self.readback = self.later(READBACK_MS, self.read_back)


HEADSET = Bose
