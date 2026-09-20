# Gauntlet — architecture

A harness for playing Magic: The Gathering games between two agents at speed,
with a full transcript of every decision and the reasoning behind it.

## The one-paragraph version

Forge, the open-source Magic engine, enforces the rules and executes the game.
A small Java bridge seats a player whose every meaningful decision is shipped
over a local socket as JSON. A Python service owns that socket, routes each
decision to whoever is holding that seat — a Claude agent, the Claude API, or
Forge's own AI — and writes a structured transcript. Nothing about Magic's
rules lives in this repo.

## Why Forge

Forge implements 33,956 cards, 99.8% of everything Commander-legal, and ships a
headless `sim` mode with seeded RNG and a Commander format. XMage is
MIT-licensed against Forge's GPLv3 and has a cleaner decision interface, but its
maintainers say it does not support large-scale headless bot games, its cards
are compiled Java classes rather than text scripts, and its wire protocol is
undocumented Java serialization pinned to an exact build.

Writing our own rules engine was the third option. A playtest result you cannot
trust is worse than no playtest, and every corner case we got wrong would
quietly poison the data.

## Layers

```
  ┌─ agent (you) ─┐   ┌─ agent (subagent) ─┐
  │  gauntlet act │   │  gauntlet act      │      Bash calls, one per decision
  └───────┬───────┘   └─────────┬──────────┘
          │                     │
  ┌───────┴─────────────────────┴──────────┐
  │  gauntletd  (Python)                   │      match orchestration
  │   · seats      who answers for whom    │      transcript
  │   · protocol   versioned JSON          │      decision policy
  │   · transcript SQLite + markdown       │
  └───────────────────┬────────────────────┘
                      │ newline-delimited JSON over loopback TCP
  ┌───────────────────┴────────────────────┐
  │  forge-gauntlet-bridge  (Java, GPLv3)  │
  │   PlayerControllerGauntlet             │      extends PlayerControllerAi
  │     extends PlayerControllerAi         │      overrides the decisions that
  └───────────────────┬────────────────────┘      need judgment, inherits the rest
                      │
  ┌───────────────────┴────────────────────┐
  │  Forge  (pinned release, unmodified)   │      rules, cards, game loop
  └────────────────────────────────────────┘
```

The Java bridge is a separate artifact under `java/`. It links against Forge and
is therefore GPLv3. The Python side talks to it over a socket and is MIT. That
separation is deliberate and should not be collapsed.

## The decision protocol

Every request the engine makes of a seat is one `DecisionRequest`. Every answer
is one `DecisionResponse`. Both are versioned and validated on each side.

```jsonc
// engine -> seat
{
  "v": 1,
  "id": 47,
  "seat": "A",
  "kind": "cast_or_pass",
  "prompt": "Main phase 1. Choose a spell or ability to play.",
  "options": [
    {"i": 0, "label": "Pass priority"},
    {"i": 1, "label": "Cast Cultivate", "cost": "{2}{G}", "card": "cultivate"},
    {"i": 2, "label": "Play Forest", "card": "forest"}
  ],
  "state": { /* see below */ },
  "new_cards": { /* oracle text for cards this seat has not been shown yet */ }
}

// seat -> engine
{"v": 1, "id": 47, "choice": 1, "why": "Ramp now, hold up nothing. Curve is the constraint."}

// seat -> engine, handing the decision back
{"v": 1, "id": 47, "choice": null}
```

Three rules keep this stable:

1. **Options are always a flat indexed list.** Whatever the underlying Forge
   call looks like, the seat picks an integer. Targeting, modes, ordering and
   damage assignment each get a payload field, but the common case is one index.
2. **Oracle text is sent once per game per seat**, in `new_cards`, keyed by a
   slug. The `state` block refers to cards by slug. A seat that has already been
   told what Cultivate does never receives that text again. This is the single
   biggest lever on round-trip size.
3. **An unanswered or invalid response is never an error.** The bridge falls
   back to Forge's AI for that decision and the transcript records
   `fallback: true` with the reason. A game never stalls on a seat.

### State block

Compact and stable field order, so two transcripts diff cleanly.

