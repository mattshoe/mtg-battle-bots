# Wire protocol

Two wires. The bridge protocol between the Java side and the Python harness,
and the control protocol between the harness and `gauntlet act`. This document
specifies both closely enough to write a third-party seat against either.

Authoritative sources, in this order.

| Concern | File |
|---|---|
| message framing, versioning, id rules | `java/.../Bridge.java`, `src/gauntlet/protocol.py` |
| which decisions leave the JVM, and their option lists | `java/.../PlayerControllerGauntlet.java` |
| the `state` block | `java/.../StateView.java` |
| how a seat renders it | `src/gauntlet/prompt.py` |
| the control socket | `src/gauntlet/server.py` |

---

# 1. The bridge protocol

## Transport

Newline-delimited JSON over loopback TCP.

The harness binds `127.0.0.1:0` and reads back the assigned port, so several
matches can run at once without picking ports and hoping. It passes the
endpoint to Forge as `--bridge SEAT=host:port`, one flag per bridged seat.

The Java side connects out. One TCP connection per bridged seat, both to the
same port, each served by its own thread in Python.

| Property | Value |
|---|---|
| direction of connect | Java to Python |
| connect timeout | 10 s |
| `TCP_NODELAY` | on |
| read timeout | the decision timeout, `--decision-timeout` seconds |
| encoding | UTF-8 |
| framing | one JSON object per line, `\n` terminated, no embedded raw newlines |
| concurrency | strictly synchronous per connection, Java holds a lock across write then read |

A seat played by Forge's AI opens no connection at all. It never reaches Python
and `ForgeSeat.decide` raising means the wiring is wrong.

Only three kinds of line exist.

| Line | Direction | Wants a reply |
|---|---|---|
| request | Java to Python | yes, exactly one |
| response | Python to Java | it is the reply |
| notification | Java to Python | no |

## Versioning

Every message carries `v`. `Bridge.PROTOCOL_VERSION` and `protocol.VERSION` are
both `1` and must match.

| Rule | |
|---|---|
| adding a field | compatible, no bump |
| renaming a field | breaking, bump both |
| removing a field | breaking, bump both |
| changing what a value means | breaking, bump both |

Python checks `v` on every incoming line. A mismatch raises `ProtocolError`,
writes a `protocol_error` event to the transcript with the offending line, and
drops the connection rather than guessing.

Java does not check `v` on a response. It checks `id`. This asymmetry is real,
a third-party harness must still send the right `v` because a future Java side
may start checking.

Fields Python does not model are kept, not dropped. `Request.parse` sweeps
anything outside `{v, id, seat, kind, prompt, options, state, new_cards}` into
`extra`, which is stored in the transcript's `extra` column. A transcript stays
complete when one side has been upgraded and the other has not.

## Request

Emitted by `PlayerControllerGauntlet.envelope` plus the per-kind additions,
with `v` and `id` stamped on by `Bridge.ask`.

This table describes a decision request. A notification is built separately and
carries only `v`, `id`, `kind` and its own payload, see section 1.9.

| Field | Type | Present | Meaning |
|---|---|---|---|
| `v` | int | always | protocol version, `1` |
| `id` | int | always | `> 0` for a question, `0` for a notification |
| `seat` | string | always | the seat letter this match was started with, `A` or `B` |
| `kind` | string | always | one of the four decision kinds, or a notification kind |
| `prompt` | string | always | one sentence of English naming the question |
| `state` | object | always | the board from this seat's point of view, section 1.5 |
| `options` | array | on a decision | the flat indexed list the answer indexes into |
| `new_cards` | object | when non-empty | oracle text for slugs this seat has not been shown this game |
| `since` | array | when non-empty | Forge's game log since this seat last acted |
| `proposed` | array | `attack` and `block` only | what Forge's AI would do |

`id` is a per-connection counter starting at `1`. Each bridged seat has its own,
so seat A and seat B both start at 1. It keeps climbing across games in a
`--games N` match, it does not reset.

