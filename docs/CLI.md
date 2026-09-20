# CLI reference

Every command, every flag. Organised by what you are trying to do.

```
gauntlet decks      list decks from the collection database
gauntlet export     write one deck out as a Forge .dck
gauntlet doctor     check the install

gauntlet run        start a match
gauntlet status     what a running match is doing
gauntlet stop       end a running match early

gauntlet act        submit a decision and wait for the next one

gauntlet sweep      one deck against a field, in parallel
gauntlet matches    recent matches
gauntlet replay     a match as a readable play-by-play
gauntlet summary    counts and timings for one match
```

Deck references are a collection slug, a collection name, or a path to a text
decklist. A path that exists wins over a name. A `.dck` path is refused.

Examples below use real slugs. `gauntlet decks` prints the ones you have.

---

# Setting up

## `gauntlet doctor`

Checks Forge, the bridge jar, the Java runtime, the collection and the storage
paths. No flags. Exits 1 if any line fails.

```bash
gauntlet doctor
```

## `gauntlet decks`

| Flag | Type | Default | What it does |
|---|---|---|---|
| `--owner` | str | all owners | only this owner's decks |
| `--json` | flag | off | machine-readable |

```bash
gauntlet decks
gauntlet decks --owner matt
gauntlet decks --json
```

Human output is four columns, slug then owner then card count then the
commander field.

```
feather-storm                  matt     100  Jetmir, Nexus of Revels
temur-roar-precon              matt     100  Ureni of the Unwritten (featured alt commander in the 99: Eshki, Temur's Roar)
```

`--json` is an array, one object per deck.

```json
[
 {
  "slug": "temur-roar-precon",
  "name": "Temur Roar — Tarkir: Dragonstorm Commander Precon",
  "owner": "matt",
  "commander": "Ureni of the Unwritten (featured alt commander in the 99: Eshki, Temur's Roar)",
  "card_count": 100
 }
]
```

`commander` is free text from the collection, including the aside in
parentheses. The harness parses the name out of it, everything before the first
`(`.

## `gauntlet export`

Writes a deck as a Forge `.dck` without playing anything. Useful for checking
what Forge will actually be handed.

| Argument or flag | Type | Required | What it does |
|---|---|---|---|
| `deck` | str | yes | slug, name, or path to a text list |
| `--out`, `-o` | path | yes | where to write the `.dck` |
| `--owner` | str | no | disambiguate when two owners have the same slug |

```bash
gauntlet export feather-storm --out /tmp/feather.dck
gauntlet export ~/lists/brew.txt --out /tmp/brew.dck
```

Validation problems print to stderr as `warning:` lines and do not stop the
write. A deck that is 99 cards, or runs two copies of a nonbasic, still exports.

```
warning: 99 cards, Commander wants 100 (1 commander plus 98)
/tmp/brew.dck  (99 cards)
```

Set codes are deliberately omitted from the `.dck`, so Forge picks whatever
printing its card data has.

---

# Running games

## `gauntlet run`

| Flag | Type | Default | What it does |
|---|---|---|---|
| `--a` | str | required | seat A deck |
| `--b` | str | required | seat B deck |
| `--seat-a` | str | `interactive` | `forge`, `sdk`, `api`, or `interactive` |
| `--seat-b` | str | `interactive` | same |
| `--games` | int | `1` | games to play in one JVM |
| `--seed` | int | none | RNG seed, for a reproducible game |
| `--format` | str | `Commander` | Forge `GameType` name |
| `--routed` | str | `mulligan,cast_or_pass,attack,block` | decision kinds sent to a seat, comma separated |
| `--decision-timeout` | int | `300` | seconds a seat may think before Forge decides for it |
| `--game-timeout` | int | `900` | seconds before a game is called a draw |
| `--model` | str | `claude-sonnet-5` | model for `api` seats, ignored otherwise |
| `--owner` | str | none | collection owner for deck lookup |
| `--trace` / `--no-trace` | flag | off | log every decision point Forge reaches, routed or not |
| `--json` | flag | off | machine-readable |

### The four seat kinds

| Seat | Who decides | Cost | Use it for |
|---|---|---|---|
| `forge` | Forge's own AI, inside the JVM | free, a few seconds a game | the control arm, thousands of games |
| `interactive` | an agent calling `gauntlet act` | one Bash call per decision | one game, close reading |
| `sdk` | a Claude Agent SDK session, on Claude Code's auth | no separate bill, about ten seconds a decision | a few hundred games unattended |
| `api` | the Claude API directly, needs `ANTHROPIC_API_KEY` | billed, one or two seconds a decision | large unattended runs |

