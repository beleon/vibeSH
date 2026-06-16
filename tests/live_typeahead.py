#!/usr/bin/env python3
"""LIVE integration (needs a Claude Code login; slow). Keystrokes typed while the
model is mid-repaint must not be lost — persistent raw mode buffers them as
typeahead for the next batch. Type 'ihello ' then 'world' during the repaint and
the saved file must read 'hello world'.
Run: uv run python tests/live_typeahead.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pty import Session, report  # noqa: E402

s = Session("--backend", "agent-sdk")
s.drain(25)
s.send(b"vim hello.txt\r"); s.drain(20)
s.send(b"ihello ")               # batch 1: enter insert mode, type "hello "
time.sleep(1.5)                  # model is mid-repaint...
s.send(b"world")                 # ...type more (used to be eaten)
s.drain(25)
s.send(b"\x1b:wq\r"); s.drain(20)
s.send(b"cat hello.txt\r"); s.drain(20)
s.send(b"exit\r"); s.drain(10)
t = s.text()
s.kill()
report("live-typeahead", [
    ("keys typed during repaint not lost", "hello world" in t),
    ("saved file has the full text", t.count("hello world") >= 2),
    ("terminal restored after vim", "\x1b[?1049l" in t),
])