JSON object order is insertion order from Gson and carries no meaning. Do not
parse positionally.

### Option

Each entry of `options`.

| Field | Type | Present | Meaning |
|---|---|---|---|
| `i` | int | always | the index. This is what goes back on the wire |
| `label` | string | always | human text, at most 93 characters |
| `card` | string | when there is a host card | the card's slug, matching `state` and `new_cards` |
| `cost` | string | when there is a payable cost | Forge's cost string with `CARDNAME` substituted |

Indices are contiguous from `0`. `label` is Forge's own description of the
ability, flattened and trimmed at 90 characters on a word boundary with `...`
appended. Where Forge's label does not already contain the card name, the name
is prepended as `Card Name - label`, because two lands in hand both come back
from Forge as `Play land`.

`cost` is omitted when Forge's cost string is empty or `no cost`. Every option
offered is already legal and payable, a seat does not need to check.

## Response

One line per request, from Python back to Java.

```json
{"v":1,"id":47,"choice":1,"why":"Ramp now. Curve is the constraint."}
```

| Field | Type | Present | Meaning |
|---|---|---|---|
| `v` | int | always | `1` |
| `id` | int | always | echoes the request's `id` |
| `choice` | int or null | always | an `i` from the request's options, or `null` to defer |
| `why` | string | on a real answer | the reasoning. Java ignores it, the transcript keeps it |

A deferral leaves `why` out entirely.

```json
{"v":1,"id":47,"choice":null}
```

### Validation, both sides

Python checks before sending, in `Response.validate_against`.

| Check | On failure |
|---|---|
| `response.id == request.id` | `ProtocolError`, the seat is recorded as a fallback |
| `choice` is one of the option indices | `ProtocolError`, the seat is recorded as a fallback |

Java checks on receipt, in `Bridge.ask` and `askForIndex`.

| Condition | Result |
|---|---|
| no line, socket closed | bridge permanently broken, `-1` |
| read timed out | `-1`, bridge stays usable |
| line is not JSON | bridge permanently broken, `-1` |
| `id` missing or not the id asked | bridge permanently broken, `-1` |
| `choice` missing, null, or not an int | `-1` |
| `choice` outside `[0, optionCount)` | `-1` |
| anything else | the index |

`-1` means Forge decides.

A permanently broken bridge is not recoverable. `routes()` returns false from
then on, so every remaining decision for that seat is played by Forge's AI
inside the JVM and nothing further reaches Python. The socket is not closed, so
the Python side sees silence rather than an error. A run whose decision count
stops climbing mid-match is this.

## The deferral contract

`choice: null` means "you decide, Forge". It is an explicit null rather than a
missing field so a reader of the raw stream can tell a deliberate hand-back from
a truncated message.

Four things produce the same outcome.

| Cause | Transcript |
|---|---|
| explicit `choice: null` | `fallback`, reason from the seat |
| no answer within the decision timeout | `fallback`, `no answer for <kind> within <n>s` |
| an answer that fails validation | `fallback`, the `ProtocolError` text |
| the seat raised anything at all | `fallback`, `seat raised <Type>: <message>` |

The harness never lets a fallback read as the seat's own play. `gauntlet replay`
labels it inline.

```
**A** main1 · cast_or_pass (Forge AI decided — no answer for cast_or_pass within 300s)
```

There is one exception to "a deferral costs one decision". `SeatExhausted` means
the seat cannot answer any more questions and retrying will not help, usually a
usage limit. It defers that decision, writes a `seat_exhausted` event, sets the
match finished flag, and marks the whole run untrustworthy. A sweep cancels
every queued pairing on it.

## Decision kinds

Four. `protocol.KINDS` on the Python side, `DEFAULT_ROUTED` on both sides.

A kind the Java side raises that Python does not know about is a bug, visible as
`unknown_kinds` in `gauntlet summary`. A kind Python knows that Java never
raises is harmless.

