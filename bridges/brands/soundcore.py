"""Soundcore (Anker): Off / ANC / Ambient, and on the six-byte models the
Ambient level (1-5) and wind noise reduction.

    cornerstone-bridge soundcore <classic-address> [<vendor-uuid>]

Every model serves its control channel on a vendor RFCOMM UUID that starts
0cf12d31-fac3-4553-bd80-d6832e7 and ends in the model id (d1402 on the
Space 2); BlueZ connects it through a Profile1. The UUID is the second
argument where the caller knows it. Where it does not (the shell today
passes the address alone), it is looked up among the UUIDs BlueZ lists for
the device, the way the old bridge did, and 0cf12d31-...-d6832e700000 when
none matches. The model id picks the row in MODELS.

Frames:

    out     08 ee 00 00 00  command u8 u8  length u16 LE  body  sum8
    in      09 ff 00 00 01  command u8 u8  length u16 LE  body  sum8

    length is the whole frame's, header and sum included; the sum is the
    plain sum of everything before it.

    01 01   out: ask for the whole state (the handshake). in: the state; the
            sound-mode block sits at the row's offset
    06 01   out: ask for the sound-mode block alone. in: the block, as a
            reply or volunteered after a change
    06 81   out: set the block. in: its ACK, with an empty body

The sound-mode block: mode (00 ANC, 01 Ambient, 02 Off), then the model's
own parameters. Six bytes on the Space 2 and Space One Pro, the last two
wind noise reduction and the Ambient level; four on the Life Q30, which has
neither. A set writes the device's own block back with one field changed,
never bytes of ours: a wrong offset or a fixed tail posts the neighbouring
settings back as parameters (PROTOCOL.md has both stories).

The offsets are per model and only a held one is trusted. Where a row says
`query`, 06 01 is asked after every state and again 400 ms after every set,
and its reply stands over whatever the offset produced.
"""
import re
import struct

from core import codec
from core.headset import SILENT, TRANSIENT, Headset, SetupError, address, choice, number, switch
from core.links import Profile

UUID_PREFIX = "0cf12d31-fac3-4553-bd80-d6832e7"
DEFAULT_UUID = UUID_PREFIX + "00000"
UUID_SHAPE = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE)

OUT_HEADER = bytes([0x08, 0xEE, 0x00, 0x00, 0x00])
IN_MARK = bytes([0x09, 0xFF])
SMALLEST = 10           # header, command, length, sum

STATE = (0x01, 0x01)
SOUND_MODES = (0x06, 0x01)
SOUND_MODES_SET = (0x06, 0x81)

MODE_NAMES = {0x00: "anc", 0x01: "ambient", 0x02: "off"}
MODE_BYTES = {name: byte for byte, name in MODE_NAMES.items()}
AVAILABLE = ["off", "anc", "ambient"]
NARROWEST = 4           # the Life Q30's block; nothing narrower has been seen
FULL = 6                # wide enough to carry wind (4) and the level (5)
WIND, LEVEL = 4, 5
LEVELS = (1, 5)

# Per model, keyed by the model id at the end of the vendor UUID. A row is
# somebody's headphones; UNKNOWN is what a model nobody has held gets.
#
#   offset  where the block sits in the 01 01 state body
#   width   how many bytes it has (six when a row does not say)
#   query   ask 06 01 after the state and after each set
MODELS = {
    # Space 2, Sovego, PR #3. Volunteers 06 01 after a change; whether it
    # answers a request is untested, so it is never asked.
    "d1402": {"name": "Space 2", "offset": 71, "query": False},
    # Space One Pro (A3062), sasiruLK, PR #5. The block sits two to the
    # left, where OpenSCQ30's A3062 parser puts it, and 06 01 is answered.
    "b3062": {"name": "Space One Pro", "offset": 69, "query": True},
    # Life Q30 (A3028), kevinbsr, 2026-09-18. A 70-byte state, the block at
    # 35 and four bytes wide; the firmware string starts at 39. 06 01 is
    # answered with the same four bytes.
    "b302a": {"name": "Life Q30", "offset": 35, "width": 4, "query": True},
}
# Read where the Space 2 keeps it, then asked, so a block elsewhere is
# corrected by the reply before anyone clicks.
UNKNOWN = {"name": "", "offset": 71, "query": True}

