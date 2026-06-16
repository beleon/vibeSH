#!/usr/bin/env python3
"""vibeSH — a hallucinated shell.

A thin REPL in front of an LLM that role-plays an entire Linux machine.
Nothing is real; the session's conversation history is the machine's only state.
See PROTOCOL.md for the wire protocol.
"""

import argparse
import getpass
import json
import os
import re
import select
import shutil
import signal
import sys
import time

import anthropic

try:
    import readline  # line editing + history for input(); also drives Tab completion
except ImportError:
    readline = None

_HERE = os.path.dirname(os.path.abspath(__file__))
SYSTEM_PROMPT_FILE = os.path.join(_HERE, "system_prompt.md")
DEFAULT_GRAMMAR_FILE = os.path.join(_HERE, "event_grammar.gbnf")
DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
TERMINAL_EVENTS = {"prompt", "yield", "exit"}
HISTORY_LIMIT = 200  # messages kept; beyond this the machine gets amnesia (canon)

DIM = "\x1b[2;3m"
RESET = "\x1b[0m"

DEBUG = False  # --debug: print per-turn token usage (set in main)

# One ANSI escape sequence, or any single character. Used to typewrite without
# splitting escape sequences.
ANSI_OR_CHAR = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b.|.", re.DOTALL)


def out(text):
    sys.stdout.write(text)
    sys.stdout.flush()


# Boot presets: a named (or free-text) @ai directive applied at first boot, so
# you can drop straight into an exotic machine. Unknown names are used verbatim.
PRESETS = {
    "solaris": "This box is a Sun SPARCstation 5 running Solaris 2.5.1, circa 1996. "
               "csh-style '%' prompt, hostname 'gravity', SunOS userland (not GNU).",
    "c64": "This box is a Commodore 64 sitting at its power-on BASIC V2 screen: "
           "40-column uppercase PETSCII, light-blue on dark-blue, 'READY.' and a "
           "blinking cursor. Commands are BASIC (PRINT, LOAD, LIST, RUN), not Unix.",
    "amiga": "This box is a Commodore Amiga 500 running AmigaOS 1.3 in an AmigaShell. "
             "'1>' prompt, AmigaDOS commands (dir, list, type, cd), not Unix.",
    "mainframe": "This box is an IBM System/370 running MVS, accessed over a 3270 "
                 "terminal session circa 1985: uppercase, TSO/ISPF, JCL, datasets.",
    "haunted": "This box is an ordinary Linux machine that is subtly haunted: clocks "
               "drift, files you didn't write appear, processes you didn't start show "
               "in ps, and the occasional message addresses the user by name. Never "
               "break character about it.",
    "failing-disk": "This is an ordinary Linux box whose primary disk is physically "
                    "dying. Commands intermittently fail with I/O errors, dmesg shows "
                    "ata exceptions and reallocated sectors, some files come back "
                    "corrupted — and it slowly gets worse over the session.",
}


class Playback:
    """Plays events and remembers the tail of what was shown (for [SIGINT after])."""

    def __init__(self):
        self.tail = ""
        self.terminal = None  # first prompt/yield/exit event seen this response
        self.speculation = {}  # keys-mode local-echo predictions for this turn

    def note(self, text):
        self.tail = (self.tail + text)[-80:]

    def play(self, ev):
        etype = ev.get("type")
        if etype in TERMINAL_EVENTS:
            if self.terminal is None:
                self.terminal = ev
            return
        if etype == "chunk":
            delay = ev.get("delay") or 0
            if delay:
                time.sleep(min(delay, 10_000) / 1000)
            text = ev.get("text", "")
            cps = ev.get("cps") or 0
            if cps > 0:
                interval = 1.0 / min(max(cps, 1), 100_000)
                for token in ANSI_OR_CHAR.findall(text):
                    out(token)
                    if not token.startswith("\x1b"):
                        time.sleep(interval)
            else:
                out(text)
            self.note(text)
        elif etype == "ooc":
            out(f"{DIM}{ev.get('text', '')}{RESET}\n")
        elif etype == "speculate":
            # keys-mode local echo: a map of {typed-sequence: bytes to show}.
            # Not rendered now — handed to the keys reader for instant echo.
            keys = ev.get("keys")
            if isinstance(keys, dict):
                self.speculation = {k: v for k, v in keys.items() if isinstance(v, str)}
        # unknown event types: skip (graceful degradation)


