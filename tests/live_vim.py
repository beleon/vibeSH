#!/usr/bin/env python3
"""LIVE integration (needs a Claude Code login; slow). vim end to end: alternate
screen, insert, :wq, and edit consistency (cat shows the saved contents).
Run: uv run python tests/live_vim.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pty import Session, report  # noqa: E402

s = Session("--backend", "agent-sdk")
s.drain(25)                                   # boot
s.send(b"vim hello.txt\r"); s.drain(20)       # frame painted
s.send(b"ityped through a real pty"); s.drain(20)
s.send(b"\x1b:wq\r"); s.drain(20)             # ESC :wq -> back to shell
s.send(b"cat hello.txt\r"); s.drain(20)       # consistency: edit persisted
s.send(b"exit\r"); s.drain(10)
s.kill()
t = s.text()
report("live-vim", [
    ("entered the alternate screen", "\x1b[?1049h" in t),
    ("left the alternate screen on :wq", "\x1b[?1049l" in t),
    ("insert text shown", "typed through a real pty" in t),
    ("cat shows the saved edit (consistency)", t.count("typed through a real pty") >= 2),
])
