# Playing a game

Read this before you touch the harness. It is the whole contract between two
agents playing a game of Magic against each other.

You hold one seat. Another agent holds the other. Neither of you runs the rules.
Forge does. You answer questions and say why.

## The one-call loop

One command does everything.

```bash
gauntlet act --match 0920-1412-a3f1 --seat A
gauntlet act --match 0920-1412-a3f1 --seat A --choice 2 --why "..."
```

The first form picks up whatever question is on the table. Every call after that
submits your answer to the last question and blocks until the next one arrives.
There is no separate "get" and "put", no state to carry, no ids to track.

The call blocks. It does not return immediately and it does not need polling.
Default wait is 120 seconds, set by `--timeout`. If nothing appears in that
window you get `nothing to decide yet, call again`, which means the other seat
is thinking or Forge is resolving something. Call again.

Taking a question does not consume it. Call with no `--choice` as many times as
you like and you get the same question back until you answer it. If you lose
your place, ask again. That is the recovery path and it is free.

Your answer goes to the question you were last shown, not to whatever happens to
be open. If those differ, the answer is refused with `stale` and you are told to
look again. That case means your previous answer arrived after the engine gave
up waiting and the game has moved on, so the choice you made is for a board that
no longer exists.

`--id` is available if you want to name the question explicitly, and it is never
required.

```bash
gauntlet act --match 0920-1412-a3f1 --seat A --id 47 --choice 2 --why "..."
```

## Starting the match, and the other seat

One agent starts it. Both seats default to `interactive`, so this is a two agent
game.

```bash
gauntlet run --a "Feather Storm" --b "Temur Roar"
```

Output names the match and the seats.

```
match 0920-1412-a3f1 started
  seat A  interactive  Feather Storm
  seat B  interactive  Temur Roar

a seat has 300s to answer before Forge decides for it.
every interactive seat needs its own loop, starting now:
  gauntlet act --match 0920-1412-a3f1 --seat A
  gauntlet act --match 0920-1412-a3f1 --seat B
```

The match runs detached, so it outlives the command that started it. The agent
that ran it takes seat A. It then launches a subagent for seat B and tells it
two things in the prompt, the match id and the letter B, plus a pointer to this
file. That is all the second agent needs.

Both seats must be draining. Seat A blocks on a question that will not arrive
until seat B answers its own, so a match with one agent working goes nowhere
until the engine starts timing out.

To play against Forge's AI instead, `--seat-b forge`. Then only your seat loops.

If you start a match, finish it or run `gauntlet stop --match <id>`. An abandoned
match holds a socket and a JVM.

## The four decision kinds

Every question is one of four. The kind is in the first line of the output.

| Kind | What you are actually deciding |
|---|---|
| `mulligan` | Keep this seven, or go again. Count lands and count whether the hand does the thing your deck is trying to do. |
| `cast_or_pass` | Which one spell, ability or land to play right now, or pass. This is where most of the game lives. |
| `attack` | Take Forge's proposed attack, or attack with nothing. |
| `block` | Take Forge's proposed blocks, or block with nothing. |

`cast_or_pass` is the one that rewards thought. The option list is everything
legal and payable, not what Forge's AI would consider, so a line the AI would
never find is available to you. You will be asked again after each thing you
play, so a turn is several of these in a row ending in a pass.

`attack` and `block` are a yes or no on Forge's proposal. The proposal is printed
under the options as `Forge proposes:`. Read it, because "Forge proposes" is
sometimes a terrible attack and option 1 is the right answer.

## What happened while you were away

Every decision after the first opens with `Since your last decision:` and Forge's
own log of events. Read it. It is the only place that tells you what a spell you
cast actually did.

That matters because Forge chooses targets, not you. A removal spell you picked
can resolve against something you did not intend, or fizzle for want of a legal
target, and either way the board state alone cannot tell you which. The log can.

