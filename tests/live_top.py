#!/usr/bin/env python3
"""LIVE integration (needs a Claude Code login; slow). top self-refreshes via
[TICK]: with no input, the REPL hands control back to repaint successive frames.
Run: uv run python tests/live_top.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pty import Session, report  # noqa: E402

s = Session("--backend", "agent-sdk")
s.drain(25)                       # boot
s.send(b"top\r")
s.drain(45)                       # do NOT type — let the REPL tick the model
s.send(b"q"); s.drain(20)         # quit
s.send(b"exit\r"); s.drain(10)
s.kill()
t = s.text()
home_repaints = t.count("\x1b[H") + t.count("\x1b[1;1H")
report("live-top", [
    ("entered top's alternate screen", "\x1b[?1049h" in t),
    ("top-ish frame painted", any(m in t for m in ("top -", "load average", "Tasks:", "%Cpu"))),
    ("self-refreshed without input (>=2 frames)", home_repaints >= 2),
    ("left the alternate screen on q", "\x1b[?1049l" in t),
])
