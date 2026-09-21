"""`run_pairing`, driven for real.

Both existing sweep tests monkeypatch this function away, so the code that turns
a MatchResult into the win/loss/draw numbers every sweep reports had never run
under test. A mutation swapping wins and losses survived, and so did deleting
the trustworthiness guard entirely.
"""

from __future__ import annotations

import pytest

from gauntlet import sweep as sweepmod
from gauntlet.server import MatchResult


@pytest.fixture
def stub_run(monkeypatch):
    """Replace only the match, so the pairing logic itself is exercised."""
    captured: dict = {}

    def make(result: MatchResult):
        def fake_run(plan_, **kw):
            captured["plan"] = plan_
            captured["kwargs"] = kw
            result.match_id = plan_.match_id
            return result

        monkeypatch.setattr(sweepmod.matchmod, "run", fake_run)
        monkeypatch.setattr(sweepmod.matchmod, "plan", lambda specs, **kw: _FakePlan(specs, **kw))
        return captured

    return make


class _FakePlan:
    def __init__(self, specs, **kw):
        self.specs = specs
        self.match_id = "p1"
        self.kw = kw


def _result(games: list[dict], **kw) -> MatchResult:
    r = MatchResult(match_id="p1", games=games, **kw)
    # A seat that answered everything, so trustworthiness is not the thing
    # under test here.
    r.expected_seats = {"A"}
    r.per_seat = {"A": (50, 0)}
    return r


def test_wins_losses_and_draws_are_counted_from_the_right_side(stub_run) -> None:
    """Seat A is always the deck under test. Swapping the two survived a
    mutation, which would invert every win rate this project has printed."""
    stub_run(
        _result(
            [
                {"game": 1, "winner": "A", "draw": False, "turns": 10},
                {"game": 2, "winner": "A", "draw": False, "turns": 11},
                {"game": 3, "winner": "B", "draw": False, "turns": 12},
                {"game": 4, "winner": None, "draw": True, "turns": 13},
            ]
        )
    )
    p = sweepmod.run_pairing(sweepmod.Pairing(deck="mine", opponent="theirs", games=4))

    assert (p.wins, p.losses, p.draws) == (2, 1, 1)
    assert p.win_rate == pytest.approx(2 / 3)
    assert p.median_turns == 12


def test_an_unfinished_game_is_not_a_loss(stub_run) -> None:
    """No winner and not a draw means the game did not finish. Scoring it a
    loss quietly depressed every win rate."""
    stub_run(_result([{"game": 1, "winner": None, "draw": False, "turns": 40}]))
    p = sweepmod.run_pairing(sweepmod.Pairing(deck="mine", opponent="theirs", games=1))
    assert p.losses == 0
    assert p.draws == 1


def test_an_untrustworthy_result_marks_the_pairing_void(stub_run) -> None:
    """Deleting this guard entirely survived a mutation."""
    bad = _result([{"game": 1, "winner": "A", "draw": False, "turns": 9}])
    bad.per_seat = {"A": (50, 50)}
    stub_run(bad)

    p = sweepmod.run_pairing(sweepmod.Pairing(deck="mine", opponent="theirs", games=1))
    assert p.exhausted, "a pairing Forge played was reported as an agent result"
    assert p.error


def test_the_model_reaches_the_seats_and_not_only_the_budget(stub_run) -> None:
    """sweep --model priced the cap and never reached the players, so a run
    billed one model's rates and played another's."""
    captured = stub_run(_result([{"game": 1, "winner": "A", "draw": False, "turns": 9}]))
    sweepmod.run_pairing(
        sweepmod.Pairing(deck="mine", opponent="theirs", games=1),
        seat_deck="sdk",
        seat_opponent="sdk",
        model="claude-opus-5",
    )
    specs = {s.seat: s for s in captured["plan"].specs}
    assert specs["A"].options["model"] == "claude-opus-5"
    assert specs["B"].options["model"] == "claude-opus-5"


def test_a_forge_seat_is_not_given_a_model(stub_run) -> None:
    captured = stub_run(_result([{"game": 1, "winner": "A", "draw": False, "turns": 9}]))
    sweepmod.run_pairing(
        sweepmod.Pairing(deck="mine", opponent="theirs", games=1),
        seat_deck="forge",
        seat_opponent="sdk",
        model="claude-opus-5",
    )
    specs = {s.seat: s for s in captured["plan"].specs}
    assert specs["A"].options == {}
    assert specs["B"].options["model"] == "claude-opus-5"


def test_a_halted_sweep_skips_the_rest_without_playing_them(stub_run) -> None:
    import threading

    stub_run(_result([{"game": 1, "winner": "A", "draw": False, "turns": 9}]))
    halt = threading.Event()
    halt.set()

    p = sweepmod.run_pairing(sweepmod.Pairing(deck="mine", opponent="theirs", games=1), halt=halt)
    assert p.exhausted
    assert p.wins == 0
    assert "skipped" in p.error


# ------------------------------------------ what makes a pairing not a result

# Three ways a pairing produces numbers nobody played, all with the guard in
# place and none of them tested, so deleting any of the three survived.


