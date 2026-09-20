# Contributing

## Setup

```bash
scripts/fetch-forge.sh     # ~300 MB, once
java/build.sh
uv sync
uv run gauntlet doctor
```

`docs/INSTALL.md` has the detail and the common failures.

## Before you open a pull request

```bash
uv run --python 3.12 --with ruff ruff check src/ tests/
uv run --python 3.12 --with ruff ruff format src/ tests/
uv run --python 3.12 --with pytest python -m pytest -q
```

Then play one real game end to end. A change that passes the tests and breaks an
actual match has happened more than once, because most of what can go wrong
lives in the gap between the JVM and Python.

```bash
uv run gauntlet run --a <deck> --b <deck> --seat-a forge --seat-b forge --games 3
```

## The invariants

These are in `CLAUDE.md` in full. The short version:

1. **Magic's rules are Forge's.** A card that behaves wrongly is an upstream
   bug. Never special-case a card here.
2. **A seat never stalls the game, and a fallback is never disguised as a play.**
3. **An exhausted seat stops the run** rather than quietly finishing it with
   Forge's AI.
4. **Hidden information stays hidden.**
5. **The protocol is versioned and both sides check it.** `VERSION` in
   `protocol.py` and `PROTOCOL_VERSION` in `Bridge.java` must match.
6. **Forge is pinned and never vendored into git.**
7. **The bridge is GPLv3, the Python is MIT, and they talk over a socket.** Do
   not collapse that boundary.

## Style

Comments explain why, never what. A comment restating the code is worse than
none. Every entry in the failure-mode table in `CLAUDE.md` is there because it
cost someone an hour, and new ones belong there too.

Tests are named for what they assert. A regression test says so in a comment,
with what broke.

## Adding a decision kind

Four places, in order, names matching exactly: `PlayerControllerGauntlet`,
`protocol.KINDS`, `match.DEFAULT_ROUTED`, `prompt.py`. `docs/EXTENDING.md` walks
through it.
