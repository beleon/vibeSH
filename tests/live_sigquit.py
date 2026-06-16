#!/usr/bin/env python3
"""LIVE integration (needs a Claude Code login; slow). Ctrl+\\ (SIGQUIT) hard-kill:
sent WHILE the model streams a response (not at a prompt), it must terminate the
process immediately and cleanly. Run: uv run python tests/live_sigquit.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pty import Session, report  # noqa: E402

s = Session("--backend", "agent-sdk")
s.drain(30)                                          # boot
s.send(b"find / -name '*.so' 2>/dev/null | sort\r")  # kick off a turn
time.sleep(0.6)                                      # let it start streaming
s.send(b"\x1c")                                      # Ctrl+\ mid-turn
s.drain(5)
exited, code = s.wait_exit(6.0)
t = s.text()
s.kill()
report("live-sigquit", [
    ("quit notice shown", "[vibesh: quit]" in t),
    ("process exited mid-turn", exited),
    ("clean exit code 0", code == 0),
])
