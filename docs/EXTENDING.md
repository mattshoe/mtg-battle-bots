# Extending Gauntlet

Four things people want to add, and the one thing nobody should.

Read `docs/PROTOCOL.md` first if you are touching the wire. Read the invariants
at the bottom of this file before you touch anything.

---

# Adding a decision kind

Four places, in this order. The names must match exactly across all of them.

## 1. `PlayerControllerGauntlet`

Override the Forge method. Every override has the same shape and a new one
should too.

```java
@Override
public TargetChoices chooseTargets(SpellAbility sa, ...) {
    // 1. bail to super if this kind is not routed, or the choice is forced
    if (!routes("choose_target")) {
        return super.chooseTargets(sa, ...);
    }
    List<GameEntity> candidates = legalTargets(sa);
    if (candidates.size() < 2) {
        return super.chooseTargets(sa, ...);   // one legal answer is not a decision
    }

    // 2. build the option list, and keep the mapping back to Forge objects
    JsonObject req = envelope("choose_target", "Choose a target for " + sa.getHostCard().getName() + ".");
    JsonArray opts = new JsonArray();
    for (int i = 0; i < candidates.size(); i++) {
        opts.add(option(i, shorten(candidates.get(i).toString())));
    }
    req.add("options", opts);

    // 3. ask
    int choice = askForIndex(req, opts.size());

    // 4. on any doubt at all, fall back to super
    if (choice < 0) {
        return super.chooseTargets(sa, ...);
    }
    return toTargetChoices(candidates.get(choice));
}
```

Step 4 is not optional. A seat that hangs, crashes or answers nonsense must cost
the run a slightly worse decision, never a lost game.

Rules for the option list.

| Rule | Why |
|---|---|
| index from `0`, contiguous | the answer is one integer, nothing else |
| skip the round trip when there is one legal answer | a forced choice is not judgment, and the round trip is the expensive part |
| lead with the card name when Forge's label does not contain it | Forge labels the ability, not the card. Two lands both come back as `Play land` |
| run labels through `shorten` | Forge's description of a spell is its full rules text |
| set `card` to the host card's slug | the seat already has that card's oracle text and can cross-reference |
| omit `cost` when it is empty or `no cost` | a missing value reads as a bug |
| never offer mana abilities | paying is Forge's job, and every option is already payable |

## 2. `protocol.py`

Add the name to `KINDS`.

```python
KINDS: frozenset[str] = frozenset(
    {
        "mulligan",
        "cast_or_pass",
        "attack",
        "block",
        "choose_target",   # new
    }
)
```

A kind the Java side raises that Python does not know about is a bug, not a
warning. It shows up in `gauntlet summary` as `unknown_kinds`. A kind Python
knows that Java never raises is harmless.

## 3. `match.py`

Add it to `DEFAULT_ROUTED` if it should be on by default.

```python
DEFAULT_ROUTED: tuple[str, ...] = ("mulligan", "cast_or_pass", "attack", "block", "choose_target")
```

This is also what `gauntlet run --routed` defaults to and validates against.
Leaving it out means the kind exists but has to be asked for.

## 4. `prompt.py`

Render it, if it needs more than an option list.

`build_prompt` already handles `since`, the state, `new_cards`, the prompt
sentence and the options. A new kind needs work here only if it carries a
payload the way `attack` and `block` carry `proposed`, which
`render_options` reads out of `request.extra`.

## A fifth place, sometimes

`GauntletMain.DEFAULT_ROUTED` is the Java-side default set. It applies only when
a bridged seat gets no `--routed` flag, which the Python harness always passes.
It matters when someone runs the java command by hand. Keep it in step anyway.

## Before you call it done

Adding a field to the protocol is a compatible change and needs no version bump.
Adding a kind adds fields to nothing, so no bump.

Run the suite, run ruff, and play one real game end to end. A change that passes
the tests and breaks an actual match has happened more than once, because most
of what can go wrong lives in the gap between the JVM and Python.

---

# Writing a new seat type

A seat is the only thing in the system that exercises judgment. Everything else
is bookkeeping and the design exists to keep it that way.