_DECODER = json.JSONDecoder()


def drain_events(buf, final):
    """Pull complete events off the front of a streaming buffer.

    Returns (events, leftover). NDJSON is the happy path, but small models break
    the wire in two ways we must survive: they run objects together with no
    newline between them (`{...}{...}`), and they leak prose/reasoning around the
    JSON. So we don't trust newlines as boundaries — we greedily decode each JSON
    object with raw_decode and render any non-JSON run as raw drift.

    `final` is True on the last call (stream closed): a trailing JSON fragment is
    then known to be broken and dropped, rather than held as still-incomplete.
    """
    events = []
    s = buf
    while True:
        s = s.lstrip()  # whitespace between events is non-semantic
        if not s:
            return events, ""
        if s[0] == "{":
            try:
                obj, end = _DECODER.raw_decode(s)
            except json.JSONDecodeError:
                # A valid object (even pretty-printed) decodes fine — raw_decode
                # treats newlines as whitespace. So a failure here is genuinely
                # broken JSON. If a line boundary follows, the object was meant to
                # end there: drop just that bad line and recover (don't let an
                # unterminated string swallow the good events after it).
                nl = s.find("\n")
                if nl != -1:
                    s = s[nl + 1:]
                    continue
                if final:
                    return events, ""  # broken tail, no newline: drop it
                return events, s       # incomplete object: wait for more
            if isinstance(obj, dict):
                events.append(obj)
            s = s[end:]
        else:
            brace = s.find("{")
            if brace == -1:  # prose with no following object yet
                nl = s.find("\n")
                if nl == -1:
                    if final:
                        events.append({"type": "chunk", "text": s + "\n"})
                        return events, ""
                    return events, s  # partial prose (or partial brace): wait
                events.append({"type": "chunk", "text": s[: nl + 1]})
                s = s[nl + 1:]
            else:  # prose inline before an object (e.g. a reasoning leak)
                seg = s[:brace]
                if seg.strip():
                    events.append({"type": "chunk", "text": seg})
                s = s[brace:]


class AnthropicBackend:
    """Streams via the Anthropic API (ANTHROPIC_API_KEY, pay-per-token)."""

    default_model = "claude-sonnet-4-6"

    def __init__(self, model, max_tokens, base_url=None, grammar=None):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("vibesh: ANTHROPIC_API_KEY is not set", file=sys.stderr)
            sys.exit(1)
        self.client = anthropic.Anthropic()
        self.model = model or self.default_model
        self.max_tokens = max_tokens
        self.errors = (anthropic.APIError,)
        self.stop_reason = None
        self.usage = None

    def stream_text(self, system, messages):
        self.usage = None
        # Top-level auto-caching: marks the last block each turn, so every
        # request reads the previous turn's cache (~0.1x input price) and the
        # stable system prompt + history never re-bill at full rate.
        with self.client.messages.stream(
            model=self.model, max_tokens=self.max_tokens, system=system, messages=messages,
            cache_control={"type": "ephemeral"},
        ) as stream:
            yield from stream.text_stream
            msg = stream.get_final_message()
            self.stop_reason = msg.stop_reason
            u = msg.usage
            self.usage = {
                "in": u.input_tokens,
                "out": u.output_tokens,
                "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
                "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
            }


