#!/usr/bin/env python3
"""LIVE integration (needs a Claude Code login; slow). Tab completion on the
agent-sdk backend: typing 'git chec' + Tab triggers a real [COMPLETE] round trip
(seconds, not an instant self-inserted tab) and completes the line.
Run: uv run python tests/live_completion.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pty import Session, report  # noqa: E402

s = Session("--backend", "agent-sdk")
s.drain(30)
s.send(b"git chec")
s.drain(1.5)
mark = len(s.buf)
s.send(b"\t")                       # Tab
latency = s.wait_for(b"k", 8.0)     # completion of 'chec' -> 'check...' inserts a 'k'
s.drain(3)
after = s.buf[mark:].decode("utf-8", "replace")
s.send(b"\x03"); s.drain(1)         # Ctrl-C to clear the line
s.send(b"exit\r"); s.drain(3)
s.kill()
round_trip = latency is not None and latency > 0.5  # a model call, not self-insert
print(f"time-to-completion = {latency*1000:.0f} ms" if latency else "no completion")
print("inserted:", repr(after[:60]))
report("live-completion", [
    ("Tab triggered a model round trip, not an instant self-insert", round_trip),
])
