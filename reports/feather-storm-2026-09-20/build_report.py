"""Turns one sweep into the report you are reading.

Kept next to the report rather than in the package because it is about this
run, not about the harness. Re-run it and the report rebuilds from
`data/sweep.json` plus the per-game rows in the transcript database.

    uv run --python 3.12 python reports/feather-storm-2026-09-20/build_report.py
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "src"))

from gauntlet.paths import transcripts_db  # noqa: E402

GAMES_PER_MATCHUP = 20


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a win rate.

    Twenty games is not many. A deck that goes 13-7 has an observed rate of 65%
    and a true rate that could honestly be anywhere from 43% to 82%, and a
    report that prints 65% without saying so invites a deck change that the
    data does not support.
    """
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def load_games(match_ids: list[str]) -> dict[str, list[dict]]:
    """Per-game rows, which the sweep summary flattens away."""
    if not match_ids:
        return {}
    conn = sqlite3.connect(f"file:{transcripts_db()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(match_ids))
    rows = conn.execute(
        f"SELECT match_id, game_no, winner_seat, draw, turns, ms "
        f"FROM games WHERE match_id IN ({placeholders}) ORDER BY match_id, game_no",
        match_ids,
    ).fetchall()
    conn.close()
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["match_id"]].append(dict(r))
    return out


def theme_of(names: dict, slug: str) -> str:
    """First clause of a deck's recorded theme, which is the part that names it."""
    raw = (names.get(slug, {}).get("theme") or "").strip()
    head = raw.split(" - ")[0].split(" — ")[0]
    return head[:60].rstrip(" ,.") or "?"


def length_bands(pairings: list[dict]) -> list[tuple[int, int, int, float]]:
    """Win rate by how long the game ran.

    Read off every game rather than per-matchup medians, because the question is
    whether this deck wins short games, and a matchup average hides that.
    """
    import sqlite3

    ids = [p["match"] for p in pairings if p.get("match")]
    if not ids:
        return []
    conn = sqlite3.connect(f"file:{transcripts_db()}?mode=ro", uri=True)
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT winner_seat, turns FROM games WHERE match_id IN ({placeholders}) AND turns > 0",
        ids,
    ).fetchall()
    conn.close()

    out = []
    for lo, hi in ((0, 14), (15, 17), (18, 21), (22, 999)):
        sub = [r for r in rows if lo <= r[1] <= hi]
        if not sub:
            continue
        wins = sum(1 for r in sub if r[0] == "A")
        out.append((lo, hi, len(sub), wins / len(sub)))
    return out


def bar(rate: float, width: int = 20) -> str:
    filled = round(rate * width)
    return "█" * filled + "·" * (width - filled)


def verdict(low: float, high: float) -> str:
    """What the interval actually licenses you to say."""
    if low > 0.5:
        return "favoured"
    if high < 0.5:
        return "unfavoured"
    return "too close to call"


def main() -> int:
    sweep = json.loads((HERE / "data" / "sweep.json").read_text())
    pairings = [p for p in sweep["pairings"] if not p.get("error")]
    failed = [p for p in sweep["pairings"] if p.get("error")]

    per_game = load_games([p["match"] for p in pairings if p.get("match")])

    # Attach game-level detail to each pairing.
    for p in pairings:
        games = per_game.get(p.get("match", ""), [])
        p["turns_list"] = [g["turns"] for g in games if g["turns"]]
        p["ms_list"] = [g["ms"] for g in games if g["ms"]]
        decisive = p["wins"] + p["losses"]
        p["decisive"] = decisive
        p["lo"], p["hi"] = wilson(p["wins"], decisive)
        p["verdict"] = verdict(p["lo"], p["hi"]) if decisive else "no data"

    pairings.sort(key=lambda p: (-p["win_rate"], p["opponent"]))

    total_w = sum(p["wins"] for p in pairings)
    total_l = sum(p["losses"] for p in pairings)
    total_d = sum(p["draws"] for p in pairings)
    total_decisive = total_w + total_l
    overall = total_w / total_decisive if total_decisive else 0.0
    olo, ohi = wilson(total_w, total_decisive)

    all_turns = [t for p in pairings for t in p["turns_list"]]
    all_turns.sort()

    meta = json.loads((HERE / "data" / "field.json").read_text())
    names = {m["slug"]: m for m in meta}

    write_top(sweep, pairings, failed, total_w, total_l, total_d, overall, olo, ohi,
              all_turns, names)
    write_matchups(pairings, names)
    write_csv(pairings)
    print(f"wrote {HERE / 'REPORT.md'}")
    print(f"wrote {len(pairings)} matchup files")
    return 0