class LocalBackend:
    """Streams via any OpenAI-compatible endpoint — a local server (llama.cpp
    llama-server, Ollama, LM Studio) or a hosted provider (OpenRouter, Mistral,
    DeepSeek, Groq, ...). Point --base-url at it; the API key comes from
    VIBESH_API_KEY (or OPENAI_API_KEY), defaulting to a dummy for local servers
    that don't check it. Reasoning arrives in a separate `reasoning_content` field
    we ignore — only `content` reaches the tty."""

    default_model = "local"

    def __init__(self, model, max_tokens, base_url, grammar=None):
        import openai  # lazy: only required for --backend local

        api_key = (os.environ.get("VIBESH_API_KEY")
                   or os.environ.get("OPENAI_API_KEY")
                   or "sk-local")  # local servers ignore the key
        self.client = openai.OpenAI(base_url=base_url, api_key=api_key)
        self.model = model or self.default_model
        self.max_tokens = max_tokens
        self.grammar = grammar  # GBNF text; forces valid NDJSON (llama.cpp only)
        self.errors = (openai.OpenAIError,)
        self.stop_reason = None
        self.usage = None

    def stream_text(self, system, messages):
        self.usage = None
        kwargs = {}
        if DEBUG:
            kwargs["stream_options"] = {"include_usage": True}
        if self.grammar:
            # llama.cpp llama-server honors a top-level `grammar` field (GBNF);
            # the OpenAI client forwards unknown fields via extra_body.
            kwargs["extra_body"] = {"grammar": self.grammar}
        stream = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            stream=True,
            messages=[{"role": "system", "content": system}] + messages,
            **kwargs,
        )
        finish = None
        try:
            for chunk in stream:
                if getattr(chunk, "usage", None):  # final chunk when include_usage
                    self.usage = {"in": chunk.usage.prompt_tokens,
                                  "out": chunk.usage.completion_tokens}
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.delta and choice.delta.content:
                    yield choice.delta.content
                if choice.finish_reason:
                    finish = choice.finish_reason
        finally:
            stream.close()
        self.stop_reason = "max_tokens" if finish == "length" else "end_turn"


class AgentSDKBackend:
    """Streams via the Claude Agent SDK, i.e. through your Claude Code login —
    billed against a Pro/Max subscription's Agent SDK credit, no API key needed.

    Unlike the other backends, the SDK holds the conversation itself (it drives
    a persistent Claude Code session), so only the newest user input is sent;
    the REPL's `messages` list is ignored except for its last entry.
    """

    default_model = None  # use the Claude Code session default
    holds_history = True   # SDK owns the conversation; no throwaway side requests

    def __init__(self, model, max_tokens, base_url=None, grammar=None):
        import asyncio
        import threading

        import claude_agent_sdk

        self._sdk = claude_agent_sdk
        self._asyncio = asyncio
        self.model = model
        self.errors = (claude_agent_sdk.ClaudeSDKError,)
        self.stop_reason = None
        self.usage = None
        self._client = None
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()

    def _run(self, coro):
        return self._asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _connect(self, system):
        options = self._sdk.ClaudeAgentOptions(
            system_prompt=system,      # full replacement — no Claude Code persona
            tools=[],                  # the machine is imaginary; no real tools
            model=self.model,
            max_turns=1,               # one response per input, like a tty
            include_partial_messages=True,
            setting_sources=[],        # don't load CLAUDE.md etc. into the dream
        )
        self._client = self._sdk.ClaudeSDKClient(options=options)
        await self._client.connect()

    async def _turn(self, system, prompt, q):
        if self._client is None:
            await self._connect(system)
        await self._client.query(prompt)
        async for message in self._client.receive_response():
            err = getattr(message, "error", None)  # e.g. model_not_found, surfaced as
            if err:                                 # a synthetic message, not a delta
                detail = ""
                for blk in getattr(message, "content", None) or []:
                    detail = getattr(blk, "text", "") or detail
                raise self._sdk.ClaudeSDKError(detail or str(err))
            ev = getattr(message, "event", None)
            if ev and ev.get("type") == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta":
                    q.put(delta["text"])
            u = getattr(message, "usage", None)  # final ResultMessage carries usage
            if isinstance(u, dict):
                self.usage = {
                    "in": u.get("input_tokens", 0),
                    "out": u.get("output_tokens", 0),
                    "cache_read": u.get("cache_read_input_tokens", 0),
                    "cache_write": u.get("cache_creation_input_tokens", 0),
                }

    def stream_text(self, system, messages):
        import queue

        self.usage = None
        q = queue.Queue()
        done = object()
        fut = self._run(self._turn(system, messages[-1]["content"], q))

        def finished(f):
            # .exception() raises on a cancelled future (Ctrl+C path) — treat as done
            q.put(done if f.cancelled() else (f.exception() or done))

        fut.add_done_callback(finished)
        try:
            while True:
                item = q.get()
                if item is done:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            if not fut.done():  # interrupted mid-turn: stop the SDK side too
                if self._client is not None:
                    self._run(self._client.interrupt())
                try:
                    fut.result(timeout=5)  # let the turn wind down cleanly
                except Exception:
                    fut.cancel()
        self.stop_reason = "end_turn"