```jsonc
{
  "turn": 7, "phase": "main1", "active": "A",
  "you": "A",                       // your seat, so a render can name you
  "me": {
    "life": 34,
    "hand": ["cultivate", "forest", "eternal-witness"],
    "battlefield": [{"c": "llanowar-elves", "pt": "1/1", "tapped": true}],
    "graveyard": ["opt"], "command": ["atraxa"], "exile": [],
    "library": 71                   // a count, contents are hidden from you too
  },
  "opponents": [{
    "name": "B", "life": 28, "hand_size": 4, "library": 68,
    "battlefield": [{"c": "sol-ring"}], "graveyard": ["shock"], "command": [],
    "cmd_damage_to_me": {"ureni-of-the-unwritten": 7}   // omitted when zero
  }],
  "stack": [],                      // omitted when empty
  "combat": [                       // omitted when no attackers
    {"c": "tempest-hawk", "pt": "2/2", "attacking": "B",
     "blocked_by": ["sygg-river-cutthroat"]}
  ]
}
```

`StateView.java` is the only writer of this and `prompt.py` the only reader.
Change one and the other stops working, so change them together. A permanent
carries `pt`, `tapped`, `sick` and `dmg` only when they apply, which keeps a
wide board short.

## Decision policy — the speed dial

A Commander game asks a player thousands of questions. Almost none of them are
interesting, so each one is handled at the cheapest level that can answer it:

| Class | Handling | Examples |
|---|---|---|
| forced | resolved in Java, never leaves the JVM | mana abilities, a repeat of a pass the seat already made on this board |
| heuristic | Forge's AI decides, logged but not asked | mana payment, targeting, modes, scry order, damage assignment |
| judgment | routed to the seat | `mulligan`, `cast_or_pass`, `attack`, `block` |

Moving a kind from `judgment` to `heuristic` is how you trade fidelity for
throughput. The default is tuned so a Commander game costs roughly 15-30 round
trips per seat rather than several hundred.

There is no policy file. `gauntlet run --routed` becomes `--routed SEAT=kinds`
on the java command line, and `PlayerControllerGauntlet.routes()` checks that
set before every decision. A kind that is not in it never leaves the JVM.

## Seats

A seat is anything that can answer a `DecisionRequest`.

- **`interactive`** — decisions queue up and an agent drains them with
  `gauntlet act`. One Bash call per decision. This is the "you and a subagent
  play a game" mode.
- **`sdk`** — a persistent Claude Agent SDK session, running on whatever auth
  Claude Code already has. No API key and nothing extra to pay, at roughly ten
  seconds a decision.
- **`api`** — the service calls the Claude API itself. Needs a key and bills
  separately, at roughly two seconds a decision.
- **`forge`** — Forge's own AI. The statistical floor, thousands of games, and
  the control arm for any experiment.

They are interchangeable per seat. Agent versus Forge AI is a valid match and
is the normal way to sanity-check a deck before spending agent time on it.

A seat that runs out of capacity raises `SeatExhausted`, which ends the match
and cancels the rest of a sweep. That is deliberately not a fallback: a run that
quietly finishes with Forge's AI wearing an agent's name is worse than a run
that stops.

## The agent loop

One call submits the previous decision and blocks for the next one:

```bash
gauntlet act --match m7 --seat A                      # first call, no decision yet
gauntlet act --match m7 --seat A --choice 1 --why "…" # answer, then wait for next
```

It long-polls, so an idle seat costs one blocked call rather than a spin loop.
It returns `{"status": "game_over", ...}` when the match ends.

## Transcript

Every match writes to SQLite: one row per decision with the full request, the
response, the reasoning, latency, and whether it fell back. `gauntlet replay`
renders a readable play-by-play. The reasoning strings are the point — a
win-rate number tells you a deck lost, the transcript tells you why.

Matches are reproducible. The RNG seed, both decklists, the policy, the Forge
version and the bridge revision are all recorded, so `gauntlet run` with the
same seed and decks plays the same games again.

## What this repo does not do

It does not implement Magic's rules, and it should never start to. If a card
behaves wrongly, that is a Forge bug and belongs upstream. The temptation to
"just special-case this one card" is how harnesses rot.