## The `Seat` contract

```python
class Seat(ABC):
    controller: str = "unknown"

    @abstractmethod
    def decide(self, request: Request, timeout: float) -> Response: ...

    def close(self) -> None: ...
```

| Member | Contract |
|---|---|
| `controller` | a class attribute naming who played. Goes in the transcript's `seats` table and in `gauntlet status`. Pick something short and stable, it ends up in reports |
| `decide` | returns a `Response`, or raises `SeatTimeout` or `SeatExhausted`. Called on the thread serving that player's bridge connection, one call at a time per seat. May block the whole `timeout`, nothing else in the match waits on it, Forge is single-threaded per game so the game is already stopped |
| `close` | releases anything held. Must be safe to call more than once. Called from `MatchServer.shutdown`, which runs even on a crash |

What `decide` must guarantee.

| Guarantee | Enforced where |
|---|---|
| `response.id == request.id` | `Response.validate_against`, called again by the server |
| `response.choice` is one of `request.options[*].index` | same |
| it returns or raises within roughly `timeout` | nothing enforces it. A seat that blocks past the timeout blocks the game thread past the point Forge would have moved on |
| it does not mutate `request` | `Request` and `Option` are frozen dataclasses with slots |
| it is not called concurrently for one seat | the bridge is synchronous per connection. A seat may still hold a lock if it wants to be sure, `SdkSeat` does |

A seat that raises anything else is caught, recorded as
`seat raised <Type>: <message>`, and deferred to Forge. That is a bug in the
seat, not a reason to end the game.

## Exhaustion against timeout

The distinction is the whole reason `SeatExhausted` exists.

| | `SeatTimeout` | `SeatExhausted` |
|---|---|---|
| means | nobody answered this one | the seat cannot answer any more, and retrying will not help |
| cost | one decision | every remaining decision |
| the run | continues, one fallback recorded | recorded as untrustworthy |
| server does | `_defer`, record the fallback | `_defer`, record the fallback, write a `seat_exhausted` event, set `self.exhausted[seat]`, set the finished flag |
| `match.run` does | nothing special | sets the match status to `exhausted` and the result error to `seat ran out of capacity mid-run (...)` |
| `sweep` does | nothing special | cancels every queued pairing and prints a warning block over the table |

Get this wrong in the lenient direction and a run spends hours falling back on
every decision and reports Forge's play as the agent's. That is the exact
failure `SdkSeat` was written around, after a real two and a half hour run.

`SdkSeat` detects it by scanning the reply text.

```python
_TERMINAL_MARKERS = (
    "session limit", "usage limit", "rate limit", "quota",
    "will not respond", "no further responses",
)
```

Crude, and it was arrived at by watching a run fail. A new backend needs its own
version of this, ideally reading a status code rather than prose.

## A worked example

```python
# src/gauntlet/my_seat.py
from .prompt import build_prompt, parse_reply
from .protocol import ProtocolError, Request, Response
from .seats import Seat, SeatExhausted, SeatTimeout


class MySeat(Seat):
    controller = "mine"

    def __init__(self, *, endpoint: str, deck_note: str = "") -> None:
        self.endpoint = endpoint
        self.deck_note = deck_note
        # The bridge sends oracle text once. A stateful seat accumulates it.
        self._seen_cards: dict[str, dict] = {}
        self._closed = False

    def decide(self, request: Request, timeout: float) -> Response:
        if self._closed:
            raise SeatExhausted("seat is closed, it ran out of capacity earlier")

        self._seen_cards.update(request.new_cards)
        text = build_prompt(request, self._seen_cards, deck_note=self.deck_note)

        try:
            reply = my_backend_call(self.endpoint, text, timeout=timeout)
        except MyQuotaError as exc:
            self._closed = True
            raise SeatExhausted(str(exc)) from exc
        except Exception as exc:
            raise SeatTimeout(f"backend failed: {type(exc).__name__}: {exc}") from exc

        try:
            choice, why = parse_reply(reply, request)
        except ProtocolError as exc:
            raise SeatTimeout(f"unusable reply: {exc}") from exc
        return Response(id=request.id, choice=choice, why=why)

    def close(self) -> None:
        self._closed = True
```

