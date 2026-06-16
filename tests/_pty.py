"""Shared helper for vibeSH pty-driven integration tests.

A `Session` forks vibeSH under a pseudo-terminal so the real raw-mode keyboard
path, readline, and rendering run for real — the only way to exercise keys mode,
local echo, colored prompts, and signals end to end.
"""
import os
import pty
import select
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Session:
    def __init__(self, *args):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child: become vibeSH
            os.chdir(REPO)
            os.execvp("uv", ["uv", "run", "python", "vibesh.py"] + list(args))
        self.buf = b""

    def _read(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return True
        try:
            d = os.read(self.fd, 65536)
        except OSError:
            return False
        if not d:
            return False
        self.buf += d
        return True

    def drain(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if not self._read(0.1):
                return

    def send(self, data):
        os.write(self.fd, data)

    def wait_for(self, needle, limit):
        """Seconds until `needle` next appears, or None. Useful for latency checks."""
        start, mark = time.time(), len(self.buf)
        while time.time() - start < limit:
            if not self._read(0.05):
                break
            if needle in self.buf[mark:]:
                return time.time() - start
        return None

    def text(self):
        return self.buf.decode("utf-8", "replace")

    def kill(self):
        try:
            os.kill(self.pid, 9)
        except ProcessLookupError:
            pass

    def wait_exit(self, limit=4.0):
        """Return (exited, exitcode|None) — polls waitpid up to `limit` seconds."""
        end = time.time() + limit
        while time.time() < end:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid == self.pid:
                return True, (os.WEXITSTATUS(status) if os.WIFEXITED(status) else None)
            time.sleep(0.2)
        return False, None


def report(name, checks):
    """checks: list of (label, bool). Print + exit nonzero on any failure."""
    fails = [label for label, okk in checks if not okk]
    for label, okk in checks:
        print(("PASS" if okk else "FAIL"), "-", label)
    print(f"\n{name}: {'all passed' if not fails else str(len(fails)) + ' FAILED'}")
    sys.exit(1 if fails else 0)
