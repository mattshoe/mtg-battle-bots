# Feather Storm against the field

*2026-09-20 — 460 games, 23 opponents, 20 games each*

## The number

**247-213-0**, a **54%** win rate across 460 decisive games.

The 95% confidence interval is **49% to 58%**, which straddles even. On 460
games the deck is somewhere between slightly behind and moderately ahead, and the
honest summary is **about even with a lean towards winning**. The overall number
is the least interesting thing in this report. The spread is where the signal is.

Per matchup, 2 are favoured, 2 unfavoured, and 19 are
inside the noise at twenty games. The per-matchup intervals are wide, so treat
the ordering below as a ranking to investigate rather than a settled result.

## Every matchup

Sorted by win rate. The bar is the observed rate, the interval is what twenty
games actually supports.

| opponent                     | W-L-D | rate | 95% interval | turns | |
|------------------------------|------:|-----:|:------------:|------:|---|
| chaos-incarnate-precon       | 16-4-0 | 80% | 58%–92% | 20 | `████████████████····` |
| quandrix-unlimited-precon    | 15-5-0 | 75% | 53%–89% | 17 | `███████████████·····` |
| blood-rites-precon           | 14-6-0 | 70% | 48%–85% | 18 | `██████████████······` |
| counter-intelligence-precon  | 14-6-0 | 70% | 48%–85% | 19 | `██████████████······` |
| squirreled-away-precon       | 14-6-0 | 70% | 48%–85% | 19 | `██████████████······` |
| counter-blitz-precon         | 13-7-0 | 65% | 43%–82% | 20 | `█████████████·······` |
| family-matters-precon        | 13-7-0 | 65% | 43%–82% | 18 | `█████████████·······` |
| abzan-armor-precon           | 12-8-0 | 60% | 39%–78% | 18 | `████████████········` |
| blight-curse-precon          | 12-8-0 | 60% | 39%–78% | 22 | `████████████········` |
| elven-council-precon         | 12-8-0 | 60% | 39%–78% | 18 | `████████████········` |
| planeswalker-party-precon    | 12-8-0 | 60% | 39%–78% | 21 | `████████████········` |
| grave-danger-precon          | 11-9-0 | 55% | 34%–74% | 17 | `███████████·········` |
| jeskai-striker-precon        | 11-9-0 | 55% | 34%–74% | 21 | `███████████·········` |
| animated-army-precon         | 10-10-0 | 50% | 30%–70% | 17 | `██████████··········` |
| halo-proxy-astor-equipment   | 10-10-0 | 50% | 30%–70% | 18 | `██████████··········` |
| silverquill-influence-precon | 10-10-0 | 50% | 30%–70% | 18 | `██████████··········` |
| sneak-attack-precon          | 10-10-0 | 50% | 30%–70% | 19 | `██████████··········` |
| eternal-might-precon         | 8-12-0 | 40% | 22%–61% | 18 | `████████············` |
| world-shaper-precon          | 8-12-0 | 40% | 22%–61% | 20 | `████████············` |
| temur-roar-precon            | 7-13-0 | 35% | 18%–57% | 17 | `███████·············` |
| lorehold-spirit-precon       | 6-14-0 | 30% | 15%–52% | 18 | `██████··············` |
| sultai-arisen-precon         | 5-15-0 | 25% | 11%–47% | 18 | `█████···············` |
| veloci-ramp-tor-precon       | 4-16-0 | 20% | 8%–42% | 18 | `████················` |

## What stands out

**Beats, with the interval clear of even:**

- `chaos-incarnate-precon` — 16-4-0, 80% (58%–92%)
- `quandrix-unlimited-precon` — 15-5-0, 75% (53%–89%)

**Loses to, with the interval clear of even:**

- `sultai-arisen-precon` — 5-15-0, 25% (11%–47%)
- `veloci-ramp-tor-precon` — 4-16-0, 20% (8%–42%)

## The pattern, and it is a clear one

Feather Storm's win rate falls off a cliff the longer a game runs.

| game length | games | win rate | |
|---|--:|--:|---|
| 0-14 turns | 59 | 69% | `██████████████······` |
| 15-17 turns | 131 | 60% | `████████████········` |
| 18-21 turns | 150 | 52% | `██████████··········` |
| 22+ turns | 120 | 42% | `████████············` |

That is **69% in the shortest games down to 42% in the
longest**, on 460 games. It is the strongest signal in the whole run and it is
exactly what you would expect from nineteen copies of a 2/2 flier: the deck is
racing, and every extra turn is one more turn for the opponent to land something
bigger than a Hawk.

The matchup table says the same thing from the other side. Sorted by theme, the
four worst matchups are all decks that ramp into large creatures or grind value
out of a graveyard, and the two best are a goad deck that makes creatures attack
each other and a counters deck that also starts small.

| | matchup | theme |
|---|---|---|
| win | `chaos-incarnate-precon` | Goad + chaos |
| win | `quandrix-unlimited-precon` | +1/+1 counters + expanding X-spells |
| loss | `temur-roar-precon` | Dragon typal |
| loss | `lorehold-spirit-precon` | Spirits + graveyard recursion |
| loss | `sultai-arisen-precon` | Graveyard value engine |
| loss | `veloci-ramp-tor-precon` | Dinosaur typal + big-mana ramp |

So the question this run actually poses is not "is the deck good". It is whether
the deck can close before turn eighteen, because after that it is losing.

## Game length distribution

Median **18 turns**, quartiles 16 and 22, range 11 to 71.

```
 10-14  ###########                              59
 15-19  ######################################## 217
 20-24  ######################                   121
 25-29  #########                                48
 30-34  ##                                       12
 35-39                                           2
 70-74                                           1
```

The median game is 18 turns, which sits in the band where the
deck is already below even.

## How to read this, and what it is not

Both seats were played by **Forge's own AI**, not by an agent. That is the right
tool for 460 games and the wrong tool for a subtle deck. Forge's documentation
says its AI is good at aggro and midrange and "pretty bad for most combo decks",
so a deck whose plan is to attack with a wide board is close to the AI's best
case, and a deck that needs to hold up interaction is close to its worst.

Read the numbers as a comparison of how well each deck's plan executes on
autopilot. A matchup that looks bad here may be fine with a person or an agent
steering, and the way to find out is to play that one matchup interactively.

Every game is reproducible. The seed is `2026` and each matchup's match id is in `data/results.csv`.

## Where to look next

| File | What is in it |
|---|---|
| `matchups/<opponent>.md` | One file per opponent: every game, who won, how long |
| `deck/feather-storm.md` | The decklist under test, its curve and composition |
| `deck/field.md` | The 23 opponents, commanders and brackets |
| `data/results.csv` | The table above, for a spreadsheet |
| `data/sweep.json` | Raw sweep output |
| `build_report.py` | Regenerates everything here from the data |

To read a single game in full:

```bash
cd ~/repos/mtg-gauntlet
uv run --python 3.12 gauntlet replay <match-id>
```