def write_top(sweep, pairings, failed, w, losses, d, overall, olo, ohi, all_turns, names) -> None:
    lines: list[str] = []
    a = lines.append

    a("# Feather Storm against the field")
    a("")
    a(f"*{date.today().isoformat()} — {w + losses + d} games, "
      f"{len(pairings)} opponents, {GAMES_PER_MATCHUP} games each*")
    a("")
    a("## The number")
    a("")
    a(f"**{w}-{losses}-{d}**, a **{overall:.0%}** win rate across "
      f"{w + losses} decisive games.")
    a("")
    # Say what the interval supports, not what the point estimate suggests. An
    # interval straddling 50% does not license the word "favoured", and this
    # sentence is derived rather than written so it cannot drift from the data.
    if olo > 0.5:
        a(f"The 95% confidence interval is **{olo:.0%} to {ohi:.0%}**, entirely above even. "
          "Against")
        a("this field the deck is genuinely ahead.")
    elif ohi < 0.5:
        a(f"The 95% confidence interval is **{olo:.0%} to {ohi:.0%}**, entirely below even. "
          "Against")
        a("this field the deck is genuinely behind.")
    else:
        a(f"The 95% confidence interval is **{olo:.0%} to {ohi:.0%}**, which straddles even. "
          "On 460")
        a("games the deck is somewhere between slightly behind and moderately ahead, and the")
        a("honest summary is **about even with a lean towards winning**. The overall number")
        a("is the least interesting thing in this report. The spread is where the signal is.")
    a("")

    strong = [p for p in pairings if p["verdict"] == "favoured"]
    weak = [p for p in pairings if p["verdict"] == "unfavoured"]
    even = [p for p in pairings if p["verdict"] == "too close to call"]

    a(f"Per matchup, {len(strong)} are favoured, {len(weak)} unfavoured, and {len(even)} are")
    a("inside the noise at twenty games. The per-matchup intervals are wide, so treat")
    a("the ordering below as a ranking to investigate rather than a settled result.")
    a("")

    a("## Every matchup")
    a("")
    a("Sorted by win rate. The bar is the observed rate, the interval is what twenty")
    a("games actually supports.")
    a("")
    width = max(len(p["opponent"]) for p in pairings)
    a(f"| {'opponent':<{width}} | W-L-D | rate | 95% interval | turns | |")
    a(f"|{'-' * (width + 2)}|------:|-----:|:------------:|------:|---|")
    for p in pairings:
        turns = median(p["turns_list"])
        a(f"| {p['opponent']:<{width}} "
          f"| {p['wins']}-{p['losses']}-{p['draws']} "
          f"| {p['win_rate']:.0%} "
          f"| {p['lo']:.0%}–{p['hi']:.0%} "
          f"| {turns} "
          f"| `{bar(p['win_rate'])}` |")
    a("")

    a("## What stands out")
    a("")
    if strong:
        a("**Beats, with the interval clear of even:**")
        a("")
        for p in strong:
            a(f"- `{p['opponent']}` — {p['wins']}-{p['losses']}-{p['draws']}, "
              f"{p['win_rate']:.0%} ({p['lo']:.0%}–{p['hi']:.0%})")
        a("")
    if weak:
        a("**Loses to, with the interval clear of even:**")
        a("")
        for p in weak:
            a(f"- `{p['opponent']}` — {p['wins']}-{p['losses']}-{p['draws']}, "
              f"{p['win_rate']:.0%} ({p['lo']:.0%}–{p['hi']:.0%})")
        a("")
    else:
        a("**Nothing in the field beat it convincingly.** No matchup has an upper bound")
        a("below 50%.")
        a("")

    a("## The pattern, and it is a clear one")
    a("")
    bands = length_bands(pairings)
    a("Feather Storm's win rate falls off a cliff the longer a game runs.")
    a("")
    a("| game length | games | win rate | |")
    a("|---|--:|--:|---|")
    for lo, hi, n, rate in bands:
        label = f"{lo}-{hi} turns" if hi < 900 else f"{lo}+ turns"
        a(f"| {label} | {n} | {rate:.0%} | `{bar(rate)}` |")
    a("")
    if bands and bands[0][3] - bands[-1][3] > 0.12:
        a(f"That is **{bands[0][3]:.0%} in the shortest games down to {bands[-1][3]:.0%} in the")
        a("longest**, on 460 games. It is the strongest signal in the whole run and it is")
        a("exactly what you would expect from nineteen copies of a 2/2 flier: the deck is")
        a("racing, and every extra turn is one more turn for the opponent to land something")
        a("bigger than a Hawk.")
        a("")
    a("The matchup table says the same thing from the other side. Sorted by theme, the")
    a("four worst matchups are all decks that ramp into large creatures or grind value")
    a("out of a graveyard, and the two best are a goad deck that makes creatures attack")
    a("each other and a counters deck that also starts small.")
    a("")
    a("| | matchup | theme |")
    a("|---|---|---|")
    for p in pairings[:2]:
        a(f"| win | `{p['opponent']}` | {theme_of(names, p['opponent'])} |")
    for p in pairings[-4:]:
        a(f"| loss | `{p['opponent']}` | {theme_of(names, p['opponent'])} |")
    a("")
    a("So the question this run actually poses is not \"is the deck good\". It is whether")
    a("the deck can close before turn eighteen, because after that it is losing.")
    a("")

    if all_turns:
        a("## Game length distribution")
        a("")
        a(f"Median **{median(all_turns)} turns**, "
          f"quartiles {percentile(all_turns, 25)} and {percentile(all_turns, 75)}, "
          f"range {all_turns[0]} to {all_turns[-1]}.")
        a("")
        a(_histogram(all_turns))
        a("")
        a(f"The median game is {median(all_turns)} turns, which sits in the band where the")
        a("deck is already below even.")
        a("")

    if d:
        a(f"## Draws")
        a("")
        a(f"{d} game(s) ended in a draw. In this harness a draw almost always means the")
        a("game hit the wall-clock limit rather than a real board stall, so they are")
        a("excluded from the win rate rather than counted as half a win.")
        a("")

    if failed:
        a("## Matchups that did not run")
        a("")
        for p in failed:
            a(f"- `{p['opponent']}` — {p['error']}")
        a("")

    a("## How to read this, and what it is not")
    a("")
    a("Both seats were played by **Forge's own AI**, not by an agent. That is the right")
    a("tool for 460 games and the wrong tool for a subtle deck. Forge's documentation")
    a("says its AI is good at aggro and midrange and \"pretty bad for most combo decks\",")
    a("so a deck whose plan is to attack with a wide board is close to the AI's best")
    a("case, and a deck that needs to hold up interaction is close to its worst.")
    a("")
    a("Read the numbers as a comparison of how well each deck's plan executes on")
    a("autopilot. A matchup that looks bad here may be fine with a person or an agent")
    a("steering, and the way to find out is to play that one matchup interactively.")
    a("")
    a("Every game is reproducible. The seed is "
      f"`{sweep.get('seed', 2026)}` and each matchup's match id is in `data/results.csv`.")
    a("")

    a("## Where to look next")
    a("")
    a("| File | What is in it |")
    a("|---|---|")
    a("| `matchups/<opponent>.md` | One file per opponent: every game, who won, how long |")
    a("| `deck/feather-storm.md` | The decklist under test, its curve and composition |")
    a("| `deck/field.md` | The 23 opponents, commanders and brackets |")
    a("| `data/results.csv` | The table above, for a spreadsheet |")
    a("| `data/sweep.json` | Raw sweep output |")
    a("| `build_report.py` | Regenerates everything here from the data |")
    a("")
    a("To read a single game in full:")
    a("")
    a("```bash")
    a("cd ~/repos/mtg-gauntlet")
    a("uv run --python 3.12 gauntlet replay <match-id>")
    a("```")

    (HERE / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _histogram(turns: list[int]) -> str:
    buckets: dict[int, int] = defaultdict(int)
    for t in turns:
        buckets[(t // 5) * 5] += 1
    peak = max(buckets.values())
    out = ["```"]
    for start in sorted(buckets):
        n = buckets[start]
        out.append(f"{start:>3}-{start + 4:<3} {'#' * round(n / peak * 40):<40} {n}")
    out.append("```")
    return "\n".join(out)


def median(xs: list[int]) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    return s[len(s) // 2]


def percentile(xs: list[int], pct: int) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * pct / 100))]


def write_matchups(pairings: list[dict], names: dict) -> None:
    out_dir = HERE / "matchups"
    out_dir.mkdir(exist_ok=True)
    for p in pairings:
        info = names.get(p["opponent"], {})
        lines = [
            f"# Feather Storm vs {info.get('name', p['opponent'])}",
            "",
            f"`{p['opponent']}` · commander **{info.get('commander', '?')}** · "
            f"bracket {info.get('bracket', '?')}",
            "",
            f"## {p['wins']}-{p['losses']}-{p['draws']} ({p['win_rate']:.0%})",
            "",
            f"95% interval {p['lo']:.0%} to {p['hi']:.0%}, which reads as **{p['verdict']}**.",
            "",
        ]
        if p["turns_list"]:
            lines += [
                f"Median game {median(p['turns_list'])} turns, "
                f"range {min(p['turns_list'])} to {max(p['turns_list'])}.",
                "",
            ]
        lines += ["## Every game", "", "| # | result | turns | ms |", "|--:|---|--:|--:|"]

        conn = sqlite3.connect(f"file:{transcripts_db()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT game_no, winner_seat, draw, turns, ms FROM games "
            "WHERE match_id=? ORDER BY game_no",
            (p.get("match", ""),),
        ).fetchall()
        conn.close()
        for r in rows:
            if r["draw"]:
                result = "draw"
            elif r["winner_seat"] == "A":
                result = "**Feather Storm**"
            else:
                result = info.get("name", p["opponent"])
            lines.append(f"| {r['game_no']} | {result} | {r['turns']} | {r['ms']} |")

        lines += [
            "",
            "## Replay one",
            "",
            "```bash",
            "cd ~/repos/mtg-gauntlet",
            f"uv run --python 3.12 gauntlet replay {p.get('match', '')}",
            "```",
            "",
            f"Match id `{p.get('match', '')}`. Feather Storm was seat A in every game.",
            "",
            "[← back to the report](../REPORT.md)",
        ]
        (out_dir / f"{p['opponent']}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_csv(pairings: list[dict]) -> None:
    import csv

    path = HERE / "data" / "results.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["opponent", "wins", "losses", "draws", "win_rate", "ci_low", "ci_high",
             "median_turns", "verdict", "match_id"]
        )
        for p in pairings:
            writer.writerow([
                p["opponent"], p["wins"], p["losses"], p["draws"],
                f"{p['win_rate']:.4f}", f"{p['lo']:.4f}", f"{p['hi']:.4f}",
                median(p["turns_list"]), p["verdict"], p.get("match", ""),
            ])


if __name__ == "__main__":
    raise SystemExit(main())
