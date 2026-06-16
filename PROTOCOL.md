# vibeSH Protocol v0.1

vibeSH is a hallucinated shell: a thin REPL in front of an LLM that role-plays an
entire Linux machine. Nothing is real. There is no filesystem, no processes, no network —
the model imagines all of it, and the session's conversation history is the machine's
only state. Close the session and the machine is gone. If the context window overflows,
the machine gets amnesia; that's accepted (and canon: this box has unreliable RAM).

This document specifies the wire protocol between the REPL (the renderer) and the model
(the machine).

## Transport

The model's response is **NDJSON**: one JSON event per line, nothing else — no markdown,
no code fences, no prose outside events. The REPL stream-parses as text arrives and plays
each event the moment it is complete, so output begins before the response finishes.

Newlines are the *intended* separator but not load-bearing: the REPL extracts events by
decoding JSON objects greedily, so a model that runs objects together (`{...}{...}` with
no newline) or leaks stray prose between them still works — the objects are parsed, the
prose is rendered as raw output. This tolerance exists for weaker local models; the
model should still emit clean one-per-line NDJSON.

Every response MUST end with exactly one terminal event: `prompt`, `yield`, or `exit`.

JSON has no `\x` escape: the ESC character in `text` fields must be encoded as
`\u001b` (e.g. `"\u001b[31mred\u001b[0m"`).

## Events (model → REPL)

### `chunk` — terminal output

```json
{"type": "chunk", "text": "Receiving objects:  38% (94512/248716)\r", "delay": 120, "cps": 0}
```

- `text` (required): raw terminal output, exactly what the machine would write to the
  tty. Includes ANSI escape codes — colors, `\r`, line clears, cursor movement,
  `\x1b[2J` to clear the screen. There are no separate events for visual effects;
  everything visual lives in `text`.
- `delay` (optional, default `0`): milliseconds to pause **before** printing this chunk.
  Use for progress-bar ticks, boot sequences, anything that needs rhythm.
- `cps` (optional, default `0` = instant): characters-per-second typewriter rate for
  this chunk. Use for long flowing output (compiler scroll, log replay) instead of
  emitting one event per line — one big chunk plus a rate is far cheaper in tokens.

Implementation note: when typewriting (`cps > 0`), the REPL emits ANSI escape sequences
atomically — never character-by-character inside an escape sequence.

### `prompt` — hand control to the user

```json
{"type": "prompt", "text": "user@vibebox:~$ ", "echo": true, "mode": "line"}
```

- `text` (required, may be empty): whatever the machine is currently waiting with. This
  is usually the shell PS1, but not always — a Python REPL ends its turn with `">>> "`,
  apt with `"Do you want to continue? [Y/n] "`, `cat` reading stdin with `""`. Nested
  interactive programs need no extra machinery; they are just different prompts.
- `echo` (optional, default `true`): when `false`, the REPL reads input without echoing
  (sudo/ssh password prompts).
- `mode` (optional, default `"line"`): `"keys"` switches the REPL to raw keyboard
  input for full-screen programs (`vim`, `less`, `top`). The REPL collects one burst
  of keystrokes (until ~250ms of idle, or Enter) and sends it as a `[KEYS "..."]`
  signal. In raw mode Ctrl+C is just a byte (`0x03`) forwarded inside the batch —
  the program decides what it means; Ctrl+] is reserved by the REPL and sends
  `[FORCE-QUIT]`. The model leaves keys mode by simply emitting its next prompt
  without `"mode": "keys"`.
- `timeout` (optional, keys mode only): milliseconds to wait for the first keystroke.
  If it elapses with no input, the REPL sends `[TICK]` instead of `[KEYS ...]` so the
  model can repaint a self-refreshing program (`top`, `htop`, `watch`). Omit it for
  programs that idle silently (`vim`, `less`) — without it the wait is unbounded. The
  REPL caps the effective wait (60s) so a huge value can't wedge the terminal.

