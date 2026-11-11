"""Sony MDR, v1 and v2: Off / ANC / Ambient, the Ambient level, Focus on voice
and, on the models that have one, the wear sensor.

    cornerstone-bridge sony <classic-address> [<mdr-uuid>] [<reported-name>]

The headset serves its control protocol on one of two vendor RFCOMM UUIDs,
956c7b26-... (MDR v2, the current range) or 96cc203e-... (MDR v1, the
WH-1000XM4 generation); BlueZ connects it through a Profile1. Which one to
register is the second argument, v2 when it is left out. The reported name
picks the model's row below; no name gets exactly what a nameless caller
always got. Confirmed on a WH-CH720N, WH-1000XM4, XM5 and XM6 (PROTOCOL.md,
tests/pins/sony).

Every frame, both ways:

    3E  kind u8  seq u8  length u32 BE  payload  sum8  3C

    The sum covers kind through payload. Everything between the markers is
    byte-stuffed: 3C, 3D and 3E become 3D followed by the byte AND EF. The
    markers themselves are never stuffed, so a frame runs from a 3E to the
    first 3C after it.

    kind    0x0C a command (0x0E is the same on a second data channel)
            0x01 an ACK
    seq     one toggling bit. A command is answered with an ACK carrying
            1 - seq; our own seq is whatever the last ACK said. Only one
            command is outstanding at a time.

Payloads:

    00 00           protocol info request, the handshake. Answered with 01 ...,
                    eight bytes from a v2 headset, four from a v1
    66 <t>          NC/ASM get, for "inquired type" <t>
    67 <t> ...      its reply          69 <t> ...  the same, volunteered
    68 <t> ...      NC/ASM set
    f2 10           wearing status get
    f3 10 <flag>    its reply          f5 10 .. <flag>  volunteered
                    the last byte 00 is worn, anything else not

Which inquired type a model answers cannot be known in advance, so every
candidate is asked in turn (v2 0x17, 0x15, 0x22; v1 0x02), the generation
the handshake's length names first. A wrong one is ACKed and then ignored,
which is why each gets a deadline, and the type that answers settles how
every later block is read and written. The block layouts:

    0x17    67 17 01 <on> <ambient> <voice> <level>          (v2, 6 bytes)
    0x15    67 15 01 <on> <ambient> <nc-value> <voice> <level>   (v2, 8)
    0x22    67 22 01 <on> <voice> <level>    ambient only: on is ambient
    0x19    69 19 01 <on> <ambient> <voice> <level> 00 00
            volunteered by a WH-1000XM6, whose GET answers on 0x17; read as
            0x17 one slot wider, and answered with 0x17's SET
    0x02    67 02 <effect> <nc-type> <nc-value> <asm-type> <asm-id> <level>
            (v1). nc-value 0 with the effect on is Ambient, anything else ANC;
            asm-id 1 is Focus on voice

A SET is ACKed at once but takes effect about a third of a second later,
when the headset volunteers the new state. That notification is what the
panel is shown; nothing is read back after a set and nothing is assumed.
"""
import re
import struct

from core import codec
from core.headset import (SILENT, TRANSIENT, Headset, SetupError, address,
                          choice, number, switch)
from core.links import Profile

UUID_V2 = "956c7b26-d49a-4ba8-b03f-b17d393cb6e2"
UUID_V1 = "96cc203e-5068-46ad-b32d-e316f5e069ba"
UUID_SHAPE = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE)

START, END, ESCAPE, ESCAPE_MASK = 0x3E, 0x3C, 0x3D, 0xEF
COMMAND, COMMAND_2, ACK = 0x0C, 0x0E, 0x01
SHORTEST = 9            # 3E, kind, seq, four length bytes, sum, 3C

PROTOCOL_INFO_GET = b"\x00\x00"
PROTOCOL_INFO = 0x01
NCASM_GET, NCASM_RET, NCASM_SET, NCASM_NTFY = 0x66, 0x67, 0x68, 0x69
STATUS_GET, STATUS_RET, STATUS_NTFY = 0xF2, 0xF3, 0xF5
WEARING = 0x10

