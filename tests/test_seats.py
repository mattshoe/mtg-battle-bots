"""Tests for the seat rendezvous.

``InteractiveSeat`` is the only place in the harness where two threads hand work
to each other, so it is the only place a race can hide. Everything here drives
it with real threads and real events, and the timeouts are short enough that a
deadlock shows up as a failure rather than as a hung suite.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from typing import Any

import pytest

from gauntlet.protocol import VERSION, ProtocolError, Request, Response
from gauntlet.seats import (
    ApiSeat,
    ForgeSeat,
    InteractiveSeat,
    Seat,
    SeatExhausted,
    SeatTimeout,
    build_seat,
)

# Short enough that a hang fails fast, long enough that a loaded machine still
# wins the handoff.
QUICK = 0.05
PATIENT = 5.0


def request(req_id: int = 47, kind: str = "cast_or_pass") -> Request:
    """A request built off a real wire line, so the protocol is in the loop."""
    return Request.parse(
        json.dumps(
            {
                "v": VERSION,
                "id": req_id,
                "seat": "A",
                "kind": kind,
                "prompt": "Main phase 1. Choose a spell or ability to play.",
                "options": [
                    {"i": 0, "label": "Pass priority"},
                    {"i": 1, "label": "Cast Cultivate", "cost": "{2}{G}", "card": "cultivate"},
                ],
                "state": {"turn": 7, "phase": "main1", "active": "A"},
            }
        )
    )


class Decider(threading.Thread):
    """Runs ``decide`` off the main thread and keeps whatever came back."""

    def __init__(self, seat: Seat, req: Request, timeout: float) -> None:
        super().__init__(daemon=True)
        self.seat = seat
        self.request = req
        self.timeout = timeout
        self.response: Response | None = None
        self.error: BaseException | None = None
        self.entered = threading.Event()

    def run(self) -> None:
        self.entered.set()
        try:
            self.response = self.seat.decide(self.request, self.timeout)
        # Catch everything. The test decides whether what came out was right.
        except BaseException as exc:
            self.error = exc

    def finish(self, timeout: float = PATIENT) -> None:
        self.join(timeout)
        assert not self.is_alive(), "decide never returned"


@pytest.fixture
def seat() -> Any:
    s = InteractiveSeat()
    yield s
    s.close()


def test_decide_blocks_until_an_answer_arrives(seat: InteractiveSeat) -> None:
    decider = Decider(seat, request(), PATIENT)
    decider.start()

    taken = seat.take(PATIENT)
    assert taken is not None
    assert taken.id == 47
    assert taken.kind == "cast_or_pass"

    # Still parked. The engine thread must not proceed on its own.
    decider.join(QUICK)
    assert decider.is_alive()

    seat.answer(Response(id=47, choice=1, why="ramp"))
    decider.finish()

    assert decider.error is None
    assert decider.response == Response(id=47, choice=1, why="ramp")


def test_take_twice_without_answering_returns_the_same_request(seat: InteractiveSeat) -> None:
    # Regression test. An earlier version consumed the question with a
    # semaphore, so a second look came back empty and an agent that retried read
    # it as "nothing to do" at exactly the moment there was something to do.
    decider = Decider(seat, request(), PATIENT)
    decider.start()

    first = seat.take(PATIENT)
    second = seat.take(PATIENT)
    third = seat.current()

    assert first is not None
    assert first is second
    assert first is third

    seat.answer(Response(id=47, choice=0))
    decider.finish()


def test_take_returns_none_when_nothing_is_pending(seat: InteractiveSeat) -> None:
    assert seat.take(QUICK) is None
    assert seat.current() is None


def test_answer_with_the_wrong_id_is_rejected(seat: InteractiveSeat) -> None:
    decider = Decider(seat, request(req_id=47), PATIENT)
    decider.start()
    seat.take(PATIENT)

    with pytest.raises(ProtocolError, match="answered 46, the open question is 47"):
        seat.answer(Response(id=46, choice=1))

    # The rejection must leave the question on the table, not eat it.
    assert seat.current() is not None
    seat.answer(Response(id=47, choice=1))
    decider.finish()
    assert decider.response is not None


def test_answer_with_an_out_of_range_choice_is_rejected(seat: InteractiveSeat) -> None:
    decider = Decider(seat, request(), PATIENT)
    decider.start()
    seat.take(PATIENT)

    with pytest.raises(ProtocolError, match=r"choice 7 is not one of \[0, 1\]"):
        seat.answer(Response(id=47, choice=7))

    assert seat.current() is not None
    seat.answer(Response(id=47, choice=0))
    decider.finish()
    assert decider.response == Response(id=47, choice=0, why="")


def test_decide_raises_seat_timeout_when_nobody_answers(seat: InteractiveSeat) -> None:
    decider = Decider(seat, request(), QUICK)
    decider.start()
    decider.finish()

    assert isinstance(decider.error, SeatTimeout)
    assert "cast_or_pass" in str(decider.error)
    assert decider.response is None


def test_a_late_answer_is_rejected_rather_than_applied(seat: InteractiveSeat) -> None:
    # An agent that thinks past the engine's patience submits for a decision
    # Forge has already played itself. Applying it would attach this seat's
    # reasoning to a move it did not make.
    decider = Decider(seat, request(), QUICK)
    decider.start()
    decider.finish()
    assert isinstance(decider.error, SeatTimeout)

    with pytest.raises(ProtocolError, match="nothing to answer"):
        seat.answer(Response(id=47, choice=1, why="too slow"))


def test_close_unblocks_a_waiting_decide(seat: InteractiveSeat) -> None:
    decider = Decider(seat, request(), PATIENT)
    decider.start()
    assert seat.take(PATIENT) is not None

    seat.close()
    decider.finish()

    assert isinstance(decider.error, SeatTimeout)
    assert "match ended" in str(decider.error)


def test_close_unblocks_a_waiting_take(seat: InteractiveSeat) -> None:
    taken: list[Request | None] = []
    done = threading.Event()

    def collect() -> None:
        taken.append(seat.take(PATIENT))
        done.set()

    threading.Thread(target=collect, daemon=True).start()
    seat.close()

    assert done.wait(PATIENT), "take never returned after close"
    assert taken == [None]


def test_decide_on_a_closed_seat_gives_up_immediately(seat: InteractiveSeat) -> None:
    seat.close()

    with pytest.raises(SeatTimeout, match="seat closed"):
        seat.decide(request(), PATIENT)


def test_forge_seat_refuses_to_decide() -> None:
    # Reaching this seat means a player with no bridge was given one, which is
    # a wiring bug. Inventing an answer would hide it.
    with pytest.raises(RuntimeError, match="a forge seat was asked to decide"):
        ForgeSeat().decide(request(), PATIENT)


def test_build_seat_makes_each_kind() -> None:
    assert isinstance(build_seat("forge"), ForgeSeat)
    assert isinstance(build_seat("interactive"), InteractiveSeat)
    assert isinstance(build_seat("api"), ApiSeat)


def test_build_seat_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown seat kind 'robot'"):
        build_seat("robot")


def test_controllers_are_what_the_transcript_records() -> None:
    assert ForgeSeat.controller == "forge"
    assert InteractiveSeat.controller == "interactive"
    assert ApiSeat.controller == "api"


# ------------------------------------------------- the late-answer guard


def test_open_id_names_the_question_the_agent_was_shown() -> None:
    """`open_id` is the whole late-answer guard and had no tests.

    It is what stops an agent's stale choice landing on a different question
    with a different option list.
    """
    seat = InteractiveSeat()
    engine = threading.Thread(target=lambda: _swallow(seat, _req(1)), daemon=True)
    engine.start()

    assert seat.take(1.0) is not None
    assert seat.open_id() == 1
    seat.close()
    engine.join(timeout=2)


def test_open_id_is_none_before_anything_was_shown() -> None:
    """Taken but not shown is not the same as shown. An answer arriving now
    belongs to a question this agent has never seen."""
    seat = InteractiveSeat()
    engine = threading.Thread(target=lambda: _swallow(seat, _req(1)), daemon=True)
    engine.start()
    try:
        # A question is open, but nobody has called take(), so nobody has been
        # shown it.
        assert seat.current() is not None
        assert seat.open_id() is None
    finally:
        seat.close()
        engine.join(timeout=2)


def test_open_id_is_none_when_the_game_moved_on() -> None:
    """The case that matters. The agent was shown question 1, took too long,
    the engine fell back, and question 2 is now open. An unqualified answer
    must not be applied to it."""
    seat = InteractiveSeat()
    first = threading.Thread(target=lambda: _swallow(seat, _req(1)), daemon=True)
    first.start()
    assert seat.take(1.0).id == 1
    assert seat.open_id() == 1

    # The engine gives up on 1 and asks 2.
    second = threading.Thread(target=lambda: _swallow(seat, _req(2)), daemon=True)
    second.start()
    deadline = time.monotonic() + 2
    while seat.current() is not None and seat.current().id != 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert seat.current().id == 2
    assert seat.open_id() is None, "a stale answer could have landed on question 2"

    seat.close()
    for t in (first, second):
        t.join(timeout=2)


def test_a_displaced_question_wakes_its_waiter_promptly() -> None:
    """The engine moving on must not leave the previous caller parked for a
    full timeout on a question nobody will ever answer."""
    seat = InteractiveSeat()
    outcome: list[str] = []

    def first():
        try:
            seat.decide(_req(1), timeout=30)
            outcome.append("answered")
        except SeatTimeout as exc:
            outcome.append(str(exc))

    t = threading.Thread(target=first, daemon=True)
    t.start()
    deadline = time.monotonic() + 2
    while seat.current() is None and time.monotonic() < deadline:
        time.sleep(0.01)

    second = threading.Thread(target=lambda: _swallow(seat, _req(2)), daemon=True)
    second.start()

    t.join(timeout=5)
    assert not t.is_alive(), "the displaced waiter was left parked for its full timeout"
    assert outcome and "moved on" in outcome[0], outcome

    seat.close()
    second.join(timeout=2)


def _req(rid: int) -> Request:
    return Request.parse(
        json.dumps(
            {
                "v": 1,
                "id": rid,
                "seat": "A",
                "kind": "cast_or_pass",
                "prompt": "priority",
                "options": [{"i": 0, "label": "Pass priority"}],
                "state": {},
            }
        )
    )


def _swallow(seat: InteractiveSeat, request: Request) -> None:
    """Drive a decision and absorb whatever it ends as."""
    with contextlib.suppress(SeatTimeout, SeatExhausted):
        seat.decide(request, timeout=10)