| Kind | Options | Payload | What a deferral does |
|---|---|---|---|
| `mulligan` | `[0] Mulligan`, `[1] Keep` | none | calls `super.mulliganKeepHand` |
| `cast_or_pass` | `[0] Pass priority`, then one per playable ability | `card`, `cost` per option | calls `super.chooseSpellAbilityToPlay` |
| `attack` | `[0] Attack as Forge proposes`, `[1] Do not attack` | `proposed` | Forge's proposal stands |
| `block` | `[0] Block as Forge proposes`, `[1] Do not block` | `proposed` | Forge's proposal stands |

`attack` and `block` are different from the other two. `super.declareAttackers`
has already run before the question is sent, so the proposal is on the board
already and the only thing choosing `1` does is clear it. A timeout there is
indistinguishable in effect from choosing `0`.

### `mulligan`

```json
{
  "seat": "A",
  "kind": "mulligan",
  "prompt": "Opening hand. Keep, or mulligan? You would put 0 card(s) on the bottom if you keep.",
  "state": { "...": "turn 0, no phase" },
  "new_cards": { "...": "the whole opening hand plus the commander" },
  "options": [
    {"i": 0, "label": "Mulligan"},
    {"i": 1, "label": "Keep"}
  ],
  "v": 1,
  "id": 1
}
```

`1` keeps. Anything else, including a deferral, goes to Forge's AI.

The first decision of a game carries no `since`. The state has `turn` 0 and no
usable `phase`, which `prompt.py` renders as `Before the game starts.` rather
than inviting a seat to reason about a board that does not exist.

### `cast_or_pass`

Where most of the game lives.

```json
{
  "seat": "A",
  "kind": "cast_or_pass",
  "prompt": "You have priority. Choose something to play, or pass.",
  "state": { "...": "" },
  "new_cards": {},
  "since": ["Turn 3 (A)", "A draws a card."],
  "options": [
    {"i": 0, "label": "Pass priority"},
    {"i": 1, "label": "Mountain - Play land", "card": "mountain"},
    {"i": 2, "label": "Cast Lightning Helix", "card": "lightning-helix", "cost": "{R}{W}"},
    {"i": 3, "label": "Sunhome Stalwart - Cast Sunhome Stalwart", "card": "sunhome-stalwart", "cost": "{1}{W}"}
  ],
  "v": 1,
  "id": 12
}
```

`0` passes priority. `n > 0` plays `candidates[n - 1]`.

The option list is legality, not Forge's AI's opinion. Forge's AI keeps a much
narrower list of abilities it would consider, and offering only those would
reduce a seat to picking among the AI's ideas. Two things are filtered out.

| Filtered | Why |
|---|---|
| mana abilities | tapping a land is part of paying for a spell, and Forge's payment machinery does it. Offering them made an option list mostly "tap this land" |
| land abilities from the general enumeration | land drops are enumerated separately from `getAvailableLandsToPlay`, which already accounts for the drop being used |

A seat picks which spell. Forge picks targets, modes and mana payment, through
`playChosenSpellAbility`. That is why `since` exists.

Forge hands priority back after every resolution, so the same question arrives
many times in one step with nothing changed. The bridge fingerprints a pass
against the turn, the active player, the stack depth, every player's life,
battlefield size and hand size, and the sorted option labels. An identical
repeat auto-passes without a round trip. Phase is deliberately not part of the
fingerprint, because sorcery-speed plays only appear in the option list during a
main phase, so arriving at one changes the options and the seat is asked again.

### `attack`

```json
{
  "seat": "A",
  "kind": "attack",
  "prompt": "Declare attackers.",
  "state": { "...": "" },
  "since": ["A casts Lightning Helix."],
  "options": [
    {"i": 0, "label": "Attack as Forge proposes"},
    {"i": 1, "label": "Do not attack"}
  ],
  "proposed": ["Feather, the Redeemed", "Sunhome Stalwart"],
  "v": 1,
  "id": 22
}
```

`proposed` is card names, not slugs. When Forge proposes no attack it is
`["nothing"]`, never `[]`, because an empty list renders as nothing at all and
absence must not look like emptiness.