They are interchangeable per seat.

### Foreground or detached

A match with at least one `interactive` seat runs detached, because the agent
holding that seat has to come back to it. Everything else runs in the
foreground and blocks until Forge exits.

Detached output names the match and every loop that has to start.

```bash
gauntlet run --a feather-storm --b temur-roar-precon
```

```
match 0920-1412-a3f1 started
  seat A  interactive  Feather Storm — Naya Tempest Hawk Swarm
  seat B  interactive  Temur Roar — Tarkir: Dragonstorm Commander Precon

a seat has 300s to answer before Forge decides for it.
every interactive seat needs its own loop, starting now:
  gauntlet act --match 0920-1412-a3f1 --seat A
  gauntlet act --match 0920-1412-a3f1 --seat B
```

`--json` for the detached case.

```json
{
 "match": "0920-1412-a3f1",
 "detached": true,
 "seats": {"A": "interactive", "B": "interactive"},
 "decision_timeout": 300
}
```

Foreground output, and its `--json`.

```bash
gauntlet run --a feather-storm --b temur-roar-precon \
  --seat-a forge --seat-b forge --games 20 --seed 2026
```

```
match 0920-1421-77c2: {'A': 13, 'B': 7}

gauntlet replay 0920-1421-77c2
```

```json
{
 "match": "0920-1421-77c2",
 "games": [
  {"kind": "game_result", "game": 1, "ms": 3184, "turns": 17, "winner": "A", "draw": false}
 ],
 "wins": {"A": 13, "B": 7},
 "crashed": false,
 "error": ""
}
```

`games` carries Forge's own result objects verbatim, one per game.

### Common shapes

```bash
# agent against Forge's AI, only your seat loops
gauntlet run --a feather-storm --b sultai-arisen-precon --seat-b forge

# two agents
gauntlet run --a feather-storm --b temur-roar-precon

# unattended, SDK on both sides, ten games in one JVM
gauntlet run --a feather-storm --b veloci-ramp-tor-precon \
  --seat-a sdk --seat-b sdk --games 10

# a text decklist against a collection deck
gauntlet run --a ~/lists/brew.txt --b abzan-armor-precon --seat-b forge

# reproducible
gauntlet run --a feather-storm --b temur-roar-precon \
  --seat-a forge --seat-b forge --games 20 --seed 2026
```

### `--routed`, the speed dial

The four kinds are `mulligan`, `cast_or_pass`, `attack`, `block`. Anything you
drop is answered by Forge's AI inside the JVM and never leaves it. Fewer kinds
means faster games and worse play.

```bash
# only the plays, skip mulligans and combat
gauntlet run --a feather-storm --b temur-roar-precon \
  --seat-a sdk --seat-b forge --routed cast_or_pass
```

An unknown kind is rejected before the match starts rather than silently
routing nothing.

```
unknown decision kind(s) ['cast'], known kinds are ['attack', 'block', 'cast_or_pass', 'mulligan']
```

`--routed` applies to both seats. The engine and the Java side support per-seat
routing, the CLI does not expose it.

### The three clocks

| Clock | Flag | Default | What happens when it runs out |
|---|---|---|---|
| how long a seat may think | `run --decision-timeout` | 300s | Forge's AI decides, the transcript records a fallback |
| how long one game may last | `run --game-timeout` | 900s | the game is a draw |
| how long `act` waits for a question | `act --timeout` | 120s | the call returns `waiting`, nothing is lost |

### `--games` and `--seed`

`--games N` plays N games in one JVM. Forge costs about forty seconds of card
database load per JVM and a few seconds per game after that, so a batch in one
process is the difference between a sweep that finishes and one that does not.

`--seed` seeds Forge's RNG. The same seed and the same decklists reproduce the
game, as long as the seats make the same choices. Forge seats do. Agent seats
do not.

### `--trace`

Sets `GAUNTLET_TRACE=1` for the JVM. The bridge then prints a line to stderr at
every decision point it reaches, whether or not that kind is routed, and it
lands in `<match-id>.forge.log`.

```
[gauntlet] A cast_or_pass -> seat
[gauntlet] A attack -> forge
```

