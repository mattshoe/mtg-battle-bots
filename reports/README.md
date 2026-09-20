# Reports

Finished playtest runs. Each directory is one run and holds everything needed to
read it, check it, or reproduce it.

| Run | What it was | Verdict |
|---|---|---|
| `feather-storm-2026-09-20/` | Feather Storm vs 23 decks, 20 games each, Forge AI both sides | valid |
| `feather-storm-vs-kayla-agents/` | Same deck vs 8 decks, agents both sides | **void**, a seat hit a session limit and 79.5% of decisions silently fell back to Forge |

A run directory usually contains:

```
REPORT.md          the top-level read
matchups/          one file per opponent, every game, replayable match ids
deck/              the decklist under test and the field it faced
data/              sweep.json, results.csv, the raw numbers
build_report.py    regenerates everything above from data/
```

Every game is reproducible. The seed, both decklists, the routing policy, the
Forge version and the bridge revision are all recorded in the transcript
database, and `gauntlet replay <match-id>` prints any single game in full.

The void run is kept rather than deleted. It is the reason `SeatExhausted`
exists, and a worked example of the failure mode this harness is built to make
impossible.