# The NC/ASM types worth asking, per generation. Sony's v1 table also
# numbers an NC-only 0x01 and an ambient-only 0x03; nobody has seen either
# answered and their block lengths are unconfirmed, so they are not asked.
CANDIDATES_V2 = (0x17, 0x15, 0x22)
CANDIDATES_V1 = (0x02,)
CANDIDATES = CANDIDATES_V2 + CANDIDATES_V1
VOLUNTEERED = {0x19: 0x17}      # a type only ever volunteered -> the type it stands for

AVAILABLE = {
    0x17: ["off", "anc", "ambient"],
    0x15: ["off", "anc", "ambient"],
    0x22: ["off", "ambient"],
    0x02: ["off", "anc", "ambient"],
}

# What a SET echoes as 0x15's nc-value (v1: nc-type) before the headset has
# reported one: 0x02, what a two-microphone headset reports on both.
NC_VALUE_DEFAULT = 0x02
MAX_LEVEL = 20

# Which headsets are asked the wearing-status question, by reported name.
# The rows that say no are pinned models nobody has seen answer it; a
# headset that works is not sent a frame it never was. A model nobody has
# held is asked: silence costs nothing, the flag is read only from a real
# reply. A new model is one more row, never an edit to another owner's.
MODELS = {
    "WH-CH720N": {"wear": False},    # ncr
    "WH-1000XM5": {"wear": False},   # huynguyendinhquang
    "WH-1000XM4": {"wear": False},   # seth-reee, PR #6
    "WH-1000XM6": {"wear": True},    # f-iacono, PR #7: answers f3 10 00
}
UNKNOWN = {"wear": True}
NAMELESS = {"wear": False}

# Names asked nothing at all, NC/ASM included, and parked right after the
# handshake. The WH-CH520 answers 66 17 with a plausible block, but a SET on
# it was ACKed and never reflected back on four live connections
# (docs/captures/sony-wh-ch520.txt), and Sony lists no ANC or Ambient for
# it. enobale, PR #15.
NO_MODES = {"WH-CH520"}
NO_MODES_MESSAGE = "no ANC/Ambient on this model (confirmed on hardware; see PROTOCOL.md)"

GREET_MS = 2000         # per handshake attempt
GREET_ATTEMPTS = 3      # Gadgetbridge sends it up to three times too
CANDIDATE_MS = 3000     # per inquired type; a wrong one is ACKed and dropped
ACK_MS = 3000           # an ACK this late is lost; the queue moves on


# ---- framing

def stuff(data):
    out = bytearray()
    for byte in data:
        if byte in (START, END, ESCAPE):
            out += bytes([ESCAPE, byte & ESCAPE_MASK])
        else:
            out.append(byte)
    return bytes(out)


def unstuff(data):
    out = bytearray()
    escaped = False
    for byte in data:
        if escaped:
            out.append(byte | 0x10)
            escaped = False
        elif byte == ESCAPE:
            escaped = True
        else:
            out.append(byte)
    return bytes(out)


def frame(kind, seq, payload=b""):
    """One frame: checksummed over the plain bytes, then stuffed."""
    body = bytes([kind, seq]) + struct.pack(">I", len(payload)) + bytes(payload)
    return bytes([START]) + stuff(body + bytes([codec.sum8(body)])) + bytes([END])


def read_frame(raw):
    """(kind, seq, payload) from one frame, markers included, or None.

    A frame that does not add up is dropped, not guessed at: acting on half a
    payload would move the headset somewhere nobody asked for.
    """
    if len(raw) < SHORTEST or raw[0] != START or raw[-1] != END:
        return None
    body = unstuff(raw[1:-1])
    if len(body) < 7:
        return None
    size = struct.unpack_from(">I", body, 2)[0]
    if len(body) != 6 + size + 1 or body[-1] != codec.sum8(body[:-1]):
        return None
    return body[0], body[1], body[6:-1]