`1` calls `combat.clearAttackers()`. `0` and every deferral leave Forge's
proposal in place. There is no way to attack with a subset.

### `block`

```json
{
  "seat": "B",
  "kind": "block",
  "prompt": "Declare blockers.",
  "state": { "...": "with a combat block, see 1.5" },
  "options": [
    {"i": 0, "label": "Block as Forge proposes"},
    {"i": 1, "label": "Do not block"}
  ],
  "proposed": ["Llanowar Elves blocks Sunhome Stalwart"],
  "v": 1,
  "id": 23
}
```

`proposed` entries read `<blocker> blocks <attacker>`. With no blocks it is
`["nothing"]` and option `0`'s label becomes
`Block as Forge proposes (it proposes no blocks)`.

`1` removes every blocker from combat. There is no way to reassign one.

## 1.5 The `state` block

Written only by `StateView.state`, read only by `prompt.py`. Change one and the
other stops working.

### Top level

| Field | Type | Present | Value |
|---|---|---|---|
| `turn` | int | always | `PhaseHandler.getTurn()`. `0` before the game starts |
| `phase` | string | always | Forge's `PhaseType` name lowercased, for example `main1`, `combat_declare_attackers`, `end_of_turn`. `"?"` when null |
| `active` | string | always | the active player's name, which is the seat letter. `"?"` when null |
| `you` | string | always | this seat's own player name, so a render can name you |
| `me` | object | always | this seat's zones |
| `opponents` | array | always, possibly empty | one object per other player |
| `stack` | array of strings | when non-empty | each item is `SpellAbility.getStackDescription()`, bottom first |
| `combat` | array | when non-empty | section below |

### `me`

| Field | Type | Present | Value |
|---|---|---|---|
| `life` | int | always | |
| `hand` | array of slugs | always | your own hand in full |
| `battlefield` | array of permanents | always | |
| `graveyard` | array of slugs | always | |
| `command` | array of slugs | always | the commander, bookkeeping cards filtered out |
| `exile` | array of slugs | always | |
| `library` | int | always | a count. Your own library contents are hidden from you too |

### An `opponents` entry

| Field | Type | Present | Value |
|---|---|---|---|
| `name` | string | always | the seat letter |
| `life` | int | always | |
| `hand_size` | int | always | a count, never contents |
| `library` | int | always | a count |
| `battlefield` | array of permanents | always | |
| `graveyard` | array of slugs | always | |
| `command` | array of slugs | always | |
| `cmd_damage_to_me` | object | when non-empty | commander slug to damage that commander has dealt **to you** |

Hidden information stays hidden. An opponent's hand and library are counts,
enforced in the state builder rather than by convention. A seat that could see
through them would make its results worthless as evidence about a deck.

Opponents have no `exile` field.

### A permanent

| Field | Type | Present | Value |
|---|---|---|---|
| `c` | string | always | the card's slug |
| `pt` | string | creatures only | `getNetPower()/getNetToughness()`, current not printed |
| `dmg` | int | creatures with damage marked | marked damage |
| `sick` | bool `true` | creatures with summoning sickness | omitted when false |
| `tapped` | bool `true` | tapped permanents | omitted when false |

A field that does not apply is absent, never false or zero. That keeps a wide
board short.

### A `combat` entry

Present whenever `game.getCombat()` is non-null and has attackers, which covers
both the attack and the block decision.

| Field | Type | Present | Value |
|---|---|---|---|
| `c` | string | always | the attacker's slug |
| `pt` | string | creature attackers | current power and toughness |
| `attacking` | string | when there is a defender | `GameEntity.toString()`, a player name or a permanent's name |
| `blocked_by` | array of slugs | when non-empty | everything already blocking this attacker |

Without this a seat asked to declare blockers has only which creatures happen to
be tapped, which does not say what they are attacking and does not cover
vigilance at all.

### What the state does not contain

A third-party seat should not expect any of it.

