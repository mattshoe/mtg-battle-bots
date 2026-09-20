# MTG Battle Bots

Play Magic: The Gathering games between AI agents, at speed, with a transcript
of every decision and the reasoning behind it.

The rules are [Forge](https://github.com/Card-Forge/forge)'s. This repo is the
harness around it: it seats the players, routes the decisions that need
judgment, keeps the ones that do not inside the engine, and writes down what
happened.

```
$ gauntlet sweep --deck feather-storm --against all --games 20

feather-storm over 460 games against 23 decks, 675s

opponent                   W-L-D   rate  turns
---------------------  ---------  -----  -----
chaos-incarnate            16-4-0    80%     20
quandrix-unlimited         15-5-0    75%     17
...
veloci-ramp-tor             4-16-0   20%     18

overall 247-213-0, 54% of decisive games
```

## What it is for

Testing a theory about a deck. Does this commander want more ramp, does that
combo line actually assemble, does the curve work.

A win rate tells you a deck lost. The transcript tells you why, in the seat's
own words, at the moment it still thought it was winning:

> **B** main1 · Scourge of Fleets
> *Every one of their lands is tapped and they hold one card, so this is a free
> window to land a 6/6. It triples my damage output and blocks a hasty Jetmir
> profitably, which the graveyard reanimation targets on offer cannot do.*

## How it works

```
  ┌─ agent ──────┐   ┌─ agent ──────┐
  │ gauntlet act │   │ gauntlet act │     one Bash call per decision
  └──────┬───────┘   └──────┬───────┘
         └──────────┬───────┘
         ┌──────────┴──────────┐
         │  gauntletd (Python) │              seats, transcript, policy
         └──────────┬──────────┘
                    │ newline-delimited JSON over loopback TCP
         ┌──────────┴──────────┐
         │  bridge (Java, GPL) │              PlayerControllerGauntlet
         └──────────┬──────────┘
         ┌──────────┴──────────┐
         │  Forge (pinned)     │              rules, 33,956 cards, game loop
         └─────────────────────┘
```

Forge asks a player thousands of questions per game and almost none of them are
interesting. The bridge answers the mundane ones itself and routes the ~30 per
game that need judgment out to a seat. That ratio is the whole design.

## The four kinds of seat

| Seat | Who decides | Speed | Use it for |
|---|---|---|---|
| `forge` | Forge's own AI | ~3s/game | the control arm, hundreds of games, a baseline to beat |
| `sdk` | Claude, via the Agent SDK | ~7min/game | real judgment, no API key, runs on existing Claude Code auth |
| `api` | Claude, via the API | ~2min/game | real judgment at volume, needs `ANTHROPIC_API_KEY` |
| `interactive` | an agent calling `gauntlet act` | you set the pace | one game, close reading, a specific line |

They are interchangeable per seat. Agent versus Forge AI is a valid match and
the normal way to sanity-check a deck before spending agent time on it.

## Quick start

```bash
scripts/fetch-forge.sh     # downloads and pins Forge, once, ~300 MB
java/build.sh              # builds the bridge against it
uv sync
uv run gauntlet doctor     # checks the install
```

Then play a game:

```bash
# Forge AI both sides, fast, free. Decks are a path to a text list,
# a path to a Forge .dck, or a slug from the collection database.
gauntlet run --a my-deck.txt --b their-deck.txt \
             --seat-a forge --seat-b forge --games 10

# Two agents, one close read
gauntlet run --a my-deck.txt --b their-deck.txt
gauntlet act --match <id> --seat A          # each agent loops on this
```

Full install notes are in [docs/INSTALL.md](docs/INSTALL.md).

## Playing as an agent

One command submits the last decision and blocks for the next one:

```bash
gauntlet act --match m7 --seat A                        # pick up the question
gauntlet act --match m7 --seat A --choice 1 --why "..." # answer, wait for next
```

The `--why` is not decoration, it is the product. An agent about to play should
read [docs/PLAYING.md](docs/PLAYING.md) first, which is the whole contract in
one page.

## Reading the result

```bash
gauntlet summary <match-id>   # wins, fallbacks, latency, per seat
gauntlet replay <match-id>    # play-by-play with every reasoning string
gauntlet matches              # recent matches
```

Everything is stored in SQLite and every match is reproducible from its seed,
decklists, policy, Forge version and bridge revision.

## Honesty guarantees

Properties this harness will not trade away, because a playtest result you
cannot trust is worse than no playtest.

**A fallback is never disguised as a play.** If a seat times out, errors, or
answers nonsense, Forge's AI decides and the transcript records `fallback` with
the reason. A run where the agent was asleep cannot be mistaken for one where it
played badly.

**An exhausted seat stops the run.** If a seat runs out of capacity — a session
limit, a quota — the match halts and the sweep cancels. It does not quietly
finish 460 games with Forge wearing the agent's name.

**Hidden information stays hidden.** Opponents' hands and libraries are sent as
counts. A seat that could see through them would make its results worthless as
evidence about a deck.

## Documentation

| Document | What is in it |
|---|---|
| [docs/PLAYING.md](docs/PLAYING.md) | The contract for an agent playing a game. Read this first if you are one. |
| [docs/INSTALL.md](docs/INSTALL.md) | Fresh clone to working install, and what breaks. |
| [docs/CLI.md](docs/CLI.md) | Every command and flag. |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | The wire format, precisely enough to write a third-party seat. |
| [docs/EXTENDING.md](docs/EXTENDING.md) | Adding decision kinds, seat types, deck sources. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Why Forge and not XMage, and how the pieces fit. |
| [CLAUDE.md](CLAUDE.md) | Maintainer notes, invariants, and every failure mode already hit. |

## Reports

`reports/` holds finished playtest runs. Each has a top-level `REPORT.md`, a
file per matchup, the decklists, the raw data, and the script that regenerates
it all.

## Licensing

The Python harness is MIT. The Java bridge under `java/` links against Forge and
is GPL-3.0-or-later, matching it. The two talk over a socket and that separation
is deliberate.

Forge itself is downloaded at install time, never vendored. Card names and rules
text are Wizards of the Coast's intellectual property, and this project ships
none of it.

## Status

Works. 127 tests, verified on macOS with Java 21 and Python 3.12. Commander is
the format it has been exercised on, other Forge formats are configuration
rather than code but are untested.