def unframe(buffer):
    """Whole frames off the front of a bytearray, as [(kind, seq, payload)].

    Bytes before a 3E are noise. A partial frame stays for the next read; a
    frame that does not decode is dropped whole.
    """
    out = []
    while True:
        start = buffer.find(bytes([START]))
        if start < 0:
            buffer.clear()
            return out
        end = buffer.find(bytes([END]), start + 1)
        if end < 0:
            del buffer[:start]
            return out
        raw = bytes(buffer[start:end + 1])
        del buffer[:end + 1]
        decoded = read_frame(raw)
        if decoded is not None:
            out.append(decoded)


# ---- the NC/ASM blocks

def question_order(handshake_length):
    """The candidates, the generation the handshake named first.

    Eight bytes is v2, fewer v1. A hint, not a rule: every candidate stays on
    the list, so a headset that answers 0x17 is always asked 0x17.
    """
    first = CANDIDATES_V1 if handshake_length < 8 else CANDIDATES_V2
    return first + tuple(c for c in CANDIDATES if c not in first)


def read_ncasm(payload, settled=None):
    """The state a RET or NTFY carries, or None.

    `settled` is the type this headset already answered with, if any; a
    block of another type is then dropped, apart from the type that stands
    in for it in volunteered notifications. The result's "inquired" is the
    type the block stands for, so a 0x19 comes back as 0x17.
    """
    if len(payload) < 2 or payload[0] not in (NCASM_RET, NCASM_NTFY):
        return None
    kind = payload[1]
    stands_for = VOLUNTEERED.get(kind, kind)
    if settled is not None and stands_for != settled:
        return None
    if kind in CANDIDATES_V1:
        state = _read_v1(payload)
    elif kind in VOLUNTEERED:
        state = _read_volunteered(payload)
    elif kind in CANDIDATES_V2:
        state = _read_v2(payload)
    else:
        return None
    if state is not None:
        state["inquired"] = stands_for
    return state


def _mode(on, ambient):
    return "off" if not on else ("ambient" if ambient else "anc")


def _read_v2(payload):
    # Voice and level are the last two bytes of all three layouts.
    if len(payload) < 6:
        return None
    kind = payload[1]
    ambient = True if kind == 0x22 else (len(payload) > 4 and payload[4] == 0x01)
    return {
        "mode": _mode(payload[3] == 0x01, ambient),
        "voice": payload[-2] == 0x01,
        "level": payload[-1],
        "nc_value": payload[5] if kind == 0x15 and len(payload) > 7 else None,
    }


def _read_volunteered(payload):
    # 0x19: 0x17's fields, voice and level one slot on, two zero bytes after.
    if len(payload) < 9:
        return None
    return {
        "mode": _mode(payload[3] == 0x01, payload[4] == 0x01),
        "voice": payload[5] == 0x01,
        "level": payload[6],
        "nc_value": None,
    }


def _read_v1(payload):
    # 67 02 effect nc-type nc-value asm-type asm-id level, read by index.
    if len(payload) < 8:
        return None
    return {
        "mode": _mode(payload[2] == 0x01, payload[4] == 0x00),
        "voice": payload[6] == 0x01,
        "level": payload[7],
        "nc_value": payload[3],
    }


def set_block(inquired, mode, level, voice, nc_value=NC_VALUE_DEFAULT):
    """The SET payload for the type this headset answered with.

    Level and voice ride along in every mode: the headset stores them and
    applies them only in Ambient, so a mode change repeats the stored values
    rather than sending zeroes.
    """
    on = 1 if mode != "off" else 0
    ambient = 1 if mode == "ambient" else 0
    level = max(0, min(MAX_LEVEL, int(level)))
    voice = 1 if voice else 0
    if inquired == 0x02:
        # What sony-device-center sends and a WH-1000XM4 obeys: effect 0x11,
        # nc-type 01, nc-value 2 for ANC and 0 for Ambient, asm-type 01, and
        # a level only in Ambient, never 0 there.
        if mode == "ambient":
            return bytes([NCASM_SET, 0x02, 0x11, 0x01, 0x00, 0x01, voice, max(1, level)])
        return bytes([NCASM_SET, 0x02, 0x11 if on else 0x00, 0x01,
                      0x02 if on else 0x00, 0x01, voice, 0x00])
    if inquired == 0x15:
        return bytes([NCASM_SET, 0x15, 0x01, on, ambient, nc_value, voice, level])
    if inquired == 0x22:
        return bytes([NCASM_SET, 0x22, 0x01, on, voice, level])
    return bytes([NCASM_SET, 0x17, 0x01, on, ambient, voice, level])