Counters on permanents. Attachments and what is attached to what. Planeswalker
loyalty. Mana pools. Poison, monarch, initiative, the day and night cycle.
Which step of a phase it is beyond the `PhaseType` name. Who holds priority.
An opponent's exile zone. Face-down cards by identity, they arrive as the slug
`face-down-card` and carry no oracle text.

`prompt.py` renders a further subset. It does not print your own `exile`, and it
prints an opponent's `command` and `graveyard` only when non-empty.

### A complete state block

```json
{
  "turn": 7,
  "phase": "main1",
  "active": "A",
  "you": "A",
  "me": {
    "life": 34,
    "hand": ["boros-charm", "mountain", "gods-willing"],
    "battlefield": [
      {"c": "plains"},
      {"c": "plains", "tapped": true},
      {"c": "sacred-foundry"},
      {"c": "feather-the-redeemed", "pt": "3/4"},
      {"c": "sunhome-stalwart", "pt": "2/2", "dmg": 1, "sick": true}
    ],
    "graveyard": ["lightning-helix"],
    "command": ["feather-the-redeemed"],
    "exile": [],
    "library": 71
  },
  "opponents": [
    {
      "name": "B",
      "life": 28,
      "hand_size": 4,
      "library": 68,
      "battlefield": [{"c": "llanowar-elves", "pt": "1/1", "tapped": true}],
      "graveyard": ["shock"],
      "command": ["ureni-of-the-unwritten"],
      "cmd_damage_to_me": {"ureni-of-the-unwritten": 7}
    }
  ],
  "stack": ["Lightning Helix deals 3 damage to Llanowar Elves."]
}
```

## Slugs

Every card in `state`, in `new_cards`, and in an option's `card` field is a
slug. `StateView.slug` lowercases the name and replaces every run of
non-alphanumeric characters with a single `-`, with no leading or trailing dash.

| Name | Slug |
|---|---|
| `Feather, the Redeemed` | `feather-the-redeemed` |
| `Sol Ring` | `sol-ring` |
| `Ureni of the Unwritten` | `ureni-of-the-unwritten` |

Slugs are not unique across all of Magic in principle. In one game they are
stable and that is all they are used for.

A face-down card is always the name `Face-down card`, slug `face-down-card`, and
it is never registered as known so it never carries oracle text.

## `new_cards` on the wire

Oracle text is sent once per card, per seat, per game, the first time that seat
could see the card. It is the single biggest lever on round-trip size. A hundred
card Commander deck's oracle text is tens of kilobytes, a state block is a
couple.

Keys are slugs. Values.

| Field | Type | Present | Value |
|---|---|---|---|
| `name` | string | always | the printed name |
| `cost` | string | when there is one | mana cost. Omitted for lands, whose cost stringifies as `no cost` |
| `type` | string | always | the full type line |
| `pt` | string | creatures only | `getBasePower()/getBaseToughness()`, printed not current |
| `text` | string | when non-empty | oracle text, with Forge's literal `\n` between paragraphs turned into real newlines |

```json
{
  "boros-charm": {
    "name": "Boros Charm",
    "cost": "{R}{W}",
    "type": "Instant",
    "text": "Choose one —\n• Boros Charm deals 4 damage to target player or planeswalker.\n• Permanents you control gain indestructible until end of turn.\n• Target creature gains double strike until end of turn."
  },
  "feather-the-redeemed": {
    "name": "Feather, the Redeemed",
    "cost": "{1}{R}{W}",
    "type": "Legendary Creature Angel",
    "pt": "3/4",
    "text": "Flying\nWhenever you cast an instant or sorcery spell that targets a creature you control, exile that card instead of putting it into your graveyard as it resolves. If you do, return it to your hand at the beginning of the next end step."
  }
}
```

The "seen" set lives on `StateView`, which lives on
`PlayerControllerGauntlet`, which Forge builds fresh per game through
`createIngamePlayer`. So in a `--games N` match the text is resent at the start
of each game. The `id` counter on the `Bridge` does not reset, the seen set
does.

A seat implementing the bridge protocol directly must accumulate `new_cards`
itself. `ApiSeat` and `SdkSeat` do exactly that, one line.