BACKENDS = {"anthropic": AnthropicBackend, "local": LocalBackend, "agent-sdk": AgentSDKBackend}

SNAPSHOT_FORMAT = "vibesh-snapshot-1"


def save_snapshot(path, messages, last_prompt):
    """Persist the machine: its whole state is the conversation, so this is just
    the message list plus where it left off. Atomic + never fatal."""
    snap = {"format": SNAPSHOT_FORMAT, "messages": messages, "last_prompt": last_prompt}
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, path)
    except Exception as e:
        out(f"{DIM}[vibesh: snapshot save failed: {e}]{RESET}\n")


def load_snapshot(path):
    """Restore (messages, last_prompt) from a snapshot file."""
    with open(path) as f:
        snap = json.load(f)
    if snap.get("format") != SNAPSHOT_FORMAT:
        raise ValueError(f"not a {SNAPSHOT_FORMAT} file")
    messages = snap["messages"]
    last_prompt = snap.get("last_prompt") or {"text": "$ ", "echo": True}
    return messages, last_prompt


def stream_turn(backend, system, messages, playback):
    """Stream one model response, playing events as they complete.

    Returns (raw_text, stop_reason). KeyboardInterrupt propagates after the
    stream is closed; raw-so-far is recoverable via playback.raw.
    """
    raw = []
    playback.raw = raw
    buf = ""
    for piece in backend.stream_text(system, messages):
        raw.append(piece)
        buf += piece
        events, buf = drain_events(buf, final=False)
        for ev in events:
            playback.play(ev)
    events, _ = drain_events(buf, final=True)  # flush the tail
    for ev in events:
        playback.play(ev)
    return "".join(raw), backend.stop_reason


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[@-Z\\-_]")

LOCAL_QUIT = {"@exit", "@quit"}  # REPL-local exit hatch: quits without a model turn


def install_hard_quit():
    """Ctrl+\\ (SIGQUIT) → exit immediately, no model turn — the bail-out for a wedged
    model mid-stream, when you're not at a prompt to type @exit. This is a *hard* kill:
    os._exit() terminates from inside the handler regardless of what the main thread is
    blocked on (a clean SystemExit can't unwind past the agent-sdk's background loop).
    We restore the terminal first so it isn't left in a broken state.
    Cooked/line mode only — in keys mode Ctrl+\\ is a raw byte (ISIG is off), as a
    full-screen program expects."""
    if not hasattr(signal, "SIGQUIT"):
        return  # not POSIX (e.g. Windows)

    def handler(signum, frame):
        RAW.exit()
        out(f"\n{DIM}[vibesh: quit]{RESET}\n")
        os._exit(0)

    signal.signal(signal.SIGQUIT, handler)


def wrap_prompt(text):
    """Wrap ANSI escapes in a readline prompt with \\001…\\002 so readline counts
    only the visible width — otherwise a colored prompt corrupts the cursor math on
    history recall, backspace, and line wrap. The markers are readline-internal and
    never reach the terminal, so the color still shows. No-op without readline."""
    if readline is None or "\x1b" not in text:
        return text
    return ANSI_ESCAPE.sub(lambda m: "\x01" + m.group(0) + "\x02", text)


def read_user_line(prompt_text, echo):
    """Read one line at the machine's prompt. Returns the line, '[EOF]', or None
    on Ctrl+C (caller re-prompts locally, like a real shell)."""
    if echo and "\x1b" in prompt_text:
        # safety net: a colored prompt that forgot to reset would bleed its color
        # into everything the user types (and the output after). Force a reset.
        prompt_text += "\x1b[0m"
    try:
        if echo:
            return input(wrap_prompt(prompt_text))  # color shows; width stays correct
        return getpass.getpass(prompt_text)
    except EOFError:
        return "[EOF]"
    except KeyboardInterrupt:
        out("^C\n")
        return None