GREET_MS = 2000
GREET_ATTEMPTS = 3
ANSWER_MS = 3000        # for a 06 01 asked because the state had no mode
CONFIRM_MS = 400        # the One Pro ACKs a set and says nothing more


def packet(command, body=b""):
    size = SMALLEST + len(body)
    raw = OUT_HEADER + bytes(command) + struct.pack("<H", size) + bytes(body)
    return raw + bytes([codec.sum8(raw)])


def unpack(buffer):
    """Whole inbound frames off the front of a bytearray, as [(command, body)].

    A frame whose sum is wrong, or whose length is too short to be a frame,
    is not one: the search goes on from the next byte, so a bad length can
    neither swallow the frame after it nor stall the reader.
    """
    out = []
    while True:
        start = buffer.find(IN_MARK)
        if start < 0:
            # Keep a lone 09 at the end; its ff may be in the next read.
            keep = 1 if buffer.endswith(IN_MARK[:1]) else 0
            del buffer[:len(buffer) - keep]
            return out
        del buffer[:start]
        if len(buffer) < 9:
            return out
        size = struct.unpack_from("<H", buffer, 7)[0]
        if size < SMALLEST:
            del buffer[:1]
            continue
        if len(buffer) < size:
            return out
        raw = bytes(buffer[:size])
        if codec.sum8(raw[:-1]) != raw[-1]:
            del buffer[:1]
            continue
        del buffer[:size]
        out.append(((raw[5], raw[6]), raw[9:-1]))


def model_for(uuid):
    """The MODELS row for a vendor UUID, or UNKNOWN."""
    text = str(uuid or "").lower()
    if not text.startswith(UUID_PREFIX):
        return UNKNOWN
    return MODELS.get(text[len(UUID_PREFIX):], UNKNOWN)


def advertised_uuid(host, address):
    """The Soundcore UUID BlueZ lists for this address, or None.

    Only the live runtime's host has a bus; anything that goes wrong here is
    left for the Profile link to meet and report in its own words.
    """
    bus = getattr(host, "bus", None)
    if bus is None:
        return None
    try:
        objects = bus().get_object("org.bluez", "/").GetManagedObjects(
            dbus_interface="org.freedesktop.DBus.ObjectManager", timeout=5)
    except Exception:
        return None
    found = None
    for interfaces in objects.values():
        device = interfaces.get("org.bluez.Device1")
        if not device or str(device.get("Address", "")).upper() != address:
            continue
        for uuid in device.get("UUIDs", []):
            uuid = str(uuid).lower()
            if uuid.startswith(UUID_PREFIX):
                if bool(device.get("Connected", False)):
                    return uuid
                found = found or uuid
    return found


def block_at(body, offset, width):
    """The sound-mode block at offset, as wide as the row says and the body
    holds, or None where not even the narrowest block fits."""
    if len(body) < offset + NARROWEST:
        return None
    return bytes(body[offset:offset + width])