This answers "is Forge even asking me this", which nothing else shows.

## `gauntlet status`

| Flag | Type | Default | What it does |
|---|---|---|---|
| `--match` | str | required | match id |
| `--json` | flag | off | no effect, the output is JSON either way |

```bash
gauntlet status --match 0920-1412-a3f1
```

```json
{
 "match": "0920-1412-a3f1",
 "finished": false,
 "games": [],
 "wins": {},
 "seats": {"A": "interactive", "B": "interactive"}
}
```

Only works while the match is live, it talks to the match's control socket. A
finished match answers through `gauntlet summary` instead.

## `gauntlet stop`

| Flag | Type | Default | What it does |
|---|---|---|---|
| `--match` | str | required | match id |

```bash
gauntlet stop --match 0920-1412-a3f1
```

Closes both sockets and every seat. An abandoned match holds a socket and a
JVM, so stop the ones you are not going to finish.

---

# Playing as an agent

## `gauntlet act`

One command does everything. It submits your answer to the last question and
blocks until the next one arrives.

| Flag | Type | Default | What it does |
|---|---|---|---|
| `--match` | str | required | match id |
| `--seat` | str | required | which seat you are playing |
| `--choice` | int | none | your answer to the open question |
| `--why` | str | `""` | one sentence on what you are playing for |
| `--id` | int | the open question | the question id you are answering |
| `--timeout` | float | `120.0` | seconds to wait for the next question |
| `--json` | flag | off | the raw control reply instead of prose |

```bash
# first call, pick up whatever is on the table
gauntlet act --match 0920-1412-a3f1 --seat A

# answer, then wait for the next question
gauntlet act --match 0920-1412-a3f1 --seat A --choice 2 \
  --why "Plains first, both my two drops are single white."
```

`--id` is never required. Leaving it out means "the question you last showed
me", which the daemon resolves and the caller cannot. Pass it only when you
want to name the question explicitly.

Taking a question does not consume it. Call with no `--choice` as often as you
like and you get the same question back, rules text included. That is the
recovery path when an agent loses its place.

Omitting `--why` with a `--choice` prints a warning to stderr and records an
empty reasoning string. `--why` from this path is stored whole, it is not
trimmed.

`docs/PLAYING.md` is the contract for an agent actually playing. This section
is the flag reference.

### What comes back

Five statuses. Two of them exit non-zero.

| Status | Exit | Human output |
|---|---|---|
| `decide` | 0 | the rendered question |
| `waiting` | 0 | `nothing to decide yet after 120s. The other seat is thinking, or Forge is resolving. Call again.` |
| `game_over` | 0 | `match over. wins: {'A': 1}` |
| `stale` | 1 | the answer was for a question the engine already gave up on |
| `error` | 1 | bad seat name, bad choice, no such match |

Rendered `decide` output, prose.

```
decision 22 (attack)

Turn 5, combat_declare_attackers, A is the active player.

YOU (A) - 40 life, 88 cards in library
  Hand (4): Boros Charm {R}{W}, Mountain, Gods Willing {W}, Sun Titan {4}{W}{W}
  Battlefield: Plains, Plains (tapped), Mountain, Sacred Foundry, Feather, the Redeemed (3/4), Sunhome Stalwart (2/2)

B - 36 life, 3 cards in hand, 88 in library
  Battlefield: Forest, Forest, Forest, Llanowar Elves (1/1, tapped), Questing Beast (4/4)

Declare attackers.

Your options:
  [0] Attack as Forge proposes
  [1] Do not attack

Forge proposes: feather-the-redeemed, sunhome-stalwart
```

`--json` gives the control reply untouched.

```json
{
 "status": "decide",
 "request": {
  "id": 22,
  "seat": "A",
  "kind": "attack",
  "prompt": "Declare attackers.",
  "options": [
   {"i": 0, "label": "Attack as Forge proposes", "card": null, "cost": null},
   {"i": 1, "label": "Do not attack", "card": null, "cost": null}
  ],
  "state": {"turn": 5, "phase": "combat_declare_attackers", "active": "A", "you": "A", "me": {}, "opponents": []},
  "cards": {"feather-the-redeemed": {"name": "Feather, the Redeemed", "cost": "{1}{R}{W}", "type": "Legendary Creature Angel", "pt": "3/4"}},
  "new_cards": {},
  "proposed": ["feather-the-redeemed", "sunhome-stalwart"],
  "since": ["Turn 5 (A)", "A draws a card."]
 }
}
```

