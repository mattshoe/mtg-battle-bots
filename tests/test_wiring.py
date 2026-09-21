"""The assignments nobody executes.

A mutation battery found that this project's units are well tested and its
wiring is not. Every Tier 1 survivor was a single line in `match.run` or
`cli.run_match` — one assignment, one `if` — that no test ever ran with a real
consequence attached.

So these tests drive the real entry points against a fake engine, and assert on
what a caller would actually see. Deleting any one of those lines has to turn
one of these red.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gauntlet import match as matchmod
from gauntlet.cli import app
from gauntlet.protocol import Request, Response
from gauntlet.seats import Seat, SeatExhausted, SeatTimeout

runner = CliRunner()

DECK = "Commander: Jetmir, Nexus of Revels\n" + "\n".join(f"1 Card {i}" for i in range(99))


@pytest.fixture
def deck_file(tmp_path) -> Path:
    p = tmp_path / "d.txt"
    p.write_text(DECK)
    return p


@pytest.fixture
def fake_forge(monkeypatch, tmp_path):
    """A Forge that plays games without a JVM, and records being stopped."""
    from gauntlet import paths

    for name in ("bridge_jar", "forge_jar", "gson_jar"):
        jar = tmp_path / f"{name}.jar"
        jar.write_text("")
        monkeypatch.setattr(paths, name, lambda j=jar: j)
    monkeypatch.setattr(paths, "forge_version", lambda: "test")

    state: dict = {"stopped": False, "drained": True}

    class _Forge:
        def __init__(self) -> None:
            self.on_line = None
            self.feed = 0

        def wait(self, timeout=None):
            if self.feed and state.get("endpoint"):
                _feed(state["endpoint"], self.feed)
            for i in (1, 2):
                self.on_line(
                    json.dumps(
                        {
                            "kind": "game_result",
                            "game": i,
                            "winner": "A",
                            "draw": False,
                            "turns": 10 + i,
                            "ms": 100,
                        }
                    )
                )
            return 0

        def drain(self, timeout=None):
            return state["drained"]

        def stop(self, grace: float = 5.0) -> None:
            state["stopped"] = True

    def launch(cmd, log_path, *, on_line=None, trace=False):
        forge = _Forge()
        forge.on_line = on_line or (lambda line: None)
        state["cmd"] = cmd
        # The endpoint Forge was told to connect to, so a test can have the
        # fake ask questions on it the way the real bridge does.
        for i, part in enumerate(cmd):
            if part == "--bridge":
                state["endpoint"] = cmd[i + 1].split("=", 1)[1]
        forge.feed = state.get("feed", 0)
        return forge

    monkeypatch.setattr(matchmod.engine, "launch", launch)
    return state


class _NeverAnswers(Seat):
    controller = "sdk"
    costs_money = True

    def decide(self, request: Request, timeout: float) -> Response:
        raise SeatTimeout("the agent never answered")


class _DiesImmediately(Seat):
    controller = "sdk"
    costs_money = True

    def decide(self, request: Request, timeout: float) -> Response:
        raise SeatExhausted("session limit")


def _plan(deck_file: Path, controller: str = "sdk"):
    return matchmod.plan(
        [
            matchmod.SeatSpec(seat="A", deck=str(deck_file), controller=controller),
            matchmod.SeatSpec(seat="B", deck=str(deck_file), controller="forge"),
        ],
        games=2,
    )


def _feed(endpoint: str, count: int) -> None:
    """Act as the bridge: ask a bridged seat some questions."""
    host, port = endpoint.split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        stream = sock.makefile("rwb")
        for i in range(1, count + 1):
            stream.write(
                (
                    json.dumps(
                        {
                            "v": 1,
                            "id": i,
                            "seat": "A",
                            "kind": "cast_or_pass",
                            "prompt": "priority",
                            "options": [{"i": 0, "label": "Pass priority"}],
                            "state": {"turn": 1},
                        }
                    )
                    + "\n"
                ).encode()
            )
            stream.flush()
            if not stream.readline():
                return


# --------------------------------------------------------- expected seats


def test_a_run_whose_bridge_never_connected_is_not_trustworthy(
    _isolated, deck_file, fake_forge, monkeypatch
) -> None:
    """The wiring for the whole trust mechanism is one assignment in run().

    Deleting `server.result.expected_seats = ...` made a run where Forge played
    every decision come back valid with a clean win record, and the suite stayed
    green because every trust test built its MatchResult by hand.
    """
    monkeypatch.setattr(matchmod, "build_seats", lambda p: {"A": _NeverAnswers()})
    result = matchmod.run(_plan(deck_file))

    assert result.games, "the fake engine should still have reported games"
    assert result.decisions == 0
    assert not result.trustworthy
    assert "never asked" in result.untrustworthy_because


def test_a_run_is_trustworthy_when_the_seat_actually_played(
    _isolated, deck_file, fake_forge, monkeypatch
) -> None:
    """The other direction, so the guard cannot be satisfied by always failing."""

    class _Answers(Seat):
        controller = "sdk"
        costs_money = True
        last_usage = (100, 10)

        def decide(self, request: Request, timeout: float) -> Response:
            return Response(id=request.id, choice=0, why="fine")

    monkeypatch.setattr(matchmod, "build_seats", lambda p: {"A": _Answers()})
    fake_forge["feed"] = 10

    result = matchmod.run(_plan(deck_file))

    assert result.decisions == 10
    assert result.fallbacks == 0
    assert result.trustworthy, result.untrustworthy_because


# -------------------------------------------------------------- stop wiring


def test_an_exhausted_seat_stops_the_real_engine(
    _isolated, deck_file, fake_forge, monkeypatch
) -> None:
    """`server.stop_engine = forge.stop` is one line in run().

    Deleting it survived every existing test, because the exhaustion test
    supplies its own callback and so only proves that _halt calls something.
    """
    monkeypatch.setattr(matchmod, "build_seats", lambda p: {"A": _DiesImmediately()})
    fake_forge["feed"] = 1

    matchmod.run(_plan(deck_file))

    for _ in range(50):
        if fake_forge["stopped"]:
            break
        time.sleep(0.02)
    assert fake_forge["stopped"], "an exhausted seat left the engine running"


def test_output_forge_never_finished_writing_is_reported(
    _isolated, deck_file, fake_forge, monkeypatch
) -> None:
    """A run whose log pump did not finish has an incomplete record."""
    monkeypatch.setattr(matchmod, "build_seats", lambda p: {})
    fake_forge["drained"] = False

    result = matchmod.run(
        matchmod.plan(
            [matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="forge")],
            games=2,
        )
    )
    assert "not fully read" in result.error


# --------------------------------------------------------- the json contract


def test_run_json_reports_an_untrustworthy_result_and_exits_nonzero(
    _isolated, deck_file, monkeypatch
) -> None:
    """The machine-readable contract, which no test invoked.

    A scripted caller reads `valid` and the exit code. Both were hardcodable to
    success with the suite green.
    """
    from gauntlet.server import MatchResult

    bad = MatchResult(match_id="m")
    bad.expected_seats = {"A"}
    bad.per_seat = {"A": (20, 20)}
    bad.games.append({"game": 1, "winner": "A", "draw": False})
    monkeypatch.setattr(matchmod, "run", lambda p, **kw: bad)

    result = runner.invoke(
        app,
        [
            "run",
            "--a",
            str(deck_file),
            "--b",
            str(deck_file),
            "--seat-a",
            "forge",
            "--seat-b",
            "forge",
            "--json",
        ],
    )
    payload = json.loads(result.output)

    assert payload["valid"] is False
    assert payload["fallbacks"] == 20
    assert payload["untrustworthy_because"]
    assert result.exit_code == 1, "a void run exited 0, so a script would take it"


def test_run_json_reports_a_good_result_and_exits_zero(_isolated, deck_file, monkeypatch) -> None:
    from gauntlet.server import MatchResult

    good = MatchResult(match_id="m")
    good.expected_seats = {"A"}
    good.per_seat = {"A": (20, 0)}
    good.games.append({"game": 1, "winner": "A", "draw": False})
    monkeypatch.setattr(matchmod, "run", lambda p, **kw: good)

    result = runner.invoke(
        app,
        [
            "run",
            "--a",
            str(deck_file),
            "--b",
            str(deck_file),
            "--seat-a",
            "forge",
            "--seat-b",
            "forge",
            "--json",
        ],
    )
    payload = json.loads(result.output)
    assert payload["valid"] is True
    assert result.exit_code == 0


def test_the_fallback_threshold_is_where_it_says_it_is() -> None:
    """The constant can drift to 0.99 and every relative assertion still holds."""
    from gauntlet.server import MAX_TOLERABLE_FALLBACK_RATE, MatchResult

    assert MAX_TOLERABLE_FALLBACK_RATE == 0.25

    middling = MatchResult(match_id="m")
    middling.expected_seats = {"A"}
    middling.per_seat = {"A": (100, 30)}
    assert not middling.trustworthy, "30% unanswered read as a real result"


# ----------------------------------------------------------- budget wiring


def test_a_detached_run_carries_its_budget_through_the_plan_file(_isolated, deck_file) -> None:
    """A detached run is a different process, so the cap travels as JSON.

    `budget_for` returning None survived every test, and detaching is what the
    documented two-agent mode does, so this was the configuration where an
    uncapped run costs real money.
    """
    from gauntlet.budget import Budget

    planned = matchmod.plan(
        [
            matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="interactive"),
            matchmod.SeatSpec(seat="B", deck=str(deck_file), controller="sdk"),
        ],
        budget=Budget(max_usd=3.5, max_decisions=777, model="claude-opus-5"),
    )
    reloaded = matchmod.load_plan(matchmod._dump_plan(planned))
    budget = matchmod.budget_for(reloaded)

    assert budget is not None, "a detached run would have been uncapped"
    assert budget.max_usd == 3.5
    assert budget.max_decisions == 777
    assert budget.model == "claude-opus-5"


def test_a_plan_with_no_budget_stays_uncapped(_isolated, deck_file) -> None:
    """A Forge-only run spends nothing and must not carry a dollar cap."""
    planned = matchmod.plan([matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="forge")])
    assert matchmod.budget_for(matchmod.load_plan(matchmod._dump_plan(planned))) is None


# ---------------------------------------------------------- routing wiring


def test_a_bare_routed_list_applies_to_every_seat() -> None:
    """--routed is the documented cost dial, and a bare list was ignored.

    cast_or_pass is most of a game's decisions, so a user trimming it paid full
    price with no signal.
    """
    from gauntlet.cli import _parse_routed

    parsed = _parse_routed("attack,block")
    assert parsed["*"] == ("attack", "block")


def test_a_lowercase_seat_letter_still_names_that_seat() -> None:
    from gauntlet.cli import _parse_routed

    assert _parse_routed("a=attack")["A"] == ("attack",)


def test_a_forge_seat_is_never_given_a_bridge(_isolated, deck_file, fake_forge) -> None:
    """A bridge for a seat Forge plays opens a connection nothing answers, and
    every decision on it times out."""
    matchmod.run(
        matchmod.plan(
            [
                matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="forge"),
                matchmod.SeatSpec(seat="B", deck=str(deck_file), controller="forge"),
            ]
        )
    )
    cmd = fake_forge["cmd"]
    assert "--bridge" not in cmd
