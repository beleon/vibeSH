#!/usr/bin/env python3
"""LIVE integration (needs a Claude Code login; slow). --preset boots straight into
a character: confirm `--preset c64` comes up as a Commodore 64.
Run: uv run python tests/live_preset.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pty import Session, report  # noqa: E402

s = Session("--backend", "agent-sdk", "--preset", "c64")
s.drain(30)                       # boot into the C64
s.send(b'PRINT "HELLO"\r'); s.drain(20)
s.send(b"exit\r"); s.drain(10)
s.kill()
t = s.text().upper()
report("live-preset", [
    ("preset booted a C64", any(m in t for m in ("READY.", "COMMODORE", "BASIC", "PETSCII"))),
])