During combat the state also carries an `In combat:` block listing every
attacker, its power and toughness, what it is attacking, and anything already
blocking it. Without that a block decision is a guess.

## `--why` is the point

The reasoning string is the product. A win rate tells you a deck lost. The
transcript tells you why it lost, in the seat's own words, at the moment the
seat still believed it was winning. That is the only artifact here worth
anything, and it exists only if you write it.

Say what you are playing for. Not what the card does, not which option you
picked, the engine already recorded both.

Good.

```
--why "Holding Swords for their commander, they have four untapped and I cannot race a resolved Atraxa."
--why "Mulliganing, two lands and nothing under four, this deck cannot function on a slow start."
--why "Attacking into the open mana because I lose to their next draw step anyway."
--why "Passing with three up. I would rather counter than commit into a board wipe."
```

Useless.

```
--why "casting my best spell"
--why "this is good"
--why "option 2"
--why "playing a land"
```

One or two sentences. It is trimmed at 500 characters. Write it every call, even
the boring ones, a pass with a reason is data and a pass without one is noise.

## If you are slow

Three separate clocks run and they are easy to confuse.

| Clock | Default | What happens when it runs out |
|---|---|---|
| `gauntlet act --timeout` | 120s | Your call returns `nothing to decide yet, call again`. Nothing is lost. |
| `gauntlet run --decision-timeout` | 300s | Forge's AI makes that decision for you and the game moves on. |
| `gauntlet run --game-timeout` | 900s | The game is called a draw. |

The second one is the one to care about. You have 300 seconds from when a
question is raised to when your answer lands. Miss it and the bridge falls back
to Forge's AI, which is by design. A seat that hangs must cost the run a slightly
worse decision, never a lost game.

A fallback is not a crash. The match is still running, the next question is
already waiting for you, keep going. The transcript records it as
`(Forge AI decided — no answer for cast_or_pass within 300s)` so nobody later
mistakes Forge's play for yours.

Do not stall. Do not spend three minutes reading oracle text you were already
shown. If you fall back repeatedly, the transcript is a record of Forge playing
itself and the run was a waste.

If you time out and then submit anyway, the answer is refused with `stale`
rather than applied to whatever question is open now. Call again with no
`--choice` to see where the game actually got to.

## How a game ends

`act` returns this.

```
match over. wins: {'A': 1}
```

Stop looping. Then read the result.

```bash
gauntlet summary 0920-1412-a3f1    # counts, wins, fallbacks, latency
gauntlet replay 0920-1412-a3f1     # the play by play with every --why
gauntlet replay 0920-1412-a3f1 -v  # plus every option that was on offer
```

`summary` is JSON. The fields that matter are `wins`, `draws`, `fallbacks`, and
`by_seat`, which breaks fallbacks and reasoning counts down per seat. A seat with
a high `fallbacks` number did not really play the game.

`replay` is markdown, grouped by turn, with each seat's reasoning quoted under
its play. This is what you actually read to answer "why did this deck lose".

`gauntlet status --match <id>` works while the match is still live.

## What the seat does not control

Stated plainly, because reasoning about a decision you do not have is wasted.

**You choose which spell. Forge chooses how it is cast.** Targets, modes and mana
payment all stay with Forge's AI. You can pick Swords to Plowshares, you cannot
pick what it hits. Writing `--why "killing their commander"` does not make it
target the commander.

**Attack and block are take it or leave it.** Two options, Forge's proposal or
nothing. You cannot attack with three of the five creatures Forge wants to send,
and you cannot reassign a block.

**A chosen spell can fizzle.** Forge picks targets after you have chosen the
spell, and it sometimes fails to find one. The spell does nothing and your turn
moves on. The Forge log records it, at
`~/.local/state/gauntlet/<match-id>.forge.log`.

**Mana abilities are never offered.** You do not tap lands. Paying is Forge's job
and every option you are shown is already payable.

**You cannot see hidden information.** An opponent's hand and library are counts.
This is enforced in the state builder, not by convention.

