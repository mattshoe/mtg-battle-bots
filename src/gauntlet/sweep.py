"""Running one deck against a field.

A single match tells you almost nothing. Commander games swing on who drew their
ramp, and a deck that lost once may be fine. The useful question is a win rate
over a field of opponents, and that means a lot of games.

So this runs pairings in parallel, each in its own JVM. The JVM is the reason:
Forge takes something like forty seconds to load its card database and a couple
of seconds per game after that, so the startup cost only makes sense amortised
over a batch, and several batches only make sense running at once.
"""

from __future__ import annotations

import concurrent.futures
import os
import time
from dataclasses import dataclass, field

from . import match as matchmod
from .transcript import Transcript


@dataclass(slots=True)
class Pairing:
    """One deck against one opponent, over some number of games."""

    deck: str
    opponent: str
    games: int
    seed: int | None = None

    wins: int = 0
    losses: int = 0
    draws: int = 0
    turns: list[int] = field(default_factory=list)
    match_id: str = ""
    error: str = ""

    @property
    def played(self) -> int:
        return self.wins + self.losses + self.draws

    @property
    def win_rate(self) -> float:
        decisive = self.wins + self.losses
        return self.wins / decisive if decisive else 0.0

    @property
    def median_turns(self) -> int:
        if not self.turns:
            return 0
        ordered = sorted(self.turns)
        return ordered[len(ordered) // 2]


def default_workers(agent_seats: int = 0) -> int:
    """How many pairings to run at once.

    With Forge on both sides the limit is memory: each worker is a JVM holding
    the whole card database, and six of those is already several gigabytes.

    With agent seats the limit is upstream instead. Every bridged seat holds a
    live model session, so six workers against two agent seats is twelve
    concurrent sessions, which is where rate limiting starts. Back off.
    """
    if agent_seats:
        return max(1, min(4, 8 // max(1, agent_seats)))
    return max(1, min(6, (os.cpu_count() or 2) // 2))


def run_pairing(
    pairing: Pairing,
    *,
    seat_deck: str = "forge",
    seat_opponent: str = "forge",
    game_format: str = "Commander",
    owner: str | None = None,
    game_timeout: int = 900,
    decision_timeout: int = 300,
    transcript: Transcript | None = None,
) -> Pairing:
    """Play one pairing to completion and fill in its results.

    The deck under test is always seat A, so a results table never has to
    explain which side it is reporting.
    """
    try:
        planned = matchmod.plan(
            [
                matchmod.SeatSpec(seat="A", deck=pairing.deck, controller=seat_deck),
                matchmod.SeatSpec(seat="B", deck=pairing.opponent, controller=seat_opponent),
            ],
            owner=owner,
            game_format=game_format,
            seed=pairing.seed,
            games=pairing.games,
            game_timeout=game_timeout,
            decision_timeout=decision_timeout,
        )
        result = matchmod.run(planned, transcript=transcript)
    except Exception as exc:
        pairing.error = f"{type(exc).__name__}: {exc}"
        return pairing

    pairing.match_id = result.match_id
    for game in result.games:
        pairing.turns.append(int(game.get("turns", 0)))
        if game.get("draw"):
            pairing.draws += 1
        elif game.get("winner") == "A":
            pairing.wins += 1
        else:
            pairing.losses += 1
    if result.error:
        pairing.error = result.error
    return pairing


def run_sweep(
    deck: str,
    opponents: list[str],
    *,
    games: int = 3,
    seed: int | None = None,
    workers: int | None = None,
    owner: str | None = None,
    game_format: str = "Commander",
    game_timeout: int = 900,
    seat_deck: str = "forge",
    seat_opponent: str = "forge",
    decision_timeout: int = 300,
    on_done=None,
) -> list[Pairing]:
    """Play a deck against every opponent, in parallel."""
    pairings = [
        # Same seed across pairings on purpose. It does not make the games
        # identical, the decks differ, but it does mean a rerun of the sweep
        # reproduces exactly, which is what makes a before-and-after comparison
        # worth anything.
        Pairing(deck=deck, opponent=opp, games=games, seed=seed)
        for opp in opponents
    ]

    agent_seats = sum(1 for k in (seat_deck, seat_opponent) if k in ("sdk", "api"))
    workers = workers or default_workers(agent_seats)
    done: list[Pairing] = []

    # One transcript per worker thread is tempting but wrong. The Transcript is
    # already thread safe and a single database keeps a sweep queryable as one
    # thing afterwards.
    transcript = Transcript()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    run_pairing,
                    p,
                    owner=owner,
                    game_format=game_format,
                    game_timeout=game_timeout,
                    seat_deck=seat_deck,
                    seat_opponent=seat_opponent,
                    decision_timeout=decision_timeout,
                    transcript=transcript,
                ): p
                for p in pairings
            }
            for future in concurrent.futures.as_completed(futures):
                finished = future.result()
                done.append(finished)
                if on_done is not None:
                    on_done(finished, len(done), len(pairings))
    finally:
        transcript.close()

    # Sort by win rate so the table reads as a ranking, with the worst matchups
    # at the bottom where someone looking for a problem will find them.
    done.sort(key=lambda p: (-p.win_rate, p.opponent))
    return done


def format_table(deck: str, results: list[Pairing], elapsed: float) -> str:
    """The sweep as something a person reads and acts on."""
    played = sum(p.played for p in results)
    wins = sum(p.wins for p in results)
    losses = sum(p.losses for p in results)
    draws = sum(p.draws for p in results)
    decisive = wins + losses

    width = max((len(p.opponent) for p in results), default=10)
    lines = [
        f"{deck} over {played} games against {len(results)} decks, {elapsed:.0f}s",
        "",
        f"{'opponent':<{width}}  {'W-L-D':>9}  {'rate':>5}  {'turns':>5}",
        f"{'-' * width}  {'-' * 9}  {'-' * 5}  {'-' * 5}",
    ]
    for p in results:
        if p.error:
            lines.append(f"{p.opponent:<{width}}  {'error':>9}  {p.error[:40]}")
            continue
        record = f"{p.wins}-{p.losses}-{p.draws}"
        lines.append(
            f"{p.opponent:<{width}}  {record:>9}  {p.win_rate:>5.0%}  {p.median_turns:>5}"
        )

    overall = wins / decisive if decisive else 0.0
    lines += ["", f"overall {wins}-{losses}-{draws}, {overall:.0%} of decisive games"]
    if draws:
        # A draw here is usually a game that hit the clock, not a real draw.
        # Treating it as half a win would flatter a deck that stalls out.
        lines.append(f"{draws} draw(s), most likely games that hit the timeout")
    return "\n".join(lines)


def timed_sweep(deck: str, opponents: list[str], **kwargs) -> tuple[list[Pairing], float]:
    started = time.monotonic()
    results = run_sweep(deck, opponents, **kwargs)
    return results, time.monotonic() - started