```python
self._seen_cards.update(request.new_cards)
```

A slug can appear in `state` before its text has ever been sent, when a card
enters a zone this seat can see in the same message it is asked about.
`prompt.py` prints the bare slug rather than nothing.

## `since`

Forge's own account of what happened since this seat was last asked anything.

Built from `game.getGameLog().getAllEntries()` past a per-controller cursor.
Each entry is its `message()`, with newlines flattened to spaces and blank ones
dropped. The cursor advances to the end on every request, and resets to zero if
the log ever shrinks, which means a new game in the same match.

Capped at 40 entries. When there are more, the array is 41 long and the first
entry is the marker.

```json
{
  "since": [
    "... 63 earlier events omitted",
    "Turn 7 (B)",
    "B casts Ureni of the Unwritten.",
    "A casts Lightning Helix targeting Llanowar Elves.",
    "Llanowar Elves is destroyed."
  ]
}
```

Omitted entirely when empty, which is the first decision of a game.

This exists because Forge chooses targets, not the seat. A removal spell a seat
picked can resolve against something it did not intend, or fizzle for want of a
legal target, and a board state cannot show the difference. The board shows the
result, `since` shows the cause.

## Notifications

`id` is `0` and no response is written. Python treats a message as a
notification if its `kind` is in `NOTIFICATIONS` **or** its `id` is `0`, so
either is sufficient.

### `game_result`

Sent by `GauntletMain` to every open bridge after each game, and printed to
Forge's stdout as the same object.

```json
{"kind":"game_result","game":1,"ms":41220,"turns":19,"winner":"A","draw":false,"v":1,"id":0}
```

| Field | Type | Value |
|---|---|---|
| `kind` | string | `game_result` |
| `game` | int | 1-based game number within the match |
| `ms` | int | wall clock for that game |
| `turns` | int | `PhaseHandler.getTurn()` at the end |
| `winner` | string or null | the winning seat's name, null on a draw |
| `draw` | bool | true when there is no outcome or the outcome is a draw |

It carries no `seat`.

Both channels are needed and both are used. A match with no bridged seat has
only stdout. An interactive agent blocked in `act` learns the game ended over
the bridge rather than waiting out its poll. Whichever arrives first wins,
`record_game_result` dedupes on the game number.

### `game_over`

`NOTIFICATIONS` reserves the name and `PlayerControllerGauntlet.reportGameOver`
constructs it, with fields `seat`, `kind` and `summary`. Nothing calls that
method, so this message is never sent. Treat the name as reserved.

## Writing a third-party harness

Replacing the Python side entirely, keeping the Java bridge.

1. Listen on a loopback TCP port.
2. Launch Forge with the bridge jar ahead of the Forge jar on the classpath,
   working directory set to the Forge install root, and
   `--bridge SEAT=127.0.0.1:port` for each seat you want to control. The exact
   command the harness builds is in `engine.build_command` and is written to
   `~/.local/state/gauntlet/<match-id>.json` for every match, so you can copy
   one and run it by hand.
3. Accept one connection per bridged seat. Serve each on its own thread.
4. Read lines. For each line, parse the object. If `id` is `0` or `kind` is a
   notification kind, handle it and write nothing. Otherwise answer with exactly
   one line.
5. Answer with `{"v":1,"id":<same id>,"choice":<int>,"why":"..."}`, or
   `{"v":1,"id":<same id>,"choice":null}` to hand it back.
6. Never write a line for a message you did not receive, and never write two for
   one. An `id` mismatch breaks the bridge permanently.
7. Accumulate `new_cards` yourself.
8. Answer or do not, but do not hang forever. Forge will time out at
   `--decision-timeout` and carry on, which is fine, and your seat will look
   like the AI in the record, which is not.

---

# 2. The control protocol

The wire between `gauntlet act` and a running match. Only interactive seats use
it. `examples/heuristic_agent.py` is a complete working client, about eighty
lines.

## Transport

