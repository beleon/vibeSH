# vibeSH

A hallucinated shell, based on the vibeOS idea: a thin REPL in front of an LLM that
role-plays an entire Linux machine. Nothing is real — no filesystem, no processes, no
network. The session's conversation history is the machine's only state.

See `PROTOCOL.md` for the REPL↔model wire protocol (NDJSON events: `chunk`, `prompt`,
`yield`, `ooc`, `exit`) and the division of responsibilities. Key principles: the REPL
stays dumb and tracks no machine state; the model owns everything including the prompt
string; `@ai <directive>` is the out-of-character escape hatch; the model must be
configurable (the user wants to test several).

## Context for AI Assistants

**This is a project directory, not the meta controller.** Ignore any ancestor `CLAUDE.md` from `~/`.