The model owns the prompt string entirely. `cd` changes the path in it, `su` turns `$`
into `#`, an `@ai` directive may change the hostname. The REPL never composes a prompt.

### `yield` — command still running, request continuation

```json
{"type": "yield"}
```

Ends the response without handing control to the user. The REPL finishes playback, then
calls the model again (sending `[CONTINUE]`, see below) for the next burst. This is the
primitive for:

- infinite commands — `ping`, `tail -f`: emit a dozen lines with incrementing
  seq/timestamps, yield, repeat until the user interrupts;
- output longer than one response — a kernel build's scroll, chained across turns;
- refreshing programs — `top`: frame, yield, redraw codes, next frame.

### `ooc` — out-of-character text

```json
{"type": "ooc", "text": "ok — this is now a SPARCstation 5 running Solaris 2.5.1. root password: root"}
```

The director's voice, used to acknowledge `@ai` directives (and nothing else). Rendered
visibly distinct from terminal output (dim/italic), so the fiction and the meta channel
never blend. Plain text, no markdown.

### `speculate` — local-echo predictions (keys mode)

```json
{"type": "speculate", "keys": {":": "\u001b[24;1H\u001b[K:", ":q": "\u001b[24;1H\u001b[K:q"}}
```

Sent alongside a keys-mode `prompt`. `keys` maps a *cumulative* keystroke sequence (from
the current frame) to the full bytes that should be on screen after it. The REPL paints a
match the instant the user reaches it, so typing isn't blind during the round trip. Purely
cosmetic and authoritative-loss-free: every keystroke is still sent as `[KEYS ...]`, and
the model's next full repaint overwrites whatever was echoed — a wrong or absent guess
just self-corrects. Each value is a complete delta from the current frame (not relative to
a shorter guess), so lookups are independent and a multi-byte read can't corrupt the
screen. Predicting the *outcome* of executing (the Enter after `:q!`) is deliberately out
of scope — only the visible typing is echoed; execution round-trips. Valid only for the
prompt it accompanies; the next turn must resend or drop it.

### `complete` — Tab-completion candidates

```json
{"type": "complete", "candidates": ["checkout", "cherry-pick"]}
```

The sole reply to a `[COMPLETE "<line>"]` signal (below). `candidates` are full
replacements for the last whitespace-separated token of the line, each beginning with
what's already typed. It is a *side query*, not a command: the model emits this one event
and nothing else — no output, no prompt, no state change — and the REPL does not persist
the exchange, so pressing Tab never alters the machine. The REPL feeds the candidates to
readline, which inserts the common prefix and lists alternatives like a real shell.

### `exit` — the machine ends the session

```json
{"type": "exit", "code": 0}
```

`exit`, `shutdown -h now`, or the machine dying after `rm -rf /`. Any farewell output is
sent as `chunk`s before this event. The REPL terminates with `code` (optional,
default `0`).

## Input (REPL → model)

- **User input** is sent verbatim as a user message. One command per turn; the model
  produces that command's output and ends with a terminal event.