def model_row(name):
    if not name:
        return NAMELESS
    return MODELS.get(name, UNKNOWN)


class Sony(Headset):
    brand = "sony"
    label = "Sony"
    usage = "<classic-address> [<mdr-uuid>] [<reported-name>]"

    @classmethod
    def parse(cls, args):
        if not 1 <= len(args) <= 3:
            raise SetupError("usage: cornerstone-bridge sony " + cls.usage)
        uuid = args[1].strip() if len(args) > 1 else ""
        if not uuid:
            uuid = UUID_V2
        if not UUID_SHAPE.match(uuid):
            raise SetupError("not a UUID: %r" % uuid)
        name = args[2].strip() if len(args) > 2 else ""
        return {"address": address(args[0]), "uuid": uuid.lower(), "name": name}

    def __init__(self, host, address, uuid=UUID_V2, name=""):
        super().__init__(host)
        self.address = address
        self.uuid = uuid
        self.name = name
        self.model = model_row(name)
        self.no_modes = name in NO_MODES
        self.buffer = bytearray()

        # One command at a time: our seq is the last ACK's, and the rest wait.
        self.seq = 0
        self.waiting = False
        self.queue = []
        self.ack_timer = None

        # The handshake, then the candidates in turn.
        self.greeted = False
        self.greet_timer = None
        self.candidates = CANDIDATES
        self.asking = None
        self.ask_timer = None

        # What the headset said. Nothing is reported, and nothing can be
        # set, until some inquired type has answered.
        self.inquired = None
        self.mode = None
        self.level = 0
        self.voice = False
        self.nc_value = NC_VALUE_DEFAULT
        # v1 reports level 0 in ANC; this is the last level it reported
        # above 0, so going back to Ambient does not start at the bottom.
        self.ambient_level = 0
        # None until the headset says; "worn" stays off the line until then.
        self.worn = None

    def links(self):
        return [Profile(self.address, self.uuid)]

    # ---- the handshake and the questions

    def opened(self, link):
        self.greet(1)

    def greet(self, attempt):
        """The protocol-info request, again while the headset ignores it."""
        self.greet_timer = None
        if self.finished or self.greeted:
            return
        if attempt > GREET_ATTEMPTS:
            self.fail(TRANSIENT, "the Sony headset did not answer the handshake")
            return
        # Straight out, not queued: a retry goes whether or not the last
        # attempt was ever acknowledged.
        self.queue.clear()
        self.cancel(self.ack_timer)
        self.ack_timer = None
        self.waiting = False
        self.send(frame(COMMAND, self.seq, PROTOCOL_INFO_GET))
        self.greet_timer = self.later(GREET_MS, self.greet, attempt + 1)

    def greeted_with(self, payload):
        if self.greeted:
            return
        self.greeted = True
        self.cancel(self.greet_timer)
        self.greet_timer = None
        if self.no_modes:
            self.fail(SILENT, NO_MODES_MESSAGE)
            return
        self.candidates = question_order(len(payload))
        self.ask(0)
        # Wearing status is volunteered only when it changes, so a model that
        # answers it is asked once, behind the first question.
        if self.model["wear"]:
            self.push(bytes([STATUS_GET, WEARING]))

    def ask(self, index):
        if self.finished or self.inquired is not None:
            return
        if index >= len(self.candidates):
            self.fail(SILENT, "the headset did not answer the listening-mode query")
            return
        self.asking = index
        self.push(bytes([NCASM_GET, self.candidates[index]]))
        self.ask_timer = self.later(CANDIDATE_MS, self.ask_next, index)

    def ask_next(self, index):
        self.ask_timer = None
        if self.inquired is None and self.asking == index:
            self.ask(index + 1)

    # ---- one command at a time

    def push(self, payload):
        self.queue.append(bytes(payload))
        self.pump()

    def pump(self):
        if self.waiting or not self.queue or self.finished:
            return
        self.waiting = True
        self.send(frame(COMMAND, self.seq, self.queue.pop(0)))
        self.ack_timer = self.later(ACK_MS, self.ack_overdue)

    def ack_overdue(self):
        # The headset drops the odd frame; nothing follows from one lost ACK.
        self.ack_timer = None
        self.waiting = False
        self.pump()

    def acked(self, seq):
        # The only thing that moves our seq: a frame the headset starts does not.
        self.seq = seq
        self.cancel(self.ack_timer)
        self.ack_timer = None
        self.waiting = False
        self.pump()

    # ---- what arrives

    def received(self, data, link):
        self.buffer += data
        for kind, seq, payload in unframe(self.buffer):
            if self.finished:
                return
            self.frame_in(kind, seq, payload)

    def frame_in(self, kind, seq, payload):
        if kind == ACK:
            self.acked(seq)
            return
        if kind in (COMMAND, COMMAND_2):
            # Before acting on it, unsolicited frames too: an unacknowledged
            # frame is sent again and then the headset goes quiet.
            self.send(frame(ACK, (seq & 1) ^ 1))
        if not payload:
            return
        if payload[0] == PROTOCOL_INFO:
            self.greeted_with(payload)
            return
        state = read_ncasm(payload, self.inquired)
        if state is not None:
            self.settle(state)
        elif (len(payload) >= 3 and payload[0] in (STATUS_RET, STATUS_NTFY)
              and payload[1] == WEARING):
            self.worn = payload[-1] == 0x00
            self.publish()

    def settle(self, state):
        if self.inquired is None:
            self.inquired = state["inquired"]
            self.cancel(self.ask_timer)
            self.ask_timer = None
        self.mode = state["mode"]
        self.level = state["level"]
        self.voice = state["voice"]
        if self.inquired == 0x02 and self.level > 0:
            self.ambient_level = self.level
        if state["nc_value"] is not None:
            # A SET must echo what the headset reported; an invented value
            # is how the 0x15 layout gets rejected.
            self.nc_value = state["nc_value"]
        self.publish()

    def publish(self):
        if self.inquired is None:
            return
        line = {
            "modes": True,
            "mode": self.mode,
            "available": list(AVAILABLE[self.inquired]),
            "level": self.level,
            "voice": self.voice,
        }
        if self.worn is not None:
            line["worn"] = self.worn
        self.report(line)

    # ---- the panel's commands

    def command(self, verb, arg):
        if self.inquired is None:
            return
        available = AVAILABLE[self.inquired]
        if verb == "set":
            mode = choice(arg, available)
            if mode is None:
                return
            level = self.level
            if self.inquired == 0x02 and mode == "ambient" and not level:
                level = self.ambient_level
            self.push(self.block(mode, level, self.voice))
        elif verb == "level":
            # The level is stored only by an Ambient SET, so asking for one
            # is asking for Ambient.
            level = number(arg, 0, MAX_LEVEL)
            if level is not None and "ambient" in available:
                self.push(self.block("ambient", level, self.voice))
        elif verb == "voice":
            # A stored setting like the level, sent in the current mode
            # rather than dragging the headset into Ambient.
            on = switch(arg)
            if on is not None:
                self.push(self.block(self.mode or "ambient", self.level, on))

    def block(self, mode, level, voice):
        return set_block(self.inquired, mode, level, voice, self.nc_value)


HEADSET = Sony