Then register it in `seats.build_seat`.

```python
        case "mine":
            # Imported here, not at module scope, if it pulls in anything heavy.
            from .my_seat import MySeat

            return MySeat(**kwargs)
```

And widen the error message in the `case _` arm so a typo names every kind.

Anything a seat needs beyond the defaults arrives through
`SeatSpec.options`, which `build_seats` passes as keyword arguments.
`build_seats` also sets `deck_note` for `api` and `sdk` seats, extend that
condition if your seat wants it.

`--seat-a` and `--seat-b` take the string unvalidated, so the new name works
from the CLI as soon as `build_seat` knows it.

## Use `prompt.build_prompt`

Both model-backed seats and `gauntlet act` render through it, so a person
reading a transcript and a model answering a question are looking at the same
thing. Two renderings of the same state would eventually disagree and the
disagreement would be invisible.

`parse_reply` is the matching half. It wants `CHOICE: <n>` and `WHY: <text>`,
accepts a bare number on its own line, validates the index against the request's
options, and trims the reasoning to 500 characters.

---

# Upgrading Forge

Deliberate, in this order, and nothing skipped.

```bash
# 1. change the pin
$EDITOR scripts/fetch-forge.sh            # FORGE_VERSION=...

# 2. refetch into a clean directory, the script skips a populated one
mv vendor vendor.old
scripts/fetch-forge.sh

# 3. rebuild the bridge against it
java/build.sh

# 4. run the suite
uv run --with pytest python -m pytest -q
uv run --with ruff ruff check src/ tests/

# 5. play one real game, both seats Forge
uv run gauntlet doctor
uv run gauntlet run --a feather-storm --b temur-roar-precon \
  --seat-a forge --seat-b forge --games 3 --seed 1

# 6. play one real game with a bridged seat
uv run gauntlet run --a feather-storm --b temur-roar-precon \
  --seat-a sdk --seat-b forge
```

Step 5 and step 6 test different things. Step 5 proves Forge still runs and
still reports results on stdout. Step 6 proves the bridge still compiles against
a real game and that the option lists are not empty.

## What breaks loudly

`java/build.sh` fails. These are the Forge API surfaces the bridge touches, and
a rename in any of them is a compile error.

| Class | Used for |
|---|---|
| `PlayerControllerAi`, `LobbyPlayerAi`, `AIOption` | the base the controller extends |
| `ComputerUtilAbility.getAvailableCards`, `.getSpellAbilities`, `.getAvailableLandsToPlay` | building the `cast_or_pass` option list |
| `ComputerUtilCost.canPayCost` | filtering that list to payable |
| `SpellAbility.isLandAbility`, `.isManaAbility`, `.canPlay`, `.toUnsuppressedString`, `.getPayCosts`, `.getHostCard`, `.setActivatingPlayer` | the same list |
| `Combat.getAttackers`, `.getBlockers`, `.getDefenderByAttacker`, `.clearAttackers`, `.getAllBlockers`, `.removeFromCombat` | attack and block |
| `Card.getOracleText`, `.getManaCost`, `.getType`, `.getBasePower`, `.getNetPower`, `.getDamage`, `.isSick`, `.isTapped`, `.isFaceDown` | `StateView` |
| `Player.getCardsIn`, `.getLife`, `.getCommanders`, `.getCommanderDamage`, `.setFirstController` | `StateView` and seating |
| `Game.getPhaseHandler`, `.getStack`, `.getCombat`, `.getGameLog`, `.getPlayers`, `.getOutcome`, `.setGameOver` | everywhere |
| `GuiBase.setInterface`, `GuiDesktop`, `FModel.initialize`, `MyRandom.setRandom` | the bootstrap |
| `Match`, `GameRules`, `GameType`, `RegisteredPlayer.forCommander`, `DeckSerializer.fromFile`, `GamePlayerUtil.createAiPlayer` | `GauntletMain` |

Fix what the compiler complains about. The bridge is five files and about a
thousand lines, so this is bounded.