- **`@ai <directive>`** — the escape hatch. A line starting with `@ai` is an
  out-of-character instruction about the simulation itself ("make this box a Solaris
  machine from 1996", "the system only has the bare minimum of binaries, plus apt").
  The model applies it, acknowledges via `ooc`, and continues; the directive stays in
  history, so it keeps applying.
- **REPL signals** are bracketed messages the REPL sends on its own:
  - `[BOOT]` — first message of every session: emit a brief boot/login (banner, MOTD)
    and the first prompt.
  - `[CONTINUE]` — playback of a yielded burst finished without interruption; continue
    the running command. Also sent when a response was cut off by the token limit.
  - `[SIGINT after: "<last chunk played>"]` — user pressed Ctrl+C; playback was halted
    at that point. Respond in fiction: `^C`, exit status 130, fresh prompt.
  - `[EOF]` — user pressed Ctrl+D at a prompt. Respond as the machine would (logout,
    exit the nested REPL, ...).
  - `[RESIZE 120x40]` — terminal size changed (sent with the next turn).
  - `[COMPLETE "<line>"]` — the user pressed Tab in line mode; the model replies with a
    single `complete` event (above) of candidates for the last token. On the stateless
    backends (anthropic/local) the REPL builds it from history but does **not** append
    it, so completion never changes machine state; on agent-sdk (which keeps history
    server-side) it is recorded, but harmlessly — the model is told it's a no-op side
    query. Under `--grammar`, the REPL swaps in a completion-only grammar for this one
    request so the constrained model can still answer.
  - `[KEYS "<batch>"]` — one burst of raw keystrokes, sent only while the last prompt
    had `"mode": "keys"`. The batch is a JSON string: control bytes and ESC are
    escaped (`"ihello world\u001b:wq\r"`), so arrow keys arrive as `"\u001b[A"` etc.
    Respond with whatever the screen does — usually a full repaint.
  - `[FORCE-QUIT]` — user pressed Ctrl+] in keys mode: the REPL's own escape hatch
    for a stuck program. Kill the foreground program unceremoniously (as if it
    crashed), restore the normal screen, return a line-mode shell prompt.
  - `[TICK]` — a keys-mode prompt's `timeout` elapsed with no keystroke. Repaint the
    next frame of a self-refreshing program (advance the clock, gauges, process list)
    and end with another keys-mode prompt. This is `yield` for full-screen programs:
    the REPL keeps ticking until the user types or quits.

  (A user could type a bracketed signal by hand to spoof one. It's a toy; that's fine.)

## REPL responsibilities

- Stream-parse NDJSON, play events in order, honor `delay`/`cps`.
- Read input with the model-supplied prompt, honoring `echo`.
- Intercept the local quit commands `@exit` / `@quit` (line mode, echoed input only)
  and exit immediately — never forwarded to the model, so the user can bail even when
  the model is wedged. (In-fiction `exit` still goes to the model as normal input.)
