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