class Soundcore(Headset):
    brand = "soundcore"
    label = "Soundcore"
    usage = "<classic-address> [<vendor-uuid>]"

    @classmethod
    def parse(cls, args):
        if not 1 <= len(args) <= 2:
            raise SetupError("usage: cornerstone-bridge soundcore " + cls.usage)
        uuid = args[1].strip().lower() if len(args) > 1 else ""
        if uuid and not UUID_SHAPE.match(uuid):
            raise SetupError("not a UUID: %r" % uuid)
        return {"address": address(args[0]), "uuid": uuid or None}

    def __init__(self, host, address, uuid=None):
        super().__init__(host)
        self.address = address
        self.uuid = uuid
        self.model = model_for(uuid)
        self.buffer = bytearray()
        self.greeted = False
        self.greet_timer = None
        self.answer_timer = None
        # Set once the device has sent a 06 01, asked or not; only then is it
        # asked again after a set.
        self.query_answered = False
        self.mode = None
        self.block = b""            # the device's own block, as it last said

    @property
    def width(self):
        return self.model.get("width", FULL)

    def links(self):
        if self.uuid is None:
            self.uuid = advertised_uuid(self.host, self.address) or DEFAULT_UUID
            self.model = model_for(self.uuid)
        return [Profile(self.address, self.uuid)]

    # ---- the handshake

    def opened(self, link):
        self.greet(1)

    def greet(self, attempt):
        self.greet_timer = None
        if self.finished or self.greeted:
            return
        if attempt > GREET_ATTEMPTS:
            self.fail(TRANSIENT, "the Soundcore headphones did not answer the handshake")
            return
        self.send(packet(STATE))
        self.greet_timer = self.later(GREET_MS, self.greet, attempt + 1)

    # ---- what arrives

    def received(self, data, link):
        self.buffer += data
        for command, body in unpack(self.buffer):
            if self.finished:
                return
            if command == STATE:
                self.read_state(body)
            elif command == SOUND_MODES:
                self.query_answered = True
                if len(body) >= NARROWEST:
                    self.take(body[:self.width])

    def read_state(self, body):
        self.greeted = True
        self.cancel(self.greet_timer)
        self.greet_timer = None
        block = block_at(body, self.model["offset"], self.width)
        if not self.take(block) and self.mode is None:
            if not self.model["query"]:
                self.fail(SILENT, "the headphones did not answer the listening-mode query")
                return
            # A row that asks gets the reply's chance before the verdict.
            if self.answer_timer is None:
                self.answer_timer = self.later(ANSWER_MS, self.give_up_if_silent)
        if self.model["query"]:
            self.ask()

    def take(self, block):
        """A block from the device. False if it names no mode we know."""
        if block is None or block[0] not in MODE_NAMES:
            return False
        self.block = bytes(block)
        self.mode = MODE_NAMES[block[0]]
        self.cancel(self.answer_timer)
        self.answer_timer = None
        self.publish()
        return True

    def give_up_if_silent(self):
        self.answer_timer = None
        if self.mode is None:
            self.fail(SILENT, "the headphones did not answer the listening-mode query")

    def ask(self):
        self.send(packet(SOUND_MODES))

    def publish(self):
        line = {"modes": True, "mode": self.mode, "available": list(AVAILABLE)}
        # A four-byte block has neither, and the panel draws the dial and the
        # switch only when the line carries them.
        if len(self.block) >= FULL:
            line["level"] = self.block[LEVEL]
            line["voice"] = bool(self.block[WIND])
        self.report(line)

    # ---- the panel's commands

    def command(self, verb, arg):
        if self.mode is None:
            return
        if verb == "set":
            mode = choice(arg, MODE_BYTES)
            if mode is not None:
                self.write(0, MODE_BYTES[mode])
            return
        if len(self.block) < FULL:
            return
        if verb == "level":
            value, field = number(arg, *LEVELS), LEVEL
        elif verb in ("wind", "voice"):
            on = switch(arg)
            value, field = (None if on is None else int(on)), WIND
        else:
            return
        if value is None:
            return
        self.write(field, value)
        # The one place the line runs ahead of the device: the level and the
        # switch are shown as written, which tests/pins/soundcore freeze.
        block = bytearray(self.block)
        block[field] = value
        self.block = bytes(block)
        self.publish()

    def write(self, field, value):
        block = bytearray(self.block)
        block[field] = value
        self.send(packet(SOUND_MODES_SET, bytes(block)))
        # Only a row that asks, and only a device that has answered 06 01.
        if self.model["query"] and self.query_answered:
            self.later(CONFIRM_MS, self.ask)


HEADSET = Soundcore
