# Tests

Two tiers: **offline** tests are fast, deterministic, and need no credentials —
run them on every change. **Live** tests fork a real pty and drive vibeSH
against the `agent-sdk` backend, so they need a Claude Code login and run slowly
(each spawns the model and takes ~1–3 min). Run those by hand when touching the
interactive paths.

## Offline (no API key, no network, no tty needed)

```
uv run python test_smoke.py                  # end-to-end loop against a scripted fake model (24 checks)
uv run python test_units.py                  # REPL logic units: parser, prompts, keys, completion, snapshots, … (37 checks)
uv run python tests/test_completion_mock.py  # Tab completion end to end against a mock OpenAI server
```

`test_smoke.py` and `test_units.py` live at the repo root (they `import vibesh`
directly). `tests/test_completion_mock.py` stands up a local mock server, so it's
still offline.

## Live (needs `claude` login; slow — run manually)

```
uv run python tests/live_vim.py            # vim: alt screen, insert, :wq, edit consistency
uv run python tests/live_top.py            # top self-refresh via [TICK]
uv run python tests/live_typeahead.py      # keys typed during a repaint aren't lost
uv run python tests/live_echo.py           # speculative local echo latency (optional feature)
uv run python tests/live_completion.py     # Tab completion round trip on agent-sdk
uv run python tests/live_sigquit.py        # Ctrl+\ hard-kill mid-turn
uv run python tests/live_preset.py         # --preset boots into a character (C64)
```

`tests/_pty.py` is the shared pty harness (`Session` + `report`).