- Handle `Ctrl+\` (SIGQUIT) as a hard-kill: restore the terminal, then terminate
  immediately — works mid-turn while the model streams, when there's no prompt to type
  at. (Cooked/line mode only; in keys mode `Ctrl+\` is a raw byte for the program.)
- Catch Ctrl+C **during playback**, halt the animation immediately, report via
  `[SIGINT ...]`. This is also what makes `yield` safe — interrupt is how every
  infinite command ends.
- Tell the model the real terminal size (`COLUMNS`/`LINES`) in the system prompt, and
  send `[RESIZE ...]` when it changes — progress bars and column layout must fit the
  actual terminal to be convincing.
- **Degrade gracefully on malformed events**: skip the bad line, or print its `text`
  raw if salvageable. Models drift out of schemas, and raw escape codes inside JSON
  strings are exactly where they fumble. Never crash the machine over a typo in the
  dream.
- **Surface empty turns.** A response that renders nothing and carries no terminal
  event is almost always a refusal (the model declined the command), occasionally an
  API hiccup. Don't silently reprint the prompt — that makes the machine look broken.
  Show a brief out-of-band notice, then hand control back at the last prompt. (A
  refusal often "sticks": the tainted context keeps producing empty turns until the
  session is restarted.)
- In keys mode (`"mode": "keys"`): hold the tty in raw mode for the **entire**
  interaction — across model turns and repaints, not just while reading — and restore
  it only at the next line-mode prompt (or on exit). Keys typed while the screen is
  catching up are typeahead: they wait in the kernel queue and become the next
  `[KEYS ...]` batch, never lost, never echoed into the frame. Batch keystrokes by
  idle gap (~250ms) or Enter, JSON-escape the batch, forward Ctrl+C as a byte,
  reserve Ctrl+] for `[FORCE-QUIT]`. (Consequence: during keys-mode playback Ctrl+C
  is typeahead for the program, not a REPL interrupt — vim semantics.) The REPL does no
  screen handling of its own — what the keys *do* is the model's business — and the only
  local echo it performs is replaying the model's own `speculate` predictions (below).
  If the prompt carries a `timeout`, bound the wait for the first key by it (capped) and
  send `[TICK]` when it elapses — that, plus the persistent raw mode, is the entire
  mechanism behind self-refreshing programs. If a `speculate` map accompanied the prompt,
  after each read look up the cumulative keystrokes and paint a match immediately; still
  send every key as `[KEYS ...]` — the echo is cosmetic, the model's repaint authoritative.
- Stay dumb. The REPL tracks no machine state — no cwd, no files, no processes. All
  state lives in the model's context.
- Two pragmatic exceptions to dumbness, both to avoid wasting a model turn: an empty
  input line or Ctrl+C **at a prompt** re-displays the same prompt locally (like a
  real shell) instead of being sent to the model.

## Model responsibilities (system-prompt contract, summarized)

- Output only what the terminal would show; never break character except via `ooc` in
  response to `@ai`.
- Boot as a generic fresh Linux box (until directed otherwise via `@ai`).
- Invent plausible details freely (kernel version, usernames, IPs, file contents) and
  then stay consistent with everything earlier in the session.
- Lazy world generation: nothing exists until observed. `git clone` produces clone
  output, not a source tree; a file's contents are invented when first `cat`-ed and
  remembered thereafter.
- Full-screen programs (`vim`, `less`, `top`): enter the alternate screen
  (`\u001b[?1049h`), paint a full frame sized to the real terminal, and end with a
  keys-mode prompt. Each `[KEYS ...]` turn answers with a **full repaint** (home
  cursor + redraw — diffs against an imagined screen corrupt too easily). On quit,
  leave the alternate screen (`\u001b[?1049l`) and return a line-mode prompt.
  Keystroke turns are slow and the user types in bursts — apply the whole batch,
  then show the resulting screen. Never refuse, never explain.

## Example exchange

```
user:  git clone --depth 1 https://git.kernel.org/.../linux.git

model: {"type": "chunk", "text": "Cloning into 'linux'...\n"}
       {"type": "chunk", "text": "remote: Enumerating objects: 92301, done.\n", "delay": 400}
       {"type": "chunk", "text": "Receiving objects:  21% (19384/92301), 38.11 MiB | 12.40 MiB/s\r", "delay": 300}
       {"type": "chunk", "text": "Receiving objects:  64% (59073/92301), 121.52 MiB | 12.71 MiB/s\r", "delay": 600}
       {"type": "chunk", "text": "Receiving objects: 100% (92301/92301), 240.18 MiB | 12.66 MiB/s, done.\n", "delay": 500}
       {"type": "chunk", "text": "Resolving deltas: 100% (12490/12490), done.\n", "delay": 300}
       {"type": "prompt", "text": "user@vibebox:~$ "}

user:  @ai make this box a Solaris machine from 1996

model: {"type": "ooc", "text": "ok — SPARCstation 5, Solaris 2.5.1, hostname 'gravity'. The linux clone is still on disk."}
       {"type": "prompt", "text": "gravity% "}
```

## Out of scope for v0.1

- Persistence of any kind (no shadow filesystem, no saved worlds).
- Local echo / client-side prediction in keys mode (keystrokes appear only after the
  model's repaint — embrace the modem lag).
- Context-overflow management beyond "the machine forgets".

## Enforcing the format on weak models

The wire format is a contract the model is *asked* to follow; the REPL tolerates
violations but cannot prevent them. For local models that drift badly, the format can be
*enforced* at the decoder with a GBNF grammar (`event_grammar.gbnf`, opt-in via
`--grammar`): it permits only a stream of event objects ending in exactly one terminal
event, so prose leaks and malformed JSON become impossible to generate. This is
llama.cpp-specific (llama-server's `grammar` request field) and orthogonal to the
protocol itself — the wire format is unchanged; generation is just constrained to it.