`cards` names everything in view and is sent every call. `new_cards` carries
oracle text and is sent once per card. `docs/PROTOCOL.md` explains the split.

`game_over` with `--json` is the status block plus the status word.

```json
{
 "status": "game_over",
 "match": "0920-1412-a3f1",
 "finished": true,
 "games": [{"kind": "game_result", "game": 1, "ms": 41220, "turns": 19, "winner": "A", "draw": false}],
 "wins": {"A": 1},
 "seats": {"A": "interactive", "B": "forge"}
}
```

---

# Running a field

## `gauntlet sweep`

One deck against many, pairings in parallel, one JVM each.

| Flag | Type | Default | What it does |
|---|---|---|---|
| `--deck` | str | required | the deck under test, always seat A |
| `--against` | str | `all` | comma separated opponents, or `all` for every other deck in the collection |
| `--games` | int | `3` | games per opponent |
| `--seed` | int | none | seed, so the sweep reproduces |
| `--workers` | int | auto | parallel JVMs, each holds about 1 GB |
| `--owner` | str | none | collection owner |
| `--format` | str | `Commander` | Forge game type |
| `--game-timeout` | int | `900` | seconds before a draw is called |
| `--seat-a` | str | `forge` | who plays the deck under test |
| `--seat-b` | str | `forge` | who plays each opponent |
| `--decision-timeout` | int | `300` | seconds a seat may think |
| `--json` | flag | off | machine-readable |

Default workers with Forge on both sides is `min(6, cpu_count // 2)`, bounded by
memory. With any `sdk` or `api` seat it drops to `min(4, 8 // agent_seats)`,
bounded by rate limits upstream.

```bash
# the fast, free version
gauntlet sweep --deck feather-storm --games 20 --seed 2026

# three named opponents
gauntlet sweep --deck feather-storm \
  --against temur-roar-precon,sultai-arisen-precon,veloci-ramp-tor-precon \
  --games 20

# agents on both sides, roughly a hundred times slower
gauntlet sweep --deck feather-storm --against temur-roar-precon \
  --seat-a sdk --seat-b sdk --games 5 --workers 2
```

Progress goes to stderr, one line per finished pairing, so the table on stdout
stays clean.

```
feather-storm against 23 decks, 20 games each (460 total)
  [1/23] chaos-incarnate-precon  16-4-0
  [2/23] temur-roar-precon  7-13-0
```

The table is sorted by win rate, worst matchups at the bottom.

```
feather-storm over 460 games against 23 decks, 4180s

opponent                          W-L-D   rate  turns
------------------------------  -------  -----  -----
chaos-incarnate-precon           16-4-0    80%     20
temur-roar-precon                7-13-0    35%     17

overall 247-213-0, 54% of decisive games
```

A draw here is usually a game that hit `--game-timeout`, and the table says so
when there are any.

`--json`.

```json
{
 "deck": "feather-storm",
 "elapsed_s": 4180.2,
 "pairings": [
  {
   "opponent": "chaos-incarnate-precon",
   "wins": 16,
   "losses": 4,
   "draws": 0,
   "win_rate": 0.8,
   "median_turns": 20,
   "match": "0920-0909-faa7",
   "error": ""
  }
 ]
}
```

`win_rate` is wins over decisive games, draws excluded. `error` is empty on a
pairing that ran and a message on one that did not.

### Exhaustion

An `sdk` or `api` seat that hits a usage limit raises `SeatExhausted`, and the
sweep cancels every pairing still queued. Falling back silently would spend
hours reporting Forge's play as the agent's. The human table prints a warning
block when this happens.

```
WARNING: a seat ran out of capacity during this sweep, so some or all
of these games were played by Forge's AI rather than the seat named.
Do not read these numbers as an agent result.
```

`--json` does not carry that flag. Read the human table, or check `fallbacks`
in `gauntlet summary` for the pairing's match id, before trusting numbers from
an agent sweep.

---

# Reading results

## `gauntlet matches`

| Flag | Type | Default |
|---|---|---|
| `--limit` | int | `20` |

```bash
gauntlet matches --limit 5
```

```
0920-1124-c051  finished  Commander  2026-09-20T15:24:53.925+00:00
0920-0909-faa7  finished  Commander  2026-09-20T13:09:33.969+00:00
```

