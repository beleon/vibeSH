#!/usr/bin/env python3
"""Offline smoke test: drives the full REPL loop against a scripted fake model.

Run with:  uv run python test_smoke.py
Exercises: [BOOT], prompt/input, ANSI chunks, delay/cps pacing, yield/[CONTINUE],
malformed-line degradation, plain-text drift, ooc, exit — no API key needed.
"""

import contextlib
import io
import os
import sys
from unittest import mock

os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-for-smoke-test")

import vibesh  # noqa: E402

# Scripted model responses, consumed in order.  inside the JSON strings is
# decoded by json.loads exactly as it would be coming off the real API.
RESPONSES = [
    # [BOOT]
    '{"type": "chunk", "text": "vibebox login: user (automatic login)\\n\\n"}\n'
    '{"type": "chunk", "text": "Welcome to Vibian GNU/Linux 13 (whimsical)\\n", "delay": 5}\n'
    '{"type": "prompt", "text": "user@vibebox:~$ ", "echo": true}\n',
    # "ls" — includes color, a malformed JSON line, and a plain-text drift line
    '{"type": "chunk", "text": "\\u001b[01;34mDocuments\\u001b[0m  notes.txt\\n", "cps": 2000}\n'
    '{"type": "chunk", "broken json here\n'
    'this line is schema drift\n'
    '{"type": "prompt", "text": "user@vibebox:~$ ", "echo": true}\n',
    # "ping example.com" — yields
    '{"type": "chunk", "text": "64 bytes from 93.184.216.34: icmp_seq=1 ttl=56 time=11.2 ms\\n", "delay": 5}\n'
    '{"type": "yield"}\n',
    # [CONTINUE] — second burst, then (for the test) the command ends with a prompt
    '{"type": "chunk", "text": "64 bytes from 93.184.216.34: icmp_seq=2 ttl=56 time=11.4 ms\\n", "delay": 5}\n'
    '{"type": "prompt", "text": "user@vibebox:~$ ", "echo": true}\n',
    # "@ai become solaris" — two events run together on ONE line (no newline
    # between them) plus a leaked-reasoning prose prefix: exactly how small local
    # models break the wire. Both events must still be extracted and played.
    'thinking: the user wants solaris.{"type": "ooc", "text": "ok — Solaris 2.5.1, hostname gravity."}'
    '{"type": "prompt", "text": "gravity% ", "echo": true}\n',
    # "vim hello.txt" — alternate screen, a speculate map, then keys-mode prompt.
    # The speculate payload must NOT render (echo only fires on a real tty).
    '{"type": "chunk", "text": "\\u001b[?1049h\\u001b[H~\\n\\"hello.txt\\" [New File]"}\n'
    '{"type": "speculate", "keys": {"i": "SPECULATIVE_ECHO_MARKER"}}\n'
    '{"type": "prompt", "text": "", "mode": "keys"}\n',
    # [KEYS "ihello"] — repaint, stay in keys mode
    '{"type": "chunk", "text": "\\u001b[Hhello"}\n'
    '{"type": "prompt", "text": "", "mode": "keys"}\n',
    # [KEYS ":wq"] — leave alternate screen, back to line mode
    '{"type": "chunk", "text": "\\u001b[?1049l"}\n'
    '{"type": "prompt", "text": "gravity% ", "echo": true}\n',
    # "./refused.sh" — model returns an EMPTY turn (a refusal): nothing rendered,
    # no terminal event. The REPL must surface it, not silently re-prompt.
    '',
    # "exit"
    '{"type": "chunk", "text": "logout\\n"}\n'
    '{"type": "exit", "code": 0}',  # note: no trailing newline on purpose
]

sent_messages = []


class FakeStream:
    def __init__(self, text):
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        # dribble in small pieces to exercise the line-buffering path
        for i in range(0, len(self.text), 7):
            yield self.text[i : i + 7]

    def get_final_message(self):
        return mock.Mock(stop_reason="end_turn")


class FakeMessages:
    def __init__(self):
        self.responses = list(RESPONSES)

    def stream(self, **kwargs):
        sent_messages.append(kwargs["messages"][-1]["content"])
        return FakeStream(self.responses.pop(0))


class FakeClient:
    def __init__(self):
        self.messages = FakeMessages()


def main():
    stdin = io.StringIO("ls\n\nping example.com\n@ai become solaris\n"
                        "vim hello.txt\nihello\n:wq\n./refused.sh\nexit\n")
    stdout = io.StringIO()
    code = None
    with mock.patch.object(vibesh.anthropic, "Anthropic", FakeClient), \
         mock.patch.object(sys, "argv", ["vibesh"]), \
         mock.patch.object(sys.stdin, "isatty", lambda: False), \
         contextlib.redirect_stdout(stdout):
        # route input() through our fake stdin, echoing nothing (pipe semantics)
        def fake_input(prompt=""):
            sys.stdout.write(prompt)
            line = stdin.readline()
            if line == "":
                raise EOFError
            return line.rstrip("\n")

        with mock.patch("builtins.input", fake_input):
            try:
                vibesh.main()
            except SystemExit as e:
                code = e.code

    output = stdout.getvalue()
    checks = [
        ("exit code 0", code == 0),
        ("boot banner shown", "Welcome to Vibian" in output),
        ("ANSI color decoded to real ESC", "\x1b[01;34mDocuments\x1b[0m" in output),
        ("schema-drift line rendered raw", "this line is schema drift" in output),
        ("broken JSON line dropped silently", '"broken json' not in output),
        ("ping burst 1 played", "icmp_seq=1" in output),
        ("ping burst 2 played after [CONTINUE]", "icmp_seq=2" in output),
        ("ooc rendered dim", "\x1b[2;3mok — Solaris" in output),
        ("solaris prompt used", "gravity% " in output),
        ("two events run together on one line both parsed",
         "\x1b[2;3mok — Solaris" in output and "gravity% " in output),
        ("leaked reasoning prose before JSON rendered raw",
         "thinking: the user wants solaris." in output),
        ("alternate screen entered", "\x1b[?1049h" in output),
        ("alternate screen left", "\x1b[?1049l" in output),
        ("speculate payload not rendered as output", "SPECULATIVE_ECHO_MARKER" not in output),
        ("keys batch sent as [KEYS]", '[KEYS "ihello"]' in sent_messages),
        ("second keys batch sent", '[KEYS ":wq"]' in sent_messages),
        ("empty/refused turn surfaced, not silent",
         "the machine returned nothing" in output),
        ("refused turn re-prompts (control handed back)",
         "./refused.sh" in sent_messages),
        ("logout shown before exit", "logout" in output),
        ("[BOOT] sent first", sent_messages[0] == "[BOOT]"),
        ("user input forwarded verbatim", "ls" in sent_messages and "ping example.com" in sent_messages),
        ("[CONTINUE] sent after yield", "[CONTINUE]" in sent_messages),
        ("@ai forwarded verbatim", "@ai become solaris" in sent_messages),
        ("empty line NOT sent to model", "" not in sent_messages),
    ]
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(("PASS" if ok else "FAIL"), "-", name)
    if failed:
        print("\n--- captured output ---")
        print(repr(output))
        print("\n--- sent messages ---", sent_messages)
        sys.exit(1)
    print(f"\nall {len(checks)} checks passed")


if __name__ == "__main__":
    main()