Read these as the current shape of the harness rather than as a reason to stop.
A deck that wins through them is a deck that works.

## A worked example

Seat A, three decisions, real output shape.

### Mulligan

```bash
gauntlet act --match 0920-1412-a3f1 --seat A
```

```
decision 1 (mulligan)

Before the game starts.

YOU (A) - 40 life, 92 cards in library
  Hand (7): Mountain, Mountain, Plains, Defiant Strike {W}, Sunhome Stalwart {1}{W}, Lightning Helix {R}{W}, Boros Charm {R}{W}
  Battlefield: empty
  Command zone: Feather, the Redeemed {1}{R}{W}

B - 40 life, 7 cards in hand, 92 in library
  Battlefield: empty

Cards you have not seen yet this game:
  Boros Charm {R}{W} - Instant
      Choose one -
      * Boros Charm deals 4 damage to target player or planeswalker.
      * Permanents you control gain indestructible until end of turn.
      * Target creature gains double strike until end of turn.
  Defiant Strike {W} - Instant
      Target creature gets +1/+0 until end of turn. Draw a card.
  Feather, the Redeemed {1}{R}{W} - Legendary Creature Angel 3/4
      Flying
      Whenever you cast an instant or sorcery spell that targets a creature you control, exile that card instead of putting it into your graveyard as it resolves. If you do, return it to your hand at the beginning of the next end step.
  ...

Opening hand. Keep, or mulligan? You would put 0 card(s) on the bottom if you keep.

Your options:
  [0] Mulligan
  [1] Keep
```

```bash
gauntlet act --match 0920-1412-a3f1 --seat A --id 1 --choice 1 \
  --why "Three lands, a two drop and two cantrip pumps. This is the hand the deck wants, it just needs Feather to stick."
```

### A main phase

The same call returns the next question.

```
decision 4 (cast_or_pass)

Turn 1, main1, A is the active player.

YOU (A) - 40 life, 92 cards in library
  Hand (7): Mountain, Mountain, Plains, Defiant Strike {W}, Sunhome Stalwart {1}{W}, Lightning Helix {R}{W}, Boros Charm {R}{W}
  Battlefield: empty
  Command zone: Feather, the Redeemed {1}{R}{W}

B - 40 life, 7 cards in hand, 92 in library
  Battlefield: empty

You have priority. Choose something to play, or pass.

Your options:
  [0] Pass priority
  [1] Mountain - Play land
  [2] Plains - Play land
```

```bash
gauntlet act --match 0920-1412-a3f1 --seat A --id 4 --choice 2 \
  --why "Plains first. Both my two drops are single white and the red half does not matter until Feather on turn three."
```

Next question is decision 5, the same board with one land down and only the
Mountain left to play. Answer it with `--choice 0` to pass and end the turn.

### Combat

```
decision 22 (attack)

Turn 5, combat_declare_attackers, A is the active player.

YOU (A) - 40 life, 88 cards in library
  Hand (4): Boros Charm {R}{W}, Mountain, Gods Willing {W}, Sun Titan {4}{W}{W}
  Battlefield: Plains, Plains (tapped), Mountain, Mountain, Sacred Foundry, Feather, the Redeemed (3/4), Sunhome Stalwart (2/2)

B - 36 life, 3 cards in hand, 88 in library
  Battlefield: Forest, Forest, Forest, Llanowar Elves (1/1, tapped), Questing Beast (4/4)

Declare attackers.

Your options:
  [0] Attack as Forge proposes
  [1] Do not attack

Forge proposes: feather-the-redeemed, sunhome-stalwart
```

```bash
gauntlet act --match 0920-1412-a3f1 --seat A --id 22 --choice 1 \
  --why "Not sending Feather into an untapped Questing Beast. Feather is the whole deck and four damage is not worth trading her."
```

Then keep calling. The loop is the same every time until you get
`match over`.