# GBNF that forces a single `complete` event — used for completion requests on a
# grammar-constrained local backend, where the normal grammar would forbid it.
COMPLETION_GRAMMAR = (
    'root    ::= "{\\"type\\": \\"complete\\", \\"candidates\\": [" items? "]}"\n'
    'items   ::= string ("," string)*\n'
    'string  ::= "\\"" char* "\\""\n'
    'char    ::= [^"\\\\\\x7F\\x00-\\x1F] | "\\\\" (["\\\\bfnrt/] | "u" hex hex hex hex)\n'
    'hex     ::= [0-9a-fA-F]\n'
)


def request_completions(backend, system, messages, line):
    """One throwaway round trip asking the model to complete the current line.

    Sent as a `[COMPLETE "<line>"]` side request built from a temporary list, so
    the REPL's own history is never touched. Returns the candidate list from the
    model's `complete` event, or []. Renders nothing — it runs while readline owns
    the line. On a grammar-constrained backend we swap in a completion-only grammar
    for the call (the normal one only allows terminal-ending event streams).
    """
    temp = messages + [{"role": "user", "content": f"[COMPLETE {json.dumps(line)}]"}]
    saved = getattr(backend, "grammar", None)
    if saved is not None:
        backend.grammar = COMPLETION_GRAMMAR
    try:
        raw = "".join(backend.stream_text(system, temp))
    finally:
        if saved is not None:
            backend.grammar = saved
    events, _ = drain_events(raw, final=True)
    for ev in events:
        if ev.get("type") == "complete" and isinstance(ev.get("candidates"), list):
            return [str(c) for c in ev["candidates"]]
    return []


class Completer:
    """readline completer that asks the model what comes next — only on Tab, so
    it costs nothing unless the user reaches for it. All other line editing
    (history, arrows, search) is readline's untouched default."""

    def __init__(self, backend, system, messages):
        self.backend = backend
        self.system = system
        self.messages = messages  # live reference: run_loop mutates this in place
        self.matches = []

    def complete(self, text, state):
        if state == 0:
            self.matches = self._fetch(text)
        return self.matches[state] if state < len(self.matches) else None

    def _fetch(self, text):
        try:
            line = readline.get_line_buffer()
            cands = request_completions(self.backend, self.system, self.messages, line)
        except Exception:
            return []  # never disturb the prompt over a failed completion
        # readline replaces `text` with the match, so a match must extend it
        return [c for c in cands if c.startswith(text)]


def install_completer(backend, system, messages):
    """Wire Tab to model-driven completion. Works on every backend: stateless ones
    (anthropic/local) send a throwaway request that never touches history; agent-sdk
    holds history server-side, so its `[COMPLETE]` does get recorded — but the model
    is told it's a no-op side query, so that's benign (just extra context)."""
    if readline is None:
        return
    readline.set_completer(Completer(backend, system, messages).complete)
    readline.set_completer_delims(" \t\n")  # complete the whole last token (incl. paths)
    # GNU readline uses "tab: complete"; libedit builds need "bind ^I rl_complete".
    binding = "bind ^I rl_complete" if "libedit" in (readline.__doc__ or "") else "tab: complete"
    try:
        readline.parse_and_bind(binding)
    except Exception:
        pass


KEY_IDLE = 0.25  # seconds of keyboard silence that closes a [KEYS] batch
FORCE_QUIT = "\x1d"  # Ctrl+] — the REPL's reserved escape key in raw mode


class RawMode:
    """Raw keyboard input for the whole keys-mode interaction — entered at the
    first key read and held across model turns and repaints, so keystrokes typed
    while the screen is catching up become typeahead for the next batch instead
    of being eaten (or echoed into the frame) by the cooked-mode line discipline.

    Unlike tty.setraw, output processing (OPOST) is left on: the model's frames
    use \\n and must still render as CRLF.
    """

    def __init__(self):
        self.saved = None

    def enter(self):
        if self.saved is not None or not sys.stdin.isatty():
            return
        import termios

        fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(fd)
        attrs = termios.tcgetattr(fd)
        attrs[0] &= ~(termios.IXON | termios.ICRNL | termios.INLCR
                      | termios.ISTRIP | termios.BRKINT)
        attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSADRAIN, attrs)

    def exit(self):
        if self.saved is None:
            return
        import termios

        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.saved)
        self.saved = None