A unix stream socket at `$XDG_STATE_HOME/gauntlet/<match-id>.sock`, defaulting
to `~/.local/state/gauntlet/<match-id>.sock`.

One request line, one reply line, then the server closes. A call is not a
session. `call_match` in `src/gauntlet/server.py` is the client, twenty lines.

## Operations

| `op` | Fields | Reply statuses |
|---|---|---|
| `act` | `seat`, `timeout`, and optionally `choice`, `why`, `id` | `decide`, `waiting`, `game_over`, `stale`, `error` |
| `status` | none | the status block |
| `stop` | none | `{"status": "stopped"}` |

## `act`

```json
{"op":"act","seat":"A","timeout":120.0,"choice":2,"why":"Plains first."}
```

Submitting and collecting are one call on purpose. An agent that crashed
mid-turn restarts by calling with no `choice` and picks up the question still on
the table.

Resolving which question a `choice` answers happens on the server, not the
client. Omitting `id` means "the question you just showed me", and only the
daemon knows which that was. A client that probed for the open id would
sometimes find a different question, its own answer having arrived late, and
answer that one with the wrong choice.

### `decide`

```json
{
  "status": "decide",
  "request": {
    "id": 12,
    "seat": "A",
    "kind": "cast_or_pass",
    "prompt": "You have priority. Choose something to play, or pass.",
    "options": [{"i": 0, "label": "Pass priority", "card": null, "cost": null}],
    "state": {},
    "cards": {},
    "new_cards": {},
    "since": ["Turn 3 (A)", "A draws a card."]
  }
}
```

The `request` object is the bridge request with two differences. `options`
entries always carry `card` and `cost` keys, explicitly null when absent. And
`new_cards` is split in two.

Everything the bridge put in `extra`, which is `since` and `proposed` and
anything a newer Java side added, is spread flat into `request`. A field the
bridge omitted is absent here too, it is not nulled.

### Why `cards` and `new_cards` are split here

The bridge sends a card's text once and never again, which works for a seat that
holds state across decisions. `gauntlet act` is a fresh process every call and
holds nothing, so the daemon remembers on its behalf and splits the reply.

| Key | Sent | Contents |
|---|---|---|
| `cards` | every call | every slug referenced anywhere in `state` or in an option's `card`, with `name`, `cost`, `type`, `pt`. No `text` |
| `new_cards` | once per slug per match | the subset whose oracle text this seat has not had rendered yet, with `text` |

An agent needs `cards` on every call to read the board at all. It needs `text`
only the first time it meets a card.

Two consequences worth knowing.

Re-asking the same question id returns the identical `new_cards`, cached against
that id. `take` is idempotent by design, and marking cards as shown on the first
call would leave the second one holding a board whose text it was never given.

The shown set is per match, not per game. In a `--games N` match with an
interactive seat, the second game's replies carry no repeated oracle text even
though the bridge resent it.

### `waiting`

```json
{"status": "waiting"}
```

Nothing on the table within `timeout`. The other seat is thinking, or Forge is
resolving. Call again. Nothing is lost.

### `game_over`

The status block with `"status": "game_over"` on the front. Returned when
`take` finds nothing and the match's finished flag is set.

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

### `stale`

```json
{
  "status": "stale",
  "error": "the question you were shown is no longer open, most likely your answer arrived after the engine gave up on it. Call again with no --choice to see where the game actually is."
}
```

Returned when `choice` came with no `id` and the question now open is not the
one this client was last shown. Applying the answer would attach one seat's
reasoning to a play Forge actually made.

### `error`

```json
{"status": "error", "error": "choice 7 is not one of [0, 1] for attack"}
```

Also for a seat that is not interactive, an unknown `op`, and a line that is not
JSON.

## `status`

```json
{"op":"status"}
```

Returns `match`, `finished`, `games`, `wins`, `seats`. Same block the
`game_over` reply embeds.

## `stop`

```json
{"op":"stop"}
```

Shuts the server down, closes both sockets and every seat, and removes the
socket file. Any seat blocked in `decide` wakes with `match ended`.
