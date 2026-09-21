"""Whether a result can be believed.

Every incident this project has had took the same shape: a run that produced a
plausible number instead of an error. The narrow guards written after each one
caught that one. These tests are about the general property, because the next
failure will arrive by a route nobody has seen.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from gauntlet.protocol import Request, Response
from gauntlet.seats import Seat, SeatTimeout
from gauntlet.server import MAX_TOLERABLE_FALLBACK_RATE, MatchServer
from gauntlet.transcript import Transcript


def _wire(rid: int) -> bytes:
    return (
        json.dumps(
            {
                "v": 1,
                "id": rid,
                "seat": "A",
                "kind": "cast_or_pass",
                "prompt": "priority",
                "options": [{"i": 0, "label": "Pass priority"}],
                "state": {"turn": 1, "phase": "main1", "active": "A", "you": "A", "me": {}},
            }
        )
        + "\n"
    ).encode()


class _AlwaysTimesOut(Seat):
    controller = "api"
    costs_money = True

    def decide(self, request: Request, timeout: float) -> Response:
        raise SeatTimeout("the agent never answered")


class _AlwaysAnswers(Seat):
    controller = "api"
    costs_money = True
    last_usage = (100, 10)

    def decide(self, request: Request, timeout: float) -> Response:
        return Response(id=request.id, choice=0, why="playing for tempo")


def _drive(seat: Seat, tmp_path, count: int) -> MatchServer:
    server = MatchServer(
        match_id="trust",
        seats={"A": seat},
        transcript=Transcript(tmp_path / "t.db"),
        decision_timeout=1.0,
    )
    server.result.expected_seats = {"A"}
    endpoint, _ = server.bind()
    server.start()
    host, port = endpoint.split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        stream = sock.makefile("rwb")
        for i in range(1, count + 1):
            stream.write(_wire(i))
            stream.flush()
            stream.readline()
    return server


def test_a_run_nobody_answered_is_not_trustworthy(tmp_path) -> None:
    """The headline incident, in its general form.

    A seat that answered nothing produces a complete, numerically perfect,
    entirely fictional result. The previous guard only caught the case where a
    seat announced its own death, so a short timeout, a missing key, or an agent
    that never started all still produced a clean win rate.
    """
    server = _drive(_AlwaysTimesOut(), tmp_path, 20)
    try:
        server.record_game_result({"game": 1, "winner": "A", "draw": False, "turns": 12})

        assert server.result.decisions == 20
        assert server.result.fallbacks == 20
        assert server.result.fallback_rate == 1.0
        assert not server.result.trustworthy, "a run of pure fallbacks read as a real result"
        # And the score is still there, which is exactly why the flag matters.
        assert server.result.wins_by_seat() == {"A": 1}
    finally:
        server.shutdown()


def test_a_run_the_seat_actually_played_is_trustworthy(tmp_path) -> None:
    server = _drive(_AlwaysAnswers(), tmp_path, 20)
    try:
        assert server.result.fallbacks == 0
        assert server.result.trustworthy
    finally:
        server.shutdown()


def test_the_threshold_is_where_it_claims_to_be(tmp_path) -> None:
    """A few fallbacks are normal. A third of them is not a game anyone played."""

    class _Flaky(Seat):
        controller = "api"
        costs_money = True
        last_usage = (100, 10)

        def __init__(self) -> None:
            self.n = 0

        def decide(self, request: Request, timeout: float) -> Response:
            self.n += 1
            if self.n % 10 == 0:
                raise SeatTimeout("one bad call")
            return Response(id=request.id, choice=0, why="fine")

    server = _drive(_Flaky(), tmp_path, 40)
    try:
        assert 0 < server.result.fallback_rate < MAX_TOLERABLE_FALLBACK_RATE
        assert server.result.trustworthy, "an occasional fallback must not void a run"
    finally:
        server.shutdown()


def test_a_free_seat_does_not_consume_the_paid_budget(tmp_path) -> None:
    """The documented two-agent mode puts an interactive seat beside a paid one.

    Billing the free seat halted the run at roughly half its real budget and
    marked a perfectly good result void.
    """
    from gauntlet.budget import Budget
    from gauntlet.seats import InteractiveSeat

    budget = Budget(max_usd=5.0)
    server = MatchServer(
        match_id="free",
        seats={"A": InteractiveSeat()},
        transcript=Transcript(tmp_path / "t.db"),
        decision_timeout=0.2,
        budget=budget,
    )
    endpoint, _ = server.bind()
    server.start()
    host, port = endpoint.split(":")
    try:
        with socket.create_connection((host, int(port)), timeout=5) as sock:
            stream = sock.makefile("rwb")
            for i in range(1, 6):
                stream.write(_wire(i))
                stream.flush()
                stream.readline()
        assert budget.decisions == 0, "a free seat was billed against the cap"
        assert budget.spent_usd == 0.0
    finally:
        server.shutdown()


def test_a_transcript_failure_does_not_hand_the_match_to_forge(tmp_path) -> None:
    """A write failure used to kill the bridge thread.

    Forge then read EOF, latched its connection broken, and played every
    remaining decision itself while Python recorded nothing. `database is
    locked` is ordinary during a parallel sweep sharing one WAL file.
    """

    class _Breaking(Transcript):
        def __init__(self, path) -> None:
            super().__init__(path)
            self.calls = 0

        def record_decision(self, **kw):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("database is locked")
            return super().record_decision(**kw)

    server = MatchServer(
        match_id="dbfail",
        seats={"A": _AlwaysAnswers()},
        transcript=_Breaking(tmp_path / "t.db"),
        decision_timeout=1.0,
    )
    endpoint, _ = server.bind()
    server.start()
    host, port = endpoint.split(":")
    try:
        answered = 0
        with socket.create_connection((host, int(port)), timeout=5) as sock:
            stream = sock.makefile("rwb")
            for i in range(1, 6):
                stream.write(_wire(i))
                stream.flush()
                line = stream.readline()
                if not line:
                    break
                answered += 1
        assert answered == 5, (
            f"the bridge died after a transcript failure, answering only {answered} of 5"
        )
        # And the decision whose record was lost is a fallback, because Forge
        # played it. Counting it as answered reported a Forge-played run as
        # clean, which is the whole failure this guards.
        asked, missed = server.result.per_seat["A"]
        assert asked == 5
        assert missed == 1, f"a lost record was counted as an answered decision: {missed}"
        assert server.serving_errors
    finally:
        server.shutdown()


def test_stopping_a_match_stops_the_engine(tmp_path) -> None:
    """`gauntlet stop` said "stopped" and left the JVM playing.

    Forge had been told to play N games and kept playing them at full fallback
    with the transcript closed underneath it.
    """
    stopped = threading.Event()
    server = MatchServer(
        match_id="stopme",
        seats={"A": _AlwaysAnswers()},
        transcript=Transcript(tmp_path / "t.db"),
        decision_timeout=1.0,
    )
    server.stop_engine = stopped.set
    server.bind()
    server.start()
    try:
        from gauntlet.server import call_match

        reply = call_match("stopme", {"op": "stop"}, timeout=5)
        assert reply["status"] == "stopped"

        deadline = time.monotonic() + 5
        while not stopped.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stopped.is_set(), "stop closed the sockets and left Forge running"
    finally:
        server.shutdown()


@pytest.mark.parametrize(
    ("message", "fatal"),
    [
        ("Error code: 401 authentication_error", True),
        ("Error code: 403 permission_error", True),
        ("Error code: 429 rate_limit_error", True),
        ("Error code: 529 overloaded_error", True),
        ("Your credit balance is too low", True),
        ("Connection error.", True),
        ("Error code: 400 invalid_request_error: bad tool", False),
        ("Read timed out", False),
    ],
)
def test_api_failures_are_classified_the_same_way_the_sdk_seat_classifies_them(
    message: str, fatal: bool
) -> None:
    """Two seats, two lists, no test relating them.

    A wrong key produced SeatTimeout on the api seat and SeatExhausted on the
    sdk seat, so the same failure void-flagged one run and silently faked
    another.
    """
    from gauntlet.seats import is_fatal_api_error

    assert is_fatal_api_error(message) is fatal


def test_a_bridged_seat_that_was_never_asked_anything_is_not_a_result(tmp_path) -> None:
    """Zero decisions used to read as a perfect run.

    fallback_rate is 0/0, which is 0.0, which passed the threshold. So a bridge
    that never connected — an unbuilt jar, a seat-name mismatch, a routing list
    that routed nothing — produced a complete win record with `valid: true` and
    exit 0. That is the original fictional-result failure with a new cause.
    """
    from gauntlet.server import MatchResult

    result = MatchResult(match_id="never-asked")
    result.expected_seats = {"A"}
    result.games.append({"game": 1, "winner": "A", "draw": False})

    assert result.decisions == 0
    assert not result.trustworthy
    assert "never asked" in result.untrustworthy_because


def test_one_dead_seat_is_not_diluted_by_a_healthy_one(tmp_path) -> None:
    """Counting match-wide let a busy seat hide a silent one.

    Asymmetric --routed is the documented way to make agent runs affordable, so
    one seat answering ten times as often as the other is the normal shape, and
    it pushed the dead seat's failures below the match-wide threshold.
    """
    from gauntlet.server import MatchResult

    result = MatchResult(match_id="lopsided")
    result.expected_seats = {"A", "B"}
    result.per_seat = {"A": (9, 9), "B": (81, 0)}

    assert result.fallback_rate == pytest.approx(0.1)
    assert not result.trustworthy, "a seat that answered nothing was averaged away"
    assert "seat A" in result.untrustworthy_because


def test_an_illegal_choice_never_becomes_a_recorded_play(tmp_path) -> None:
    """The last live check that a seat cannot record a play it never made.

    Both shipped seats validate upstream in parse_reply, so deleting the
    server's own check looks harmless until a third seat kind arrives. Then an
    out-of-range choice reaches Forge and is written down as the agent's play,
    with the agent's reasoning attached.
    """
    import sqlite3

    class _Illegal(Seat):
        controller = "api"
        costs_money = True
        last_usage = (10, 2)

        def decide(self, request: Request, timeout: float) -> Response:
            # Past the end of a two-option list.
            return Response(id=request.id, choice=99, why="a play I cannot make")

    server = _drive(_Illegal(), tmp_path, 3)
    try:
        conn = sqlite3.connect(tmp_path / "t.db")
        rows = conn.execute(
            "SELECT chosen, fallback, why FROM decisions WHERE match_id = 'trust'"
        ).fetchall()
        conn.close()

        assert rows, "nothing was recorded at all"
        for chosen, fallback, why in rows:
            assert fallback == 1, "an illegal choice was recorded as a real play"
            assert chosen is None
            assert why == "", "Forge's play carries the agent's reasoning"
        assert not server.result.trustworthy
    finally:
        server.shutdown()


def test_a_seat_that_fails_every_decision_still_burns_its_budget(tmp_path) -> None:
    """Charging only on success meant a seat failing every call spent on every
    call while the cap never moved."""
    from gauntlet.budget import Budget

    budget = Budget(max_usd=5.0, model="claude-haiku-4-5")
    server = MatchServer(
        match_id="trust",
        seats={"A": _AlwaysTimesOut()},
        transcript=Transcript(tmp_path / "t.db"),
        decision_timeout=0.2,
        budget=budget,
    )
    server.result.expected_seats = {"A"}
    endpoint, _ = server.bind()
    server.start()
    host, port = endpoint.split(":")
    try:
        with socket.create_connection((host, int(port)), timeout=5) as sock:
            stream = sock.makefile("rwb")
            for i in range(1, 11):
                stream.write(_wire(i))
                stream.flush()
                stream.readline()

        assert budget.decisions == 10, f"ten failed calls were billed as {budget.decisions}"
        assert budget.spent_usd > 0
    finally:
        server.shutdown()


def test_one_decisions_token_count_is_not_billed_to_the_next(tmp_path) -> None:
    """`last_usage` is cleared after billing.

    Leaving it meant every decision after a failure re-billed the previous
    decision's tokens, so one expensive turn kept charging forever.
    """
    from gauntlet.budget import Budget

    class _ExpensiveThenSilent(Seat):
        controller = "api"
        costs_money = True

        def __init__(self) -> None:
            self.n = 0
            self.last_usage = None

        def decide(self, request: Request, timeout: float) -> Response:
            self.n += 1
            if self.n == 1:
                self.last_usage = (100_000, 1_000)
                return Response(id=request.id, choice=0, why="expensive")
            # Says nothing about cost, so the budget must estimate rather than
            # reuse the first turn's figure.
            raise SeatTimeout("quiet failure")

    budget = Budget(max_usd=500.0, model="claude-haiku-4-5")
    server = MatchServer(
        match_id="trust",
        seats={"A": _ExpensiveThenSilent()},
        transcript=Transcript(tmp_path / "t.db"),
        decision_timeout=0.2,
        budget=budget,
    )
    server.result.expected_seats = {"A"}
    endpoint, _ = server.bind()
    server.start()
    host, port = endpoint.split(":")
    try:
        with socket.create_connection((host, int(port)), timeout=5) as sock:
            stream = sock.makefile("rwb")
            for i in range(1, 4):
                stream.write(_wire(i))
                stream.flush()
                stream.readline()

        # One expensive turn plus two estimated ones, not three expensive ones.
        assert budget.input_tokens < 110_000, (
            f"a stale token count was re-billed: {budget.input_tokens}"
        )
    finally:
        server.shutdown()


def test_the_server_reserves_rather_than_merely_checking(tmp_path) -> None:
    """The cap has to bind on decisions in flight, not just billed ones.

    `reserve()` is tested on the Budget and was tested nowhere on the server,
    so swapping it for `check()` survived. With `check()` every worker in a
    sweep passes a cap one of them had room for, which is the overrun the
    budget docstring says already happened.
    """
    import threading

    from gauntlet.budget import Budget, estimate_usd

    holding = threading.Event()
    release = threading.Event()

    class _Slow(Seat):
        controller = "api"
        costs_money = True
        last_usage = (1030, 60)

        def decide(self, request: Request, timeout: float) -> Response:
            holding.set()
            release.wait(timeout=5)
            return Response(id=request.id, choice=0, why="held")

    model = "claude-haiku-4-5"
    # Room for one decision only.
    budget = Budget(max_usd=estimate_usd(1, model) * 1.5, model=model)
    server = MatchServer(
        match_id="reserve",
        seats={"A": _Slow()},
        transcript=Transcript(tmp_path / "t.db"),
        decision_timeout=5.0,
        budget=budget,
    )
    server.result.expected_seats = {"A"}
    endpoint, _ = server.bind()
    server.start()
    host, port = endpoint.split(":")

    try:
        # One decision in flight and unbilled.
        first = socket.create_connection((host, int(port)), timeout=5)
        first.sendall(_wire(1))
        assert holding.wait(timeout=5)

        # A second seat asks while the first is still thinking. The cap has
        # room for one, so this must be refused rather than dispatched.
        assert budget.in_flight == 1, "the decision in flight was never reserved"
        assert budget.committed_usd >= budget.max_usd * 0.5

        release.set()
        first.makefile("rb").readline()
        first.close()
    finally:
        release.set()
        server.shutdown()