## What breaks quietly

Nothing here fails to compile, and all of it changes what a seat sees.

| Thing | Where it leaks | How you notice |
|---|---|---|
| `PhaseType` enum names | `state.phase` strings | a transcript full of a phase name nothing recognises |
| `GameLogEntry.message()` wording | the `since` feed | the feed still populates, the prose changed |
| `SpellAbility.toUnsuppressedString()` | every `cast_or_pass` option label | labels get longer, shorter, or stop containing the card name |
| `getOracleText()` newline convention | `new_cards[*].text` | multi-line cards arrive as one run-on line. The bridge rewrites a literal `\n` today |
| the `no cost` sentinel string | option `cost`, card `cost` | lands start carrying a cost field reading `no cost` |
| the bootstrap ordering in `bootstrapForge` | startup | `HeadlessException`, or `MissingResourceException: en-US` |
| the `.dck` format | every deck | Forge refuses to load a deck |
| `GameType` enum members | `--format` | `IllegalArgumentException` at startup |
| the fat jar's filename | `paths.forge_version()`, which splits on `-` and takes index 3 | transcripts record the wrong version, or `unknown` |
| card data as `res/cardsfolder/cardsfolder.zip` rather than loose files | `scripts/fetch-forge.sh`'s card count line | that line prints `0`, harmlessly |

Compare a `gauntlet replay -v` from before and after on the same seed. The state
blocks should differ only where the game did.

## Rollback

Put the old pin back, restore `vendor.old`, rebuild. Transcripts record
`forge_version` and `bridge_revision` per match, so an old result stays
attributable to the version that produced it, and a result that moved after an
upgrade is visible as such.

`bridge_revision` is the bridge jar's mtime, formatted `%Y%m%d-%H%M%S`. A poor
version, an honest one. Anything derived from git would lie the moment someone
builds without committing.

---

# Adding a deck source

`DeckList` is the boundary. Everything upstream of it is a source, everything
downstream only knows `DeckList`.

```python
@dataclass(frozen=True, slots=True)
class DeckList:
    name: str
    commanders: tuple[str, ...]
    main: tuple[tuple[int, str], ...]   # (quantity, card name)
    source: str
```

Two sources exist, the collection database and pasted text.

## The steps

Write a loader that returns a `DeckList`.

```python
def from_moxfield(deck_id: str) -> DeckList:
    payload = fetch(deck_id)
    return DeckList(
        name=payload["name"],
        commanders=tuple(payload["commanders"]),
        main=_sorted_main(tuple((c["qty"], c["name"]) for c in payload["main"])),
        source=f"moxfield:{deck_id}",
    )
```

Wire it into `match.resolve_deck`, which is the only place that turns a string
from the CLI into a `DeckList`.

```python
def resolve_deck(ref: str, *, owner: str | None = None) -> deckmod.DeckList:
    candidate = Path(ref).expanduser()
    if candidate.exists():
        ...
    if ref.startswith("moxfield:"):
        return deckmod.from_moxfield(ref.removeprefix("moxfield:"))
    return deckmod.from_collection(ref, owner=owner)
```

A path that exists wins over a name, because someone who typed a path meant it.
Put a prefixed scheme ahead of the collection lookup and behind the path check.

## Rules a source has to follow

| Rule | Why |
|---|---|
| card names spelled the way Forge's card database spells them | a wrong name is a deck that will not load. `_forge_name` handles the `A // B` cases, split cards and rooms keep both halves, everything else keeps the front face |
| the commander is not also in `main` | Forge refuses a deck whose commander is one of the 99. `from_collection` strips it because the database stores it as an ordinary row |
| quantities summed, not one row per copy | eight Swamps is one entry with qty 8 |
| `main` sorted through `_sorted_main` | exporting the same deck twice gives byte-identical files, so a diff between two runs means the deck really changed |
| `source` is a scheme prefix and an identifier | it goes in the transcript's `seats.deck_source` column and in the `replay` header. `collection:<slug>`, `text` |
| no set codes | `to_dck` omits them deliberately. The collection records the printing you own, Forge only knows the sets its card data was built with. Pinning one to the other turns a missing set into a deck that will not load |
| accent and apostrophe folding through `_norm` | Clavileño and Yuna's Guardian both arrive spelled more than one way |

