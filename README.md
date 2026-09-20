# Gauntlet

Play Magic: The Gathering games between two agents, fast, with a transcript of
every decision and the reasoning behind it.

The rules are Forge's. This repo is the harness around it: it seats agents,
routes the decisions that need judgment, keeps the ones that do not inside the
engine, and writes down what happened.

## What it is for

Testing a theory about a deck. Does this commander want more ramp, does that
combo line actually assemble, does the curve work. A win-rate tells you a deck
lost. The transcript tells you why, in the seat's own words.

## Quick start

```bash
scripts/fetch-forge.sh          # downloads and pins Forge, once, ~300 MB
java/build.sh                   # builds the bridge against it

gauntlet decks                  # decks available from the collection database
gauntlet run --a "Feather Storm" --b "Temur Roar" --seat-b forge
gauntlet replay <match-id>
```

## The three kinds of seat

| Seat | Who decides | Use it for |
|---|---|---|
| `forge` | Forge's own AI | the control arm, thousands of games, a baseline to beat |
| `api` | the Claude API, unattended | hundreds of games, statistical answers |
| `interactive` | an agent calling `gauntlet act` | one game, close reading, a specific line |

They are interchangeable per seat. Agent versus Forge AI is the normal way to
check a deck before spending agent time on it.

## Playing a game as an agent

Two agents, one seat each. Each loops on a single command that submits the last
decision and blocks for the next one.

```bash
gauntlet act --match m7 --seat A                        # first call
gauntlet act --match m7 --seat A --choice 1 --why "..." # answer, wait for next
```

The `--why` is not decoration. It is the thing the transcript is for.

## Layout

```
src/gauntlet/     the harness: protocol, seats, transcript, CLI
java/             the Forge bridge (GPLv3, links against Forge)
vendor/           the pinned Forge release, not in git
docs/             the agent-facing contract, read this before playing
```

`ARCHITECTURE.md` explains why Forge and not XMage or a rules engine of our own,
and how the pieces fit.

## Licensing

The Python harness is MIT. The Java bridge under `java/` links against Forge and
is GPLv3-or-later, matching it. The two talk over a socket and that separation
is deliberate. Forge itself is downloaded, never vendored into git.
