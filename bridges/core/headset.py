"""The base every brand module builds on.

A brand module describes one headphone protocol and nothing else: which link
to open, what to say when it opens, how to read what comes back, and what to
do with a command from the panel. Everything around that (the BlueZ plumbing,
the GLib loop, stdin and stdout, signals, exit codes) is the runtime's, and a
brand reaches it only through the handful of methods below. That keeps every
protocol testable with no Bluetooth at all: tests hand a Headset a fake host
and play bytes at it.

The contract with the shell is BRIDGE.md. In short: every line on stdout is
the device's whole state as JSON, a line goes out only when that state
changes, and the process ends with one of four exit codes.
"""
import re

CLEAN = 0       # asked to stop, or the shell closed the pipe
TRANSIENT = 1   # the link failed; says nothing about the device
SILENT = 3      # linked, asked, never answered: this device lacks the feature
SETUP = 4       # bad arguments or a missing helper: this machine's problem

MAIN = "main"   # the name of a headset's first (usually only) link


class SetupError(Exception):
    """Raised while reading arguments or opening a link: exit 4, this message."""


class Headset:
    """One device's protocol. Subclass it once per brand.

    Class attributes a subclass sets:

        brand   the module name, used for the BlueZ profile path
        label   how messages name the device ("Moto" -> "the Moto channel closed")
        usage   the arguments after the brand, for -h and for argument errors

    Methods a subclass overrides (all optional except links):

        parse(args)            classmethod; argv after the brand -> options dict
        links()                the link specs to open at start (core.links)
        opened(link)           a link is up
        received(data, link)   bytes (or a text line, for a Child link) arrived
        closed(link, reason)   a link went away; default ends the run, exit 1
        unavailable(link, message, code)
                               a link never came up; default ends the run
        command(verb, arg)     one stdin line, split at the first space
        stopping()             the run is over, whatever the reason
    """

    brand = ""
    label = ""
    usage = ""

    def __init__(self, host):
        self.host = host
        self.exit_code = None
        self._last_line = None

    # ---- what a brand overrides

    @classmethod
    def parse(cls, args):
        return {}

    def links(self):
        return []

    def opened(self, link):
        pass

    def received(self, data, link):
        pass

    def closed(self, link, reason):
        self.fail(TRANSIENT, "the %s channel closed%s"
                  % (self.label, ": " + reason if reason else ""))

    def unavailable(self, link, message, code):
        """A link never came up. code is TRANSIENT, SILENT or SETUP."""
        self.fail(code, message)

    def command(self, verb, arg):
        pass

    def stopping(self):
        pass

    # ---- what a brand calls

    @property
    def finished(self):
        return self.exit_code is not None

    def send(self, data, link=MAIN):
        if not self.finished:
            self.host.send(link, data)

    def later(self, ms, callback, *args):
        """Run callback(*args) once, ms from now. Returns a handle for cancel()."""
        return self.host.later(ms, callback, args, False)

    def every(self, ms, callback, *args):
        """Run callback(*args) every ms until cancelled or the run ends."""
        return self.host.later(ms, callback, args, True)

    def cancel(self, handle):
        if handle is not None:
            self.host.cancel(handle)

    def now(self):
        """Monotonic seconds, from the host's clock (a fake one in tests)."""
        return self.host.now()

    def open(self, spec):
        """Open another link while running (TOZO's case)."""
        if not self.finished:
            self.host.open(spec)

    def close(self, link):
        self.host.close(link)

    def emit(self, line):
        """Write one line as it is, whether or not it repeats the last one."""
        self._last_line = line
        self.host.emit(line)

    def report(self, line):
        """Write the device's state, but only when it differs from the last line."""
        if line != self._last_line:
            self.emit(line)

    def fail(self, code, message=None):
        """End the run with this exit code, once. The first decision stands."""
        if self.finished:
            return
        self.exit_code = code
        if message:
            self.host.emit({"modes": False, "error": message})
        self.host.finish(code)

    def stop(self):
        self.fail(CLEAN)

    def handle_line(self, line):
        """One stdin line. Blank lines and stray whitespace say nothing."""
        text = line.strip()
        if not text or self.finished:
            return
        verb, _, rest = text.partition(" ")
        rest = rest.strip()
        self.command(verb, rest if rest else None)


# ---- Reading command arguments. Each returns None for anything it does not
#      accept, and a brand ignores a command whose argument came back None:
#      BRIDGE.md says a command the device cannot take sends nothing.

def choice(arg, options):
    return arg if arg is not None and arg in options else None


def number(arg, low, high):
    """A whole number in [low, high], written as plain digits; else None."""
    if arg is None:
        return None
    text = arg.strip()
    if not text.isdigit():
        return None
    value = int(text)
    return value if low <= value <= high else None


def switch(arg):
    """'on' -> True, 'off' -> False, anything else None."""
    return {"on": True, "off": False}.get(arg)


ADDRESS = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def address(text):
    """A Bluetooth address, upper-cased, or SetupError. Checked before any
    of it reaches BlueZ or a child process's command line."""
    if not ADDRESS.match(str(text)):
        raise SetupError("not a Bluetooth address: %s" % text)
    return str(text).upper()