RAW = RawMode()


REFRESH_CAP = 60.0  # max seconds the REPL will idle-wait for a key before ticking


def echo_speculation(batch, speculation):
    """Local echo: if the cumulative keystrokes so far match a model prediction,
    paint it immediately so the user isn't typing blind during the round trip.

    Each value is the full visual delta from the current frame, so lookups are
    independent — a multi-byte read that skips intermediate prefixes still shows
    the right thing, and the model's authoritative repaint overwrites it anyway.
    """
    if speculation:
        delta = speculation.get(batch)
        if delta:
            out(delta)


def read_keys_batch(prompt_ev, speculation=None):
    """Key reader for full-screen programs (prompt "mode": "keys").

    Collects one burst of keystrokes — everything until ~250ms of idle, or Enter —
    and sends it as a single [KEYS "..."] turn. Ctrl+C is just a byte here and is
    forwarded to the program; Ctrl+] force-quits ([FORCE-QUIT]) if the machine
    is stuck. Raw mode persists after returning (see RawMode); the next line-mode
    prompt restores the terminal.

    If the prompt carries a `timeout` (ms), the wait for the *first* key is bounded:
    when it elapses with no input, we return [TICK] so the model can repaint a
    self-refreshing program (top, htop, watch). Keys typed during the model's
    repaint are buffered by the held raw mode and flushed on the next call.

    `speculation` (from a `speculate` event) drives local echo: matching keystrokes
    are painted instantly. It's purely cosmetic — every key is still sent to the
    model, whose full repaint is authoritative, so a wrong guess self-corrects.
    """
    text = prompt_ev.get("text", "")
    if text:
        out(text)
    timeout = prompt_ev.get("timeout")
    if not sys.stdin.isatty():
        # pipes/tests: no raw mode; one input line stands in for one key batch
        line = read_user_line("", True)
        if line == "[EOF]":
            return "[EOF]"
        return f"[KEYS {json.dumps(line or '')}]"

    RAW.enter()
    fd = sys.stdin.fileno()
    if timeout is not None:  # refresh-aware: wait at most `timeout` ms for a key
        wait = min(max(timeout, 0) / 1000, REFRESH_CAP)
        if not select.select([fd], [], [], wait)[0]:
            return "[TICK]"  # idle window elapsed; let the model repaint
    batch = os.read(fd, 1024).decode("utf-8", "replace")  # block for first key
    if not batch:
        return "[EOF]"
    echo_speculation(batch, speculation)
    while len(batch) < 4096 and not batch.endswith("\r"):
        ready, _, _ = select.select([fd], [], [], KEY_IDLE)
        if not ready:
            break
        batch += os.read(fd, 1024).decode("utf-8", "replace")
        echo_speculation(batch, speculation)
    if FORCE_QUIT in batch:
        return "[FORCE-QUIT]"
    return f"[KEYS {json.dumps(batch)}]"


def prompt_loop(prompt_ev, speculation=None, tail=""):
    """Prompt until we have something to send. Ctrl+C and empty lines re-prompt
    locally (like a real shell) instead of costing a model turn.

    `tail` is what was already rendered this turn: if the model both printed the
    prompt as a chunk AND set it as the prompt string (common for sudo password
    prompts), the screen already shows it, so we don't re-print it the first time.
    """
    if prompt_ev.get("mode") == "keys":
        return read_keys_batch(prompt_ev, speculation)
    RAW.exit()  # back to line mode: restore the cooked terminal
    text = prompt_ev.get("text", "")
    echo = prompt_ev.get("echo", True)
    first = True
    while True:
        shown = "" if (first and text and tail.endswith(text)) else text
        first = False
        line = read_user_line(shown, echo)
        if echo and line and line.strip().lower() in LOCAL_QUIT:
            # the REPL's own exit hatch — quit now, no model turn (the in-fiction
            # `exit` still works too, but this bails even if the model is wedged)
            out(f"{DIM}[vibesh: exited]{RESET}\n")
            sys.exit(0)
        if line:
            return line


