"""Running one headset: arguments, the GLib loop, stdin, stdout, signals.

    cornerstone-bridge <brand> <arguments the brand takes>
    cornerstone-bridge <brand> --help
    cornerstone-bridge --help

The shell starts one of these per connected headset (DeviceFollower.qml) and
talks to it through its stdin and stdout, as BRIDGE.md describes. With
CORNERSTONE_TRACE=1 in the environment every frame in and out is also written
to stderr as hex, which is how a new device's conversation is read
(tools/trace does exactly that).
"""
import ctypes
import importlib
import json
import os
import signal
import sys
import time
import traceback

from .codec import hexstr
from .headset import CLEAN, SETUP, TRANSIENT, SetupError

BRANDS_PACKAGE = "brands"


def write_line(line):
    """One JSON object on stdout. A closed pipe is the shell going away."""
    try:
        sys.stdout.write(json.dumps(line, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, ValueError, OSError):
        os._exit(CLEAN)


def arm_parent_death():
    """Die with the shell: an orphan would keep the channel nobody reads."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)
    except (OSError, AttributeError):
        return
    if os.getppid() == 1:
        os._exit(CLEAN)


def brand_names():
    from brands import NAMES
    return list(NAMES)


def load_brand(name):
    """The Headset subclass for a brand name, or None for one that is not ours."""
    if name not in brand_names():
        return None
    module = importlib.import_module(BRANDS_PACKAGE + "." + name)
    return module.HEADSET


class Host:
    """What a Headset reaches the world through, on a real GLib loop."""

    def __init__(self, headset_class, options, trace=False):
        from gi.repository import GLib
        self.GLib = GLib
        self.loop = GLib.MainLoop()
        self.trace = trace
        self.links = {}
        self.timers = set()
        self._bus = None
        self._stdin = b""
        self.headset = headset_class(self, **options)

    # -- the host interface a Headset uses

    def emit(self, line):
        if self.trace:
            self.say("OUT", json.dumps(line))
        write_line(line)

    def send(self, name, data):
        link = self.links.get(name)
        if link is None:
            return
        if self.trace:
            self.say("TX " + name, data if isinstance(data, str) else hexstr(data))
        link.write(data)

    def later(self, ms, callback, args, repeat):
        handle = []

        def fire():
            if self.headset.finished:
                self.timers.discard(handle[0])
                return False
            self.guard(callback, *args)
            keep = repeat and not self.headset.finished
            if not keep:
                self.timers.discard(handle[0])
            return keep

        handle.append(self.GLib.timeout_add(int(ms), fire))
        self.timers.add(handle[0])
        return handle[0]

    def cancel(self, handle):
        if handle in self.timers:
            self.timers.discard(handle)
            self.GLib.source_remove(handle)

    def now(self):
        return time.monotonic()

    def open(self, spec):
        from . import bluez
        self.close(spec.name)
        link = bluez.make(self, spec)
        self.links[spec.name] = link
        link.start()

    def close(self, name):
        link = self.links.pop(name, None)
        if link is not None:
            link.shutdown()

    def finish(self, _code):
        self.loop.quit()

    # -- what the links report

    def current(self, link):
        return self.links.get(link.name) is link

    def link_opened(self, link):
        if self.current(link):
            if self.trace:
                self.say("OPEN " + link.name, "channel %s" % link.channel if link.channel else "")
            self.guard(self.headset.opened, link.name)

    def link_data(self, link, data):
        if self.current(link):
            if self.trace:
                self.say("RX " + link.name, data if isinstance(data, str) else hexstr(data))
            self.guard(self.headset.received, data, link.name)

    def link_closed(self, link, reason):
        if self.current(link):
            self.close(link.name)
            self.guard(self.headset.closed, link.name, reason)

    def link_unavailable(self, link, message, code):
        if self.current(link):
            self.close(link.name)
            self.guard(self.headset.unavailable, link.name, message, code)

    def channel(self, name):
        link = self.links.get(name)
        return link.channel if link is not None else None

    # -- plumbing

    def bus(self):
        if self._bus is None:
            import dbus
            import dbus.mainloop.glib
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            self._bus = dbus.SystemBus()
        return self._bus

    def guard(self, callback, *args):
        """Run a headset callback; an exception in it ends the run, said aloud.

        A bridge that has stopped understanding its device is worse than none:
        it would hold the channel and report nothing.
        """
        try:
            return callback(*args)
        except Exception as error:
            if self.trace:
                traceback.print_exc()
            self.headset.fail(TRANSIENT, "%s: %s" % (type(error).__name__, error))
            return None

    def say(self, what, text):
        sys.stderr.write("%.3f %-10s %s\n" % (time.monotonic(), what, text))
        sys.stderr.flush()

    def _on_stdin(self, fd, condition):
        data = b""
        if condition & self.GLib.IO_IN:
            try:
                data = os.read(fd, 4096)
            except OSError:
                data = b""
        if not data:
            self.headset.stop()
            return False
        self._stdin += data
        while b"\n" in self._stdin and not self.headset.finished:
            raw, self._stdin = self._stdin.split(b"\n", 1)
            line = raw.decode("utf-8", "replace")
            if self.trace:
                self.say("IN", line)
            self.guard(self.headset.handle_line, line)
        if len(self._stdin) > 8192:
            self._stdin = b""
        return not self.headset.finished

    def run(self):
        GLib = self.GLib
        try:
            from gi.repository import GLibUnix
            signal_add = GLibUnix.signal_add
        except ImportError:
            signal_add = GLib.unix_signal_add
        for number in (signal.SIGTERM, signal.SIGINT):
            signal_add(GLib.PRIORITY_DEFAULT, number,
                       lambda *_: self.headset.stop() or False)
        GLib.io_add_watch(0, GLib.PRIORITY_DEFAULT,
                          GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._on_stdin)
        try:
            for spec in self.guard(self.headset.links) or []:
                if self.headset.finished:
                    break
                self.guard(self.open, spec)
            if not self.headset.finished:
                self.loop.run()
        finally:
            for name in list(self.links):
                self.close(name)
            self.guard(self.headset.stopping)
        return self.headset.exit_code or CLEAN


def usage(cls=None):
    if cls is None:
        return ("usage: cornerstone-bridge <brand> <arguments>\nbrands: "
                + " ".join(brand_names()))
    return "usage: cornerstone-bridge %s %s" % (cls.brand, cls.usage)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(usage() + "\n")
        return CLEAN if argv else SETUP
    name, args = argv[0], argv[1:]
    cls = load_brand(name)
    if cls is None:
        write_line({"modes": False, "error": "no bridge for %r; %s" % (name, usage())})
        return SETUP
    if args in (["-h"], ["--help"]):
        sys.stdout.write(usage(cls) + "\n")
        return CLEAN
    try:
        options = cls.parse(list(args))
    except SetupError as error:
        write_line({"modes": False, "error": str(error)})
        return SETUP
    arm_parent_death()
    try:
        import dbus  # noqa: F401
        from gi.repository import GLib  # noqa: F401
    except ImportError as error:
        write_line({"modes": False, "error": "missing python dependency %s"
                    % (getattr(error, "name", None) or error)})
        return SETUP
    trace = os.environ.get("CORNERSTONE_TRACE") == "1"
    try:
        return Host(cls, options, trace).run()
    except KeyboardInterrupt:
        return CLEAN
    except Exception as error:
        write_line({"modes": False, "error": "%s: %s" % (type(error).__name__, error)})
        return TRANSIENT
