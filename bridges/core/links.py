"""What a brand asks the runtime to open. Plain descriptions, no Bluetooth.

A Headset's links() returns a list of these; the runtime (core/bluez.py) turns
each into a live connection and calls the headset's opened / received / closed
with the spec's name. Kept apart from the code that opens them so a brand
module, and every test, imports no D-Bus and no GLib.
"""
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from .headset import MAIN


@dataclass
class Profile:
    """An RFCOMM channel BlueZ connects for us, through an org.bluez.Profile1.

    The headset advertises the service UUID in its SDP record; BlueZ looks up
    the channel and hands us the socket in NewConnection. The first
    ConnectProfile after the device connects often fails with
    br-connection-busy while bluetoothd finishes its own setup, so it is
    retried a few times before the run gives up.
    """
    address: str
    uuid: str
    name: str = MAIN
    delay_ms: int = 500       # before the first ConnectProfile
    retry_ms: int = 1500      # between attempts
    attempts: int = 10
    timeout_s: int = 30       # one ConnectProfile, which BlueZ answers slowly


@dataclass
class Rfcomm:
    """An RFCOMM channel we dial ourselves, by number.

    `channels` are tried in order on every attempt. Where the right channel
    can only be told by asking, `probe` is written on each one as it opens and
    `accepts(reply_bytes)` decides whether it is the one; the bytes that
    answered are then delivered to the headset as its first received data.
    """
    address: str
    channels: Sequence[int]
    name: str = MAIN
    attempts: int = 6
    retry_ms: int = 1500
    timeout_s: float = 5.0
    probe: Optional[bytes] = None
    accepts: Optional[Callable[[bytes], bool]] = None
    probe_timeout_s: float = 1.0


@dataclass
class Child:
    """A helper program whose stdout lines are the link (btgatt-client).

    received() is called with each line as text, ANSI colour stripped; send()
    takes a text line and writes it to the program's stdin.
    """
    argv: Sequence[str]
    name: str = MAIN


@dataclass
class Gatt:
    """A BLE GATT service on a device BlueZ knows, by its address.

    The link is up once notifications on `notify` are subscribed; each
    notification is one received() call with its whole value. A device that
    is not connected is connected here, and disconnected again on close only
    if this link was the one that connected it. With `scan`, a device BlueZ
    has not seen yet is looked for with a short discovery first.
    """
    address: str
    service: str
    notify: str
    write: str
    name: str = MAIN
    timeout_s: float = 20.0
    scan: bool = False
    write_type: str = "command"