Newest first. Status is `running`, `finished`, `crashed`, or `exhausted`.

## `gauntlet summary`

| Argument | Type | Required |
|---|---|---|
| `match` | str | yes, positional |

Always JSON, there is no `--json` flag.

```bash
gauntlet summary 0920-0909-faa7
```

```json
{
 "match_id": "0920-0909-faa7",
 "exists": true,
 "status": "finished",
 "format": "Commander",
 "seed": "2026",
 "started_at": "2026-09-20T13:09:33.969+00:00",
 "finished_at": "2026-09-20T15:21:16.043+00:00",
 "decisions": 2470,
 "events": 0,
 "games": 20,
 "wins": {"A": 17, "B": 3},
 "draws": 0,
 "winner": "A",
 "fallbacks": 755,
 "by_seat": {
  "A": {"decisions": 929, "fallbacks": 492, "why": 437},
  "B": {"decisions": 1541, "fallbacks": 263, "why": 1278}
 },
 "by_kind": {"mulligan": 46, "cast_or_pass": 2128, "attack": 213, "block": 83},
 "unknown_kinds": [],
 "latency_ms": {"n": 2470, "mean": 3149, "p50": 1984, "p90": 6079, "max": 65450}
}
```

| Field | Read it for |
|---|---|
| `wins`, `draws`, `winner` | the result. `winner` is null when two seats tied |
| `fallbacks`, `by_seat[*].fallbacks` | how much of the game the seat actually played. 492 of 929 means Forge played half of seat A |
| `by_seat[*].why` | how many decisions carry reasoning. Far below `decisions` means the transcript will not answer "why did it lose" |
| `by_kind` | where the round trips went. `cast_or_pass` dominates |
| `unknown_kinds` | non-empty means the Java side raised a kind Python does not model. That is a bug |
| `latency_ms` | per-decision timing. `max` near the decision timeout means a seat was close to falling back |
| `exists` | false for a match id not in this database |

## `gauntlet replay`

| Argument or flag | Type | Default | What it does |
|---|---|---|---|
| `match` | str | required, positional | match id |
| `-v` | flag | off | include every option offered and the full state JSON |
| `--out`, `-o` | path | stdout | write to a file instead |

```bash
gauntlet replay 0920-0909-faa7
gauntlet replay 0920-0909-faa7 -v
gauntlet replay 0920-0909-faa7 --out report.md
```

Markdown, grouped by game and turn, with each seat's reasoning quoted under its
play.

```markdown
# Match 0920-0909-faa7 — Commander

**A** — Feather Storm — Naya Tempest Hawk Swarm · sdk · collection:feather-storm
**B** — Temur Roar — Tarkir: Dragonstorm Commander Precon · sdk · collection:temur-roar-precon

status finished · seed 2026 · forge 2.0.14 · bridge 20260920-090112 · 2470 decisions · 755 fallbacks

## Turn 5 — A's turn

**A** main1 · Lightning Helix {R}{W}
> Killing the Elves before it ramps them to Ureni, and three life keeps me out of burn range.

**B** combat_declare_blockers · block (Forge AI decided — no answer for block within 300s)
```

A fallback is always labelled. Forge's play never reads as the seat's own.

Works on a match that is still running and on one that crashed, because it
renders whatever rows are on disk. A running match says so at the top.

`-v` adds the sequence number, the kind, the latency, every option with the
chosen one marked, and the full state block as JSON. It is much longer and it
is what you read when a decision looks wrong.

---

# Environment

| Variable | Read by | Effect |
|---|---|---|
| `GAUNTLET_FORGE_HOME` | `scripts/fetch-forge.sh`, `paths.vendor_dir()` | Forge install outside the repo. `java/build.sh` ignores it |
| `GAUNTLET_TRACE=1` | the Java bridge | same as `run --trace`, set for you by that flag |
| `XDG_DATA_HOME` | `paths.data_dir()` | where `transcripts.db` and exported decks live |
| `XDG_STATE_HOME` | `paths.state_dir()` | where match sockets, logs and meta files live |
| `ANTHROPIC_API_KEY` | the `anthropic` client | needed by `api` seats, not by `sdk` seats |

# Exit codes

| Code | Meaning |
|---|---|
| 0 | fine |
| 1 | the command failed, the reason is on stderr. `doctor` uses this for a failed check, `act` for `error` and `stale` |
| 2 | typer rejected the arguments |