def test_a_crashed_forge_does_not_contribute_its_partial_games(stub_run) -> None:
    """Forge exits four games into twenty.

    Without the guard those four wins go into the headline, no warning prints,
    `--json` says valid, and the command exits 0. A script polling the exit
    status reads a sixteen-game-short run as complete.
    """
    crashed = _result([{"game": 1, "winner": "A", "draw": False, "turns": 9}])
    crashed.crashed = True
    crashed.error = "forge exited 1"
    stub_run(crashed)

    p = sweepmod.run_pairing(sweepmod.Pairing(deck="mine", opponent="theirs", games=20))
    assert p.exhausted, "a crashed match reported its partial games as a result"
    assert p.error


def test_a_pairing_that_never_launched_is_not_a_nil_nil_draw(monkeypatch) -> None:
    """A typo'd slug or an unreadable collection produced a pairing with an
    error and exhausted=False, so the sweep read as valid and exited 0."""

    def explode(*a, **kw):
        raise RuntimeError("no deck named that")

    monkeypatch.setattr(sweepmod.matchmod, "plan", explode)
    p = sweepmod.run_pairing(sweepmod.Pairing(deck="mine", opponent="ghost", games=4))

    assert p.exhausted
    assert "no deck named that" in p.error
    assert p.played == 0


def test_a_halted_sweep_still_accounts_for_every_opponent(_isolated, monkeypatch) -> None:
    """Twenty-three decks halting after two used to come back as a two-deck
    sweep, with the headline computed over whichever finished first.

    The fake blocks after the second pairing so the halt really does land
    mid-field. A fake that returns instantly lets the pool finish the whole
    field before the first result is even read, which is not the case under
    test.
    """
    import threading

    played: list[str] = []
    lock = threading.Lock()
    proceed = threading.Event()

    def fake_pairing(p, halt=None, **kw):
        if halt is not None and halt.is_set():
            p.exhausted = True
            p.error = "skipped, the sweep stopped before reaching it"
            return p
        with lock:
            played.append(p.opponent)
            seen = len(played)
        if p.opponent == "second":
            p.exhausted = True
            p.error = "seat ran out of capacity"
            return p
        if seen >= 2:
            # Hold the remaining workers until the halt has been processed.
            proceed.wait(timeout=5)
        p.wins = p.games
        return p

    monkeypatch.setattr(sweepmod, "run_pairing", fake_pairing)
    opponents = ["first", "second", "third", "fourth", "fifth"]

    def release() -> None:
        import time

        time.sleep(0.5)
        proceed.set()

    threading.Thread(target=release, daemon=True).start()
    results = sweepmod.run_sweep("d", opponents, games=1, workers=1)

    # The property that matters, whatever the timing did: the field is
    # accounted for, and nothing it never reached is reported as a result.
    assert {p.opponent for p in results} == set(opponents), (
        f"the sweep dropped the field it never reached: {[p.opponent for p in results]}"
    )
    assert any(p.exhausted for p in results)
    for p in results:
        if p.opponent not in played:
            assert p.exhausted, f"{p.opponent} was never played and is not marked"
            assert p.wins == 0


def test_every_pairing_is_given_the_sweeps_seed(_isolated, monkeypatch) -> None:
    """A sweep advertised as reproducible could be silently unseeded."""
    seen: list[int | None] = []
    monkeypatch.setattr(sweepmod, "run_pairing", lambda p, **kw: (seen.append(p.seed), p)[1])
    sweepmod.run_sweep("d", ["a", "b", "c"], games=1, seed=4242, workers=1)
    assert seen == [4242, 4242, 4242]


def test_the_table_ranks_the_best_matchups_first(_isolated) -> None:
    """The docstring says a reader looks for the worst at the bottom, and
    flipping the sort survived."""
    good = sweepmod.Pairing(deck="d", opponent="easy", games=4)
    good.wins = 4
    bad = sweepmod.Pairing(deck="d", opponent="hard", games=4)
    bad.losses = 4
    middling = sweepmod.Pairing(deck="d", opponent="even", games=4)
    middling.wins, middling.losses = 2, 2

    ordered = sweepmod.run_sweep.__wrapped__ if hasattr(sweepmod.run_sweep, "__wrapped__") else None
    table = sweepmod.format_table(
        "d", sorted([bad, good, middling], key=lambda p: (-p.win_rate, p.opponent)), 1.0
    )
    assert table.index("easy") < table.index("even") < table.index("hard")
    assert ordered is None  # nothing to unwrap, kept explicit


def test_a_sub_threshold_fallback_rate_is_still_reported(_isolated) -> None:
    """The only place a fallback rate under the void threshold surfaces.

    Twenty percent of a deck's decisions played by Forge is worth knowing even
    though it does not void the run.
    """
    noisy = sweepmod.Pairing(deck="d", opponent="noisy", games=10)
    noisy.wins, noisy.losses = 6, 4
    noisy.decisions, noisy.fallback_rate = 200, 0.2

    table = sweepmod.format_table("d", [noisy], 1.0)
    assert "fell back to Forge" in table
    assert "20%" in table
