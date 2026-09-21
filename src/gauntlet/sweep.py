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
import threading
import time
from dataclasses import dataclass, field

from . import match as matchmod
from .budget import Budget
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
    #: Set when a seat ran out of capacity. The numbers are not usable.
    exhausted: bool = False
    #: Share of decisions the seat did not answer. A high one means Forge
    #: played the games, whatever the record says.
    fallback_rate: float = 0.0
    decisions: int = 0

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
    free = max(1, min(6, (os.cpu_count() or 2) // 2))
    if not agent_seats:
        return free
    # Never more than the free ceiling. An independent formula here gave a
    # two-core runner four agent workers against one Forge worker, which is
    # backwards: an agent pairing costs strictly more than a Forge one.
    return max(1, min(free, 4 // agent_seats))


def _seat_options(controller: str, model: str) -> dict[str, str]:
    """Options for a seat of this kind. Only paid seats take a model."""
    if model and controller in ("api", "sdk"):
        return {"model": model}
    return {}


def run_pairing(
    pairing: Pairing,
    *,
    halt: threading.Event | None = None,
    seat_deck: str = "forge",
    seat_opponent: str = "forge",
    game_format: str = "Commander",
    owner: str | None = None,
    game_timeout: int = 900,
    decision_timeout: int = 300,
    model: str = "",
    transcript: Transcript | None = None,
    budget: Budget | None = None,
) -> Pairing:
    """Play one pairing to completion and fill in its results.

    The deck under test is always seat A, so a results table never has to
    explain which side it is reporting.
    """
    if halt is not None and halt.is_set():
        # An earlier pairing lost its seat. Anything played now would be Forge
        # wearing an agent's name.
        pairing.error = "skipped, an earlier pairing ran out of capacity"
        pairing.exhausted = True
        return pairing

    try:
        planned = matchmod.plan(
            [
                # The model reaches the seats, not just the budget. It used to
                # price the cap and never be passed on, so a sweep billed one
                # model's rates and played another's.
                matchmod.SeatSpec(
                    seat="A",
                    deck=pairing.deck,
                    controller=seat_deck,
                    options=_seat_options(seat_deck, model),
                ),
                matchmod.SeatSpec(
                    seat="B",
                    deck=pairing.opponent,
                    controller=seat_opponent,
                    options=_seat_options(seat_opponent, model),
                ),
            ],
            owner=owner,
            game_format=game_format,
            seed=pairing.seed,
            games=pairing.games,
            game_timeout=game_timeout,
            decision_timeout=decision_timeout,
        )
        result = matchmod.run(planned, transcript=transcript, budget=budget)
    except Exception as exc:
        pairing.error = f"{type(exc).__name__}: {exc}"
        return pairing

    pairing.match_id = result.match_id
    for game in result.games:
        pairing.turns.append(int(game.get("turns", 0)))
        winner = game.get("winner")
        if game.get("draw") or winner is None:
            # No winner and not a draw means the game did not finish, which is
            # not a loss. Counting it as one quietly depressed every win rate.
            pairing.draws += 1
        elif winner == "A":
            pairing.wins += 1
        else:
            pairing.losses += 1
    if result.error:
        pairing.error = result.error
    if getattr(result, "exhausted", None):
        pairing.exhausted = True
    pairing.decisions = result.decisions
    pairing.fallback_rate = result.fallback_rate
    if not result.trustworthy:
        pairing.exhausted = True
        if not pairing.error:
            pairing.error = result.untrustworthy_because
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
    model: str = "",
    budget: Budget | None = None,
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

    # Cancelling a future only works before it starts, and with a worker already
    # mid-pairing the rest of the sweep ran regardless. A flag every pairing
    # checks on the way in closes that window.
    halt = threading.Event()

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
                    model=model,
                    transcript=transcript,
                    budget=budget,
                    halt=halt,
                ): p
                for p in pairings
            }
            for future in concurrent.futures.as_completed(futures):
                finished = future.result()
                done.append(finished)
                if on_done is not None:
                    on_done(finished, len(done), len(pairings))

                # One exhausted seat means every later pairing would be played
                # by Forge wearing an agent's name. Cancel the rest rather than
                # spend hours producing a result that reads as an agent run.
                if finished.exhausted:
                    halt.set()
                    for pending in futures:
                        pending.cancel()
                    # Account for the rest of the field rather than dropping it.
                    # A sweep that stopped early used to report only what it
                    # finished, so eight opponents came back as two with no
                    # mention of the six.
                    reported = {p.opponent for p in done}
                    for skipped in pairings:
                        if skipped.opponent not in reported:
                            skipped.exhausted = True
                            skipped.error = "not played, the sweep stopped before reaching it"
                            done.append(skipped)
                    break
    finally:
        transcript.close()

    # Sort by win rate so the table reads as a ranking, with the worst matchups
    # at the bottom where someone looking for a problem will find them.
    done.sort(key=lambda p: (-p.win_rate, p.opponent))
    return done


def format_table(deck: str, results: list[Pairing], elapsed: float) -> str:
    """The sweep as something a person reads and acts on."""
    played = sum(p.played for p in results)
    # Only pairings the seat actually played count toward the headline. Summing
    # void ones reported a deck that went 1-3 as 79%, with the warning printed
    # underneath the number.
    counted = [p for p in results if not p.exhausted]
    wins = sum(p.wins for p in counted)
    losses = sum(p.losses for p in counted)
    draws = sum(p.draws for p in counted)
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
        lines.append(f"{p.opponent:<{width}}  {record:>9}  {p.win_rate:>5.0%}  {p.median_turns:>5}")

    overall = wins / decisive if decisive else 0.0
    excluded = len(results) - len(counted)
    summary = f"overall {wins}-{losses}-{draws}, {overall:.0%} of decisive games"
    if excluded:
        summary += f" (excludes {excluded} pairing(s) the seat did not play)"
    lines += ["", summary]

    spent = [p for p in results if p.exhausted]
    if spent:
        lines += [
            "",
            "WARNING: one or more pairings were not played by the seat named. Some or",
            "all of these games were Forge's AI. Do not read them as an agent result.",
        ]
        for p in spent:
            lines.append(f"  {p.opponent}: {p.error or 'seat ran out of capacity'}")

    noisy = [p for p in results if not p.exhausted and p.fallback_rate > 0.05]
    if noisy:
        lines += ["", "Some decisions fell back to Forge:"]
        for p in noisy:
            lines.append(f"  {p.opponent}: {p.fallback_rate:.0%} of {p.decisions}")
    if draws:
        # A draw here is usually a game that hit the clock, not a real draw.
        # Treating it as half a win would flatter a deck that stalls out.
        lines.append(f"{draws} draw(s), most likely games that hit the timeout")
    return "\n".join(lines)


def timed_sweep(deck: str, opponents: list[str], **kwargs) -> tuple[list[Pairing], float]:
    started = time.monotonic()
    results = run_sweep(deck, opponents, **kwargs)
    return results, time.monotonic() - started
