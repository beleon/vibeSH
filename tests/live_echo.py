#!/usr/bin/env python3
"""LIVE integration (needs a Claude Code login; slow). Speculative local echo:
typing ':' in vim should paint instantly (sub-400ms) from the model's prediction,
versus seconds for a round trip. Speculation is optional, so a slow result is
reported, not failed — but the vim flow must stay intact.
Run: uv run python tests/live_echo.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pty import Session, report  # noqa: E402

s = Session("--backend", "agent-sdk")
s.drain(25)
s.send(b"vim echo.txt\r"); s.drain(20)   # frame (+ maybe a speculate map)
s.send(b":")
latency = s.wait_for(b":", 8.0)
s.send(b"\x1b:q!\r"); s.drain(20)
s.send(b"exit\r"); s.drain(10)
t = s.text()
s.kill()
flow_ok = "\x1b[?1049h" in t and "\x1b[?1049l" in t
echoed = latency is not None and latency < 0.4
print(f"time-to-':' = {latency*1000:.0f} ms" if latency is not None else "':' never appeared")
if not echoed and latency is not None:
    print("NOTE - model didn't speculate ':' this run (optional); flow intact")
report("live-echo", [
    ("':' eventually rendered", latency is not None),
    ("vim alt-screen flow intact", flow_ok),
])
