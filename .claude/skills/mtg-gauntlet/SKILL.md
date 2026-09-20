---
name: mtg-gauntlet
description: Playtest a Magic&#58; The Gathering deck by actually playing games with it, using the Forge based gauntlet harness. Use whenever Matt wants to test a deck or a theory about one, play a game of Magic against another agent, or find out how a deck performs in practice - "playtest this deck", "does this deck actually work", "test whether the combo assembles", "how does this curve play out", "play a game against Forge's AI", "run these two decks against each other", "simulate some games", "which of these two decks is better". Runs real games and produces a transcript of every decision and the reasoning behind it. Not for building or editing a decklist, that is mtg-build-deck, and not for collection changes, those are mtg-add-cards and mtg-remove-cards.
---

# Playtesting with the gauntlet

Repo is `~/repos/mtg-gauntlet`. Run everything from there with
`uv run --python 3.12 gauntlet ...`, the system python is broken on this machine.

Check the install first with `gauntlet doctor`. `gauntlet decks` lists what is
playable out of the collection database.

## Pick a mode

| Question | Command | Why |
|---|---|---|
| Is this deck better than that deck, statistically | `--seat-a forge --seat-b forge --games 200` | No agent time, Forge's AI both sides, a win rate |
| Does this deck's plan hold up when someone plays it properly | `--seat-a interactive --seat-b forge` | You play, Forge is the control arm |
| Why does this matchup go the way it does | both seats `interactive` | Two agents, one close read, the best transcript |
| Hundreds of games with real judgment | `--seat-a api --seat-b api` | The Claude API plays unattended, costs tokens |

Start with a forge versus forge batch. It is free and it tells you whether the
deck is broken before an agent spends a turn on it.

```bash
gauntlet run --a "Feather Storm" --b "Temur Roar" --seat-a forge --seat-b forge --games 100
```

That one runs in the foreground and prints the win counts. Anything with an
interactive seat detaches and hands you a match id instead.

## Play a game

```bash
gauntlet run --a "Feather Storm" --b "Temur Roar"
```

Both seats default to `interactive`, so this is a two agent match. It prints the
match id. You take seat A. Launch a subagent for seat B and give it the match id,
the letter B, and this instruction.

> Read `~/repos/mtg-gauntlet/docs/PLAYING.md`. You are seat B of match
> `<id>`. Play the game out. Run everything with
> `cd ~/repos/mtg-gauntlet && uv run --python 3.12 gauntlet ...`.

Then loop on one command until it says the match is over.

```bash
gauntlet act --match <id> --seat A                             # take the question
gauntlet act --match <id> --seat A --choice 2 --why "..."      # answer, get the next
```

It blocks, so no polling. Taking a question does not consume it, call again with
no `--choice` if you lose your place. Your answer goes to the question you were
last shown, and is refused as `stale` rather than misapplied if the game moved
on without you. Answer within 300 seconds or Forge's AI
takes that decision and the transcript marks it.

`--why` is the reason the harness exists. Say what you are playing for, not what
the card does. A game played without it produced nothing but a win or a loss.

If you start a match and abandon it, run `gauntlet stop --match <id>`.

## Read the result

```bash
gauntlet summary <id>    # wins, draws, fallbacks, latency, per seat counts
gauntlet replay <id>     # play by play with every reasoning string
gauntlet replay <id> -v  # plus the full option list at each decision
gauntlet matches         # recent match ids
```

A high `fallbacks` count means a seat was too slow and Forge played for it.
Discount that run.

## Before you play

Read `docs/PLAYING.md`. It is short and it is the contract, including the four
decision kinds, what a good `--why` looks like, and what the seat does not
control. The short version of the last one is that you choose which spell to
cast, Forge still chooses targets, modes and mana payment, and attack and block
are a yes or no on Forge's proposal.

`ARCHITECTURE.md` covers how the pieces fit and which decisions are routed to a
seat versus resolved inside the engine. Read it only if you are changing the
harness.