## Validation

`decks.validate` returns a list of strings and never raises. A deck can be worth
playing while still being illegal, and the caller decides whether this run cares.
`gauntlet export` prints them as warnings, `match.plan` prints them to stderr.

Extend it rather than inventing a second checker. Note that the singleton
exemption list is looked up from oracle text rather than hardcoded, because the
list grows every set and a stale one reports a legal deck as illegal.

## Do not write to the collection database

`decks._connect` opens it `mode=ro` through a URI, deliberately. It is the record
of what Matt physically owns. A harness that plays games has no business
changing it. A new source should reach for its own storage.

---

# What not to do

## Never special-case a card

This is the one that matters. Magic's rules belong to Forge.

If a card behaves wrongly, that is an upstream Forge bug and it belongs upstream.
Not a workaround here, not a conditional on the card name, not a small list of
exceptions in `StateView` or `PlayerControllerGauntlet`.

The reason is not purity. A harness that knows about individual cards stops
being a harness. Every special case is a place where the game the harness plays
and the game Magic plays have quietly diverged, and the result is a playtest
number that looks exactly like a real one. A playtest result you cannot trust is
worse than no playtest.

Search `PlayerControllerGauntlet.java` and `StateView.java` for a card name. You
will not find one. Keep it that way.

The one adjacent thing that is allowed is naming a *mechanism*, not a card.
`isBookkeeping` filters command-zone entries whose name ends in `Effect`,
because those are Forge's internal representation of the commander tax rather
than objects a player acts on. That is a fact about Forge's data model. A list
of Eldrazi titans would not be.

## Never let a seat see hidden information

An opponent's hand and library are counts. This is enforced in `StateView`, not
by convention. A seat that could see through them would make every transcript
worthless as evidence about a deck, and it would be invisible in the results.

A new field on `state` has to be checked against this. "The opponent's top card"
is a bug. "The opponent's graveyard" is not, because graveyards are public.

## Never let a fallback read as the seat's own play

Every route outward falls back to Forge's AI on timeout, error or nonsense, and
the transcript records `fallback` with the reason. `gauntlet replay` labels it
inline, `gauntlet summary` counts it per seat.

A fallback presented as the agent's own play would make every transcript a lie.
If you add a path where a decision can be made without the seat, it records a
fallback or it does not ship.

## Never collapse the GPL and MIT boundary

The Java bridge links against Forge and is GPLv3-or-later. The Python harness is
MIT. They talk over a socket and that separation is deliberate.

Do not import one into the other's build. Do not add a JNI shim. Do not vendor
the bridge's source into the Python package. The socket is the license boundary
as much as it is the process boundary.

## Never vendor Forge into git

`scripts/fetch-forge.sh` owns the version and `vendor/` is gitignored. 300 MB of
someone else's GPL code plus card data does not belong in this repository.
Pinning it in a script keeps the upgrade an explicit act.

## Never bump one side's protocol version alone

`protocol.VERSION` and `Bridge.PROTOCOL_VERSION` must match. Adding a field is
compatible and needs neither. Renaming a field, removing one, or changing what a
value means needs both.

## Never make `take` consuming

`InteractiveSeat.take` returns the same question until it is answered. An earlier
version used a semaphore and the second look came back empty, which read as
"nothing to do" at exactly the moment there was something to do. There is a
regression test. Leave it alone.

## Do not trade fidelity for speed by accident

`--routed` is the dial and it is meant to be turned deliberately. Dropping a
kind makes games faster and the play worse, in that order. Measure before
optimising. `sweep --json` reports per-pairing timings and the transcript stores
per-decision latency.

Two throughput facts worth knowing before you optimise anything else. Forge
costs about forty seconds of card database load per JVM and a few seconds per
game after that, so a batch belongs in one process through `--games N`. And
`sweep` runs pairings in parallel, one JVM each, bounded by memory rather than
CPU.
