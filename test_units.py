#!/usr/bin/env python3
"""Offline unit tests for vibeSH's REPL logic — fast, deterministic, no API key,
no network, no tty. Covers the pieces test_smoke.py's end-to-end pass doesn't isolate:
the NDJSON parser, prompt rendering, keys-mode reader, completion, snapshots, etc.

Run with:  uv run python test_units.py
"""
import io
import json
import os
import re
import signal
import tempfile
import types
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-for-tests")

import vibesh  # noqa: E402

FAILS = []


def ok(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        FAILS.append(name)


# --- drain_events: the streaming NDJSON parser -------------------------------
def feed(pieces):
    buf, out = "", []
    for p in pieces:
        buf += p
        evs, buf = vibesh.drain_events(buf, final=False)
        out += evs
    evs, _ = vibesh.drain_events(buf, final=True)
    return out + evs


def test_drain_events():
    line = 'User sent EOF.{"type":"ooc","text":"Logout"}{"type":"exit","code":0}\n'
    ok("drain: prose + run-together objects (the dump bug)",
       [e.get("type") for e in feed([line])] == ["chunk", "ooc", "exit"])
    ok("drain: dribbled across object boundaries",
       [e.get("type") for e in feed([line[i:i+5] for i in range(0, len(line), 5)])]
       == ["chunk", "ooc", "exit"])
    ok("drain: clean multi-line NDJSON",
       [e.get("type") for e in feed(['{"type":"chunk","text":"hi\\n"}\n{"type":"prompt","text":"$ "}\n'])]
       == ["chunk", "prompt"])
    pretty = feed(['{\n  "type": "chunk",\n  "text": "ok"\n}\n'])
    ok("drain: pretty-printed JSON decodes", len(pretty) == 1 and pretty[0]["text"] == "ok")
    ok("drain: broken line dropped, following event survives",
       [e.get("type") for e in feed(['{"type":"chunk","oops unterminated\n{"type":"prompt","text":"$ "}\n'])]
       == ["prompt"])


# --- wrap_prompt: readline-safe colored prompts ------------------------------
def test_wrap_prompt():
    p = "\x1b[32muser@vibebox\x1b[0m \x1b[36m~\x1b[0m> "
    w = vibesh.wrap_prompt(p)
    ok("wrap: every escape gets \\001..\\002", w.count("\x01") == 4 and w.count("\x02") == 4)
    ok("wrap: strips back to the original (terminal sees color)",
       w.replace("\x01", "").replace("\x02", "") == p)
    ok("wrap: markers wrap only escapes",
       all(re.fullmatch(r"\x1b\[[0-9;?]*[A-Za-z]", s) for s in re.findall(r"\x01(.*?)\x02", w)))
    ok("wrap: plain prompt untouched", vibesh.wrap_prompt("user@vibebox:~$ ") == "user@vibebox:~$ ")
    saved = vibesh.readline
    vibesh.readline = None
    ok("wrap: no-op without readline (markers would corrupt the tty)", vibesh.wrap_prompt(p) == p)
    vibesh.readline = saved


# --- prompt_loop: double-prompt dedup + local quit ---------------------------
def drive_prompt(prompt_ev, returns, tail=""):
    it = iter(returns)
    shown = []
    def fake_read(s, e):
        shown.append(s)
        return next(it)
    with mock.patch.object(vibesh, "read_user_line", fake_read), \
         mock.patch.object(vibesh, "RAW", mock.Mock()), \
         mock.patch.object(vibesh, "out", lambda t: None):
        result = vibesh.prompt_loop(prompt_ev, tail=tail)
    return shown, result


def test_prompt_dedup():
    shown, res = drive_prompt({"text": "[sudo] password for user: ", "echo": False},
                              ["pw"], tail="...\n[sudo] password for user: ")
    ok("dedup: duplicated sudo prompt not re-printed", shown == [""] and res == "pw")
    shown, _ = drive_prompt({"text": "$ ", "echo": True}, ["ls"], tail="out\n")
    ok("dedup: normal prompt shown once", shown == ["$ "])
    shown, res = drive_prompt({"text": "pw: ", "echo": False}, ["", "x"], tail="pw: ")
    ok("dedup: re-prompt restores full text", shown == ["", "pw: "] and res == "x")


def test_local_quit():
    def quits(cmd, echo=True):
        try:
            drive_prompt({"text": "$ ", "echo": echo}, [cmd])
            return False
        except SystemExit as e:
            return e.code == 0
    ok("@exit/@quit quit locally (any case/space)",
       all(quits(c) for c in ("@exit", "@quit", "  @EXIT ", "@Quit")))
    ok("normal commands incl. 'exit' pass through",
       drive_prompt({"text": "$ ", "echo": True}, ["ls"])[1] == "ls"
       and drive_prompt({"text": "$ ", "echo": True}, ["exit"])[1] == "exit")
    ok("@exit in a password field is treated as input, not a quit",
       drive_prompt({"text": "pw: ", "echo": False}, ["@exit"])[1] == "@exit")


# --- read_keys_batch: raw key reader, timeout/TICK, local echo ---------------
def drive_keys(prompt_ev, selects, reads, speculation=None):
    sel, rd, written = iter(selects), iter(reads), []
    with mock.patch.object(vibesh, "RAW", mock.Mock()), \
         mock.patch.object(vibesh.sys.stdin, "isatty", lambda: True), \
         mock.patch.object(vibesh.sys.stdin, "fileno", lambda: 7), \
         mock.patch.object(vibesh.select, "select", lambda *a, **k: next(sel)), \
         mock.patch.object(vibesh.os, "read", lambda *a, **k: next(rd)), \
         mock.patch.object(vibesh, "out", lambda t: written.append(t)):
        sig = vibesh.read_keys_batch(prompt_ev, speculation)
    return sig, "".join(written)


def test_keys_batch():
    READY, IDLE = ([7], [], []), ([], [], [])
    ok("keys: timeout with no key -> [TICK]",
       drive_keys({"mode": "keys", "timeout": 2000}, [IDLE], [])[0] == "[TICK]")
    ok("keys: timeout but a key waiting -> flushed as [KEYS]",
       drive_keys({"mode": "keys", "timeout": 2000}, [READY, IDLE], [b"q"])[0] == '[KEYS "q"]')
    ok("keys: no timeout blocks then batches",
       drive_keys({"mode": "keys"}, [IDLE], [b"i"])[0] == '[KEYS "i"]')
    ok("keys: Ctrl+] -> [FORCE-QUIT]",
       drive_keys({"mode": "keys"}, [IDLE], [b"\x1d"])[0] == "[FORCE-QUIT]")
    seen = {}
    def cap(rl, wl, xl, t):
        seen["t"] = t
        return IDLE
    with mock.patch.object(vibesh, "RAW", mock.Mock()), \
         mock.patch.object(vibesh.sys.stdin, "isatty", lambda: True), \
         mock.patch.object(vibesh.sys.stdin, "fileno", lambda: 7), \
         mock.patch.object(vibesh.select, "select", cap):
        r = vibesh.read_keys_batch({"mode": "keys", "timeout": 9_999_999})
    ok("keys: huge timeout clamped to REFRESH_CAP", r == "[TICK]" and seen["t"] == vibesh.REFRESH_CAP)
    spec = {":": "<C>", ":q": "<CQ>", ":q!": "<CQB>"}
    ok("echo: per-key cumulative deltas painted",
       drive_keys({"mode": "keys"}, [READY, READY, IDLE], [b":", b"q", b"!"], spec)
       == ('[KEYS ":q!"]', "<C><CQ><CQB>"))
    ok("echo: fast multi-byte read shows the final delta only",
       drive_keys({"mode": "keys"}, [IDLE], [b":q!"], spec) == ('[KEYS ":q!"]', "<CQB>"))
    ok("echo: a miss paints nothing, still batches",
       drive_keys({"mode": "keys"}, [IDLE], [b"x"], spec) == ('[KEYS "x"]', ""))


# --- snapshots ---------------------------------------------------------------
def test_snapshots():
    msgs = [{"role": "user", "content": "[BOOT]"},
            {"role": "assistant", "content": '{"type":"prompt","text":"$ "}'}]
    lp = {"text": "user@vibebox:~$ ", "echo": True}
    path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    vibesh.save_snapshot(path, msgs, lp)
    m2, lp2 = vibesh.load_snapshot(path)
    ok("snapshot: round-trips messages + last_prompt", m2 == msgs and lp2 == lp)
    with open(path, "w") as f:
        json.dump({"format": "nope", "messages": []}, f)
    try:
        vibesh.load_snapshot(path)
        bad = False
    except ValueError:
        bad = True
    ok("snapshot: wrong format rejected", bad)
    os.unlink(path)


# --- presets -----------------------------------------------------------------
def test_presets():
    ok("preset: named directive resolves", "Solaris" in vibesh.PRESETS.get("solaris", ""))
    free = "a machine that only speaks French"
    ok("preset: unknown name used verbatim as a directive", vibesh.PRESETS.get(free, free) == free)


# --- completion: request, grammar swap, gating, key resolution ---------------
class FakeStreamBackend:
    """Minimal backend: records the messages it was handed, streams a fixed reply."""
    def __init__(self, reply, grammar=None):
        self.reply, self.grammar, self.seen, self.grammar_during = reply, grammar, None, None
    def stream_text(self, system, messages):
        self.seen, self.grammar_during = messages, self.grammar
        for i in range(0, len(self.reply), 8):
            yield self.reply[i:i+8]


def test_completion():
    real = [{"role": "user", "content": "[BOOT]"}]
    be = FakeStreamBackend('{"type":"complete","candidates":["checkout","cherry-pick"]}')
    before = list(real)
    cands = vibesh.request_completions(be, "SYS", real, "git che")
    ok("completion: parses candidates", cands == ["checkout", "cherry-pick"])
    ok("completion: real history not mutated, temp list sent", real == before and be.seen is not real)
    ok("completion: sends the [COMPLETE] signal", be.seen[-1]["content"] == '[COMPLETE "git che"]')
    gb = FakeStreamBackend('{"type":"complete","candidates":["x"]}', grammar="ORIG")
    vibesh.request_completions(gb, "S", real, "g")
    ok("completion: grammar swapped to completion grammar during call, restored after",
       gb.grammar_during == vibesh.COMPLETION_GRAMMAR and gb.grammar == "ORIG")
    drift = FakeStreamBackend('{"type":"chunk","text":"oops"}{"type":"prompt","text":"$ "}')
    ok("completion: drift -> no candidates, no raise", vibesh.request_completions(drift, "S", real, "x") == [])

    comp = vibesh.Completer(FakeStreamBackend('{"type":"complete","candidates":["checkout","status"]}'), "S", real)
    with mock.patch.object(vibesh.readline, "get_line_buffer", lambda: "git che"):
        got = [comp.complete("che", 0), comp.complete("che", 1)]
    ok("completer: filters to candidates extending the typed text", got == ["checkout", None])

    class Agent:   holds_history = True;  grammar = None
    class GramLoc: holds_history = False; grammar = "g"
    class Plain:   holds_history = False; grammar = None
    installed = []
    with mock.patch.object(vibesh.readline, "set_completer", lambda f: installed.append(f)), \
         mock.patch.object(vibesh.readline, "set_completer_delims", lambda d: None), \
         mock.patch.object(vibesh.readline, "parse_and_bind", lambda b: None):
        for b in (Agent(), GramLoc(), Plain()):
            vibesh.install_completer(b, "S", real)
    ok("completion: installs on every backend (agent-sdk + grammar no longer gated)", len(installed) == 3)


def test_key_resolution():
    captured = {}
    class FakeOpenAI:
        def __init__(self, base_url=None, api_key=None):
            captured["key"] = api_key
        chat = types.SimpleNamespace()
    import openai
    with mock.patch.object(openai, "OpenAI", FakeOpenAI):
        for var in ("VIBESH_API_KEY", "OPENAI_API_KEY"):
            os.environ.pop(var, None)
        vibesh.LocalBackend("m", 100, "http://x/v1")
        no_env = captured["key"]
        os.environ["OPENAI_API_KEY"] = "sk-openai"
        vibesh.LocalBackend("m", 100, "http://x/v1")
        openai_key = captured["key"]
        os.environ["VIBESH_API_KEY"] = "sk-or-abc"
        vibesh.LocalBackend("m", 100, "http://x/v1")
        vibesh_key = captured["key"]
    for v in ("VIBESH_API_KEY", "OPENAI_API_KEY"):
        os.environ.pop(v, None)
    ok("key: dummy when no env; OPENAI_API_KEY; VIBESH_API_KEY wins",
       no_env == "sk-local" and openai_key == "sk-openai" and vibesh_key == "sk-or-abc")


def test_hard_quit_installed():
    if hasattr(signal, "SIGQUIT"):
        vibesh.install_hard_quit()
        ok("hard-quit: SIGQUIT handler installed", callable(signal.getsignal(signal.SIGQUIT)))
    else:
        ok("hard-quit: skipped (no SIGQUIT on this platform)", True)


def main():
    for fn in (test_drain_events, test_wrap_prompt, test_prompt_dedup, test_local_quit,
               test_keys_batch, test_snapshots, test_presets,
               test_completion, test_key_resolution, test_hard_quit_installed):
        fn()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED: {FAILS}")
        raise SystemExit(1)
    print("\nall unit checks passed")


if __name__ == "__main__":
    main()
