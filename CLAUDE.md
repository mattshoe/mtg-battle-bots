# Working on Gauntlet

For an agent *playing* a game, read `docs/PLAYING.md` instead. This file is for
an agent changing the harness.

## Orient yourself first

```bash
uv run --python 3.12 gauntlet doctor      # is the install sound
uv run --python 3.12 --with pytest python -m pytest -q
uv run --python 3.12 --with ruff ruff check src/ tests/
```

System `python3` on this machine is broken by an unaccepted Xcode license. Always
`uv run --python 3.12`. The same license blocks `git`, so version control needs
`sudo xcodebuild -license` accepted once by a person.

## The shape of it

Forge plays Magic. A Java bridge routes the decisions that need judgment out to
a socket. Python answers them and writes everything down. `ARCHITECTURE.md` has
the reasoning, including why Forge and not XMage.

```
java/src/main/java/forge/gauntlet/    the bridge, GPLv3, links against Forge
  Bridge.java                one socket, one line of JSON each way
  PlayerControllerGauntlet   which decisions leave the JVM
  StateView.java             the board, compact, hidden info kept hidden
  LobbyPlayerGauntlet        installs the controller
  GauntletMain.java          entry point, a sibling of Forge's SimulateMatch

src/gauntlet/                the harness, MIT
  protocol.py                the wire format, versioned
  seats.py                   who answers: forge, interactive, api
  server.py                  the match daemon, two sockets
  match.py                   assemble and run one match
  sweep.py                   one deck against a field, in parallel
  transcript.py              SQLite plus the play-by-play render
  decks.py                   collection database to Forge .dck
  prompt.py                  a decision as text an agent reads
```

## Invariants — breaking any of these makes results untrustworthy

1. **Magic's rules are Forge's.** If a card behaves wrongly that is an upstream
   bug. Never special-case a card here. A harness that knows about individual
   cards stops being a harness.
2. **A seat never stalls the game.** Every route outward falls back to Forge's
   AI on timeout, error, or nonsense, and the transcript records `fallback` with
   the reason. A fallback presented as the agent's own play would make every
   transcript a lie.
3. **Hidden information stays hidden.** `StateView` sends opponents' hands and
   libraries as counts. A seat that could see through them would make its
   results worthless as evidence about a deck.
4. **The protocol is versioned and both sides check it.** Adding a field is
   fine. Renaming one, removing one, or changing a meaning bumps `VERSION` in
   `protocol.py` and `PROTOCOL_VERSION` in `Bridge.java`, and they must match.
5. **Forge is pinned and never vendored into git.** `scripts/fetch-forge.sh`
   owns the version. Upgrading is deliberate: change the version, refetch,
   rebuild the bridge, run the suite.
6. **The bridge is GPLv3 and the Python is MIT.** They talk over a socket. Do
   not collapse that boundary by importing one into the other's build.

## Things that already caught someone out

| Symptom | Cause | What was done |
|---|---|---|
| `MissingResourceException: en-US` | Forge reads `res/` relative to the working directory | the JVM is launched with `cwd` set to the Forge install root |
| `HeadlessException` from `GuiDesktop` | `java.awt.headless` set before the class loads, it needs a screen device to compute UI scale | headless is set *after* `GuiBase.setInterface` |
| A seat's whole option list was "tap this land" | mana abilities were being enumerated as choices | `sa.isManaAbility()` filtered out, they are Forge's job during payment |
| Two options both labelled "Play land" | Forge labels the ability, not the card | the card name is prepended when it is not already in the label |
| Cards rendered as slugs after the first decision | rules text is sent once per game, but `gauntlet act` is a fresh process each call | the daemon remembers, and splits `cards` (names, every time) from `new_cards` (text, once) |
| A seat blocked without being told what was attacking | the state had no combat information, and vigilance meant attackers were not even tapped | `StateView.combat()` sends attackers, their stats and existing blocks |
| A removal spell left hand and mana and killed nothing, silently | Forge picks targets, so a chosen spell can resolve elsewhere or fizzle, and a board state cannot show a cause | each request carries `since`, Forge's own log of what happened since this seat last acted |
| 173 of 196 decisions were `cast_or_pass`, most of them noise | Forge hands priority back after every resolution, so the same question repeats within one step | a pass is fingerprinted against turn, phase, stack and both boards, and an identical repeat auto-passes |
| Forge proposing no blocks rendered as nothing at all | an empty proposal list is falsy, so the line was skipped | "nothing" is sent explicitly, absence and emptiness must not look the same |
| `take()` returned nothing while a question was open | the rendezvous consumed its own wakeup | rewritten on a condition variable, `take` is idempotent, there is a regression test |
| A pure Forge-AI match reported no results | results arrive over the bridge, and that match has no bridge | Forge's stdout is parsed too, and `record_game_result` dedupes by game number |

## Adding a decision kind

Four places, in this order, and the names must match exactly:

1. `PlayerControllerGauntlet` — override the Forge method, guard it with
   `routes("your_kind")`, fall back to `super` on any doubt
2. `protocol.py` — add the name to `KINDS`
3. `match.py` — add it to `DEFAULT_ROUTED` if it should be on by default
4. `prompt.py` — render it, if it needs more than an option list

A kind the Java side raises that Python does not know about is a bug, not a
warning. A kind Python knows that Java never raises is harmless.

## Speed

A Commander game is roughly 15 to 30 turns and asks a seat 15 to 50 questions.
Forge itself costs about 40 seconds of card-database load per JVM and two to
four seconds per game after that. So:

- the JVM start is amortised by `--games N` in one process, never N processes
- `sweep` runs pairings in parallel, one JVM each, bounded by memory not CPU
- `--routed` is the fidelity dial. Dropping a kind makes games faster and the
  play worse, in that order

Measure before optimising. `sweep --json` reports per-pairing timings and the
transcript stores per-decision latency.

## Before you finish

Run the suite, run `ruff`, and play one real game end to end. A change that
passes the tests and breaks an actual match has happened more than once, because
most of what can go wrong lives in the gap between the JVM and Python.