def main():
    parser = argparse.ArgumentParser(description="vibeSH — a hallucinated shell")
    parser.add_argument("--backend", choices=sorted(BACKENDS),
                        default=os.environ.get("VIBESH_BACKEND", "anthropic"),
                        help="anthropic (API key) or local (OpenAI-compatible server; "
                             "or set VIBESH_BACKEND)")
    parser.add_argument("--model", default=os.environ.get("VIBESH_MODEL"),
                        help="model id (or set VIBESH_MODEL; default per backend)")
    parser.add_argument("--base-url", default=os.environ.get("VIBESH_BASE_URL", DEFAULT_BASE_URL),
                        help=f"OpenAI-compatible endpoint for --backend local: a local "
                             f"server or a hosted provider (OpenRouter, Mistral, ...). Set "
                             f"the key via VIBESH_API_KEY. (or VIBESH_BASE_URL; "
                             f"default {DEFAULT_BASE_URL})")
    parser.add_argument("--max-tokens", type=int, default=4096,
                        help="max tokens per response burst (default 4096)")
    parser.add_argument("--debug", action="store_true",
                        help="print per-turn token usage (cache_read > 0 from turn 2 "
                             "means prompt caching is working)")
    parser.add_argument("--grammar", nargs="?", const=DEFAULT_GRAMMAR_FILE, default=None,
                        metavar="PATH",
                        help="constrain output to valid NDJSON with a GBNF grammar "
                             "(local backend / llama.cpp only). Bare --grammar uses the "
                             "bundled event_grammar.gbnf; pass a path to override.")
    parser.add_argument("--save", metavar="PATH",
                        help="snapshot the machine to PATH after every turn "
                             "(not supported for --backend agent-sdk)")
    parser.add_argument("--load", metavar="PATH",
                        help="restore a machine snapshotted with --save and resume it")
    parser.add_argument("--preset", metavar="NAME",
                        help="boot straight into a preset machine — one of "
                             f"{', '.join(sorted(PRESETS))} — or any free-text directive")
    args = parser.parse_args()
    global DEBUG
    DEBUG = args.debug

    install_hard_quit()  # Ctrl+\ bails out even when the model is wedged mid-turn

    with open(SYSTEM_PROMPT_FILE) as f:
        system_template = f.read()

    grammar = None
    if args.grammar:
        if args.backend != "local":
            print(f"vibesh: --grammar only applies to the local backend, "
                  f"ignoring it for --backend {args.backend}", file=sys.stderr)
        else:
            with open(args.grammar) as f:
                grammar = f.read()

    if (args.save or args.load) and args.backend == "agent-sdk":
        # agent-sdk keeps the conversation server-side, so our message list isn't
        # the whole state — snapshots would be lossy. Refuse rather than mislead.
        print("vibesh: --save/--load aren't supported with --backend agent-sdk "
              "(it holds history server-side); use anthropic or local", file=sys.stderr)
        sys.exit(1)

    backend = BACKENDS[args.backend](args.model, args.max_tokens, args.base_url,
                                     grammar=grammar)
    size = shutil.get_terminal_size()
    # .replace, not .format — the template is full of JSON braces
    system = system_template.replace("{cols}", str(size.columns)).replace("{rows}", str(size.lines))

    messages = []
    last_prompt = {"text": "$ ", "echo": True}
    api_errors = 0

    if args.load:
        messages, last_prompt = load_snapshot(args.load)
        out(f"{DIM}[vibesh: restored snapshot from {args.load} "
            f"({len(messages)} messages)]{RESET}\n")

    # Tab completion needs the final `messages` reference (post-load) and must be
    # installed before any prompt is read.
    install_completer(backend, system, messages)

    if args.load:
        pending = prompt_loop(last_prompt, {})  # resume without a model turn
    elif args.preset:
        directive = PRESETS.get(args.preset, args.preset)
        pending = f"[BOOT]\n@ai {directive}"
    else:
        pending = "[BOOT]"

    try:
        run_loop(backend, system, messages, pending, last_prompt, api_errors, size,
                 save_path=args.save)
    finally:
        RAW.exit()  # never leave the user's terminal raw


def run_loop(backend, system, messages, pending, last_prompt, api_errors, size,
             save_path=None):
    empty_turns = 0  # consecutive turns that rendered nothing — bound the give-up
    while True:
        new_size = shutil.get_terminal_size()
        if new_size != size:
            size = new_size
            pending = f"[RESIZE {size.columns}x{size.lines}]\n{pending}"

        messages.append({"role": "user", "content": pending})
        if len(messages) > HISTORY_LIMIT:
            # amnesia: drop the oldest turns, keep alternation intact (start on a user turn)
            del messages[: len(messages) - HISTORY_LIMIT]
            while messages and messages[0]["role"] != "user":
                del messages[0]

        playback = Playback()
        try:
            raw, stop_reason = stream_turn(backend, system, messages, playback)
            interrupted = False
        except KeyboardInterrupt:
            raw, stop_reason = "".join(getattr(playback, "raw", [])), None
            interrupted = True
        except backend.errors as e:
            messages.pop()  # retract the unanswered user turn
            out(f"\n{DIM}[vibesh: API error: {e}]{RESET}\n")
            if getattr(backend, "grammar", None) and "parse" in str(e).lower():
                # A grammar parse 500 on a harmony model (gpt-oss) — the grammar
                # constrains the channel-control tokens the model must emit.
                out(f"{DIM}[vibesh: this is --grammar failing on the server. "
                    f"GBNF can't constrain harmony-format models like gpt-oss; drop "
                    f"--grammar (the parser tolerates drift) or use a non-harmony "
                    f"model (Qwen/Gemma/Llama).]{RESET}\n")
            api_errors += 1
            if api_errors >= 3:
                print("vibesh: giving up after 3 consecutive API errors", file=sys.stderr)
                sys.exit(1)
            pending = prompt_loop(last_prompt)
            continue
        api_errors = 0

        if DEBUG and backend.usage:
            stats = " ".join(f"{k}={v}" for k, v in backend.usage.items())
            out(f"{DIM}[{stats}]{RESET}\n")

        # keep history alternating even if the response was empty/interrupted early
        messages.append({"role": "assistant", "content": raw or '{"type": "chunk", "text": ""}'})

        if interrupted:
            tail = playback.tail.replace("\n", "\\n").replace('"', '\\"')
            pending = f'[SIGINT after: "{tail}"]'
            continue

        terminal = playback.terminal
        if terminal is None:
            if stop_reason == "max_tokens":
                empty_turns = 0
                pending = "[CONTINUE]"  # burst was cut off mid-command
                continue
            if DEBUG:
                # no terminal event: show exactly what the model sent, escapes
                # visible — the fast way to see a local model breaking schema
                out(f"{DIM}[raw response ({len(raw)} chars): {raw[:1000]!r}]{RESET}\n")
            if not raw.strip():
                # Nothing rendered and no terminal event: the model returned an
                # empty turn — almost always a refusal (it declined to play out
                # the command), sometimes an API hiccup. Without this, the REPL
                # just reprints the prompt and the machine looks silently broken.
                empty_turns += 1
                RAW.exit()
                out(f"{DIM}[vibesh: the machine returned nothing — likely a "
                    f"refusal. The session context may now be \"stuck\"; "
                    f"rephrase, or restart for a clean machine.]{RESET}\n")
                if empty_turns >= 3:
                    # a persistently empty model (bad --model, broken server) would
                    # otherwise spin forever, especially at EOF — give up.
                    print("vibesh: giving up after 3 consecutive empty responses "
                          "(bad --model or broken backend?)", file=sys.stderr)
                    sys.exit(1)
            else:
                empty_turns = 0  # drifted out of schema but did produce output
            terminal = last_prompt  # reuse the last prompt and hand control back
        else:
            empty_turns = 0

        etype = terminal.get("type", "prompt")
        if etype == "exit":
            sys.exit(int(terminal.get("code") or 0))
        if etype == "yield":
            pending = "[CONTINUE]"
            continue

        last_prompt = terminal
        if save_path:  # snapshot at the prompt: messages end on the assistant turn
            save_snapshot(save_path, messages, last_prompt)
        pending = prompt_loop(terminal, playback.speculation, tail=playback.tail)


if __name__ == "__main__":
    main()
