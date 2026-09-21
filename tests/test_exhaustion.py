"""A seat that runs out of capacity must stop the run, not quietly finish it.

Both tests here are regressions for real incidents. The first is a run that
reported 95-65 when 79.5% of its decisions had fallen back to Forge after a
session limit. The second is the follow-up: the guard fired and marked the match
exhausted, but Forge had been told to play twenty games and played eight more of
them at 100% fallback, because setting a flag does not stop a JVM.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from gauntlet.protocol import Request
from gauntlet.sdk_seat import _terminal_reply
from gauntlet.seats import Seat, SeatExhausted
from gauntlet.server import MatchServer
from gauntlet.transcript import Transcript

WIRE = json.dumps(
    {
        "v": 1,
        "id": 1,
        "seat": "A",
        "kind": "cast_or_pass",
        "prompt": "You have priority.",
        "options": [{"i": 0, "label": "Pass priority"}],
        "state": {"turn": 3, "phase": "main1", "active": "A", "you": "A", "me": {}},
    }
)


class _ExhaustedSeat(Seat):
    controller = "sdk"

    def decide(self, request: Request, timeout: float):
        raise SeatExhausted("You've hit your session limit")


@pytest.mark.parametrize(
    "reply",
    [
        "You've hit your session limit · resets 3pm (America/New_York)",
        "I will not respond to Magic game prompts.",
        "Session conclusively ended. No further responses.",
        "you have exceeded your usage limit",
    ],
)
def test_terminal_replies_are_recognised(reply: str) -> None:
    assert _terminal_reply(reply)


@pytest.mark.parametrize(
    "reply",
    ["CHOICE: 1\nWHY: racing", "CHOICE: 0", "I think option 2 is best.\nCHOICE: 2"],
)
def test_ordinary_replies_are_not_mistaken_for_exhaustion(reply: str) -> None:
    assert not _terminal_reply(reply)


def _serve(tmp_path):
    server = MatchServer(
        match_id="exhaust-test",
        seats={"A": _ExhaustedSeat()},
        transcript=Transcript(tmp_path / "t.db"),
        decision_timeout=2.0,
    )
    endpoint, _ = server.bind()
    server.start()
    host, port = endpoint.split(":")
    return server, host, int(port)


def test_exhaustion_defers_records_and_ends_the_match(tmp_path) -> None:
    server, host, port = _serve(tmp_path)
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(WIRE.encode() + b"\n")
            reply = json.loads(sock.makefile("rb").readline())

        # The engine is told to decide for itself rather than left hanging.
        assert reply["choice"] is None
        # The reason is prefixed with its category, so match on the cause
        # rather than the exact wording.
        assert "session limit" in server.exhausted["A"]
        assert server.finished.is_set()
    finally:
        server.shutdown()


def test_exhaustion_stops_the_engine(tmp_path) -> None:
    """The regression. A flag is not enough, the process has to be stopped."""
    server, host, port = _serve(tmp_path)
    stopped = threading.Event()
    server.stop_engine = stopped.set
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(WIRE.encode() + b"\n")
            sock.makefile("rb").readline()

        deadline = time.monotonic() + 5
        while not stopped.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stopped.is_set(), "an exhausted seat left Forge running"
    finally:
        server.shutdown()


def test_budget_stops_the_run_before_it_overspends(tmp_path) -> None:
    """A run must stop at its limit, and stop the engine while doing it."""
    from gauntlet.budget import Budget

    class _FreeSeat(Seat):
        controller = "api"
        costs_money = True
        last_usage = (1000, 50)

        def decide(self, request: Request, timeout: float):
            from gauntlet.protocol import Response

            return Response(id=request.id, choice=0, why="fine")

    tiny = Budget(max_usd=0.005, model="claude-haiku-4-5")
    server = MatchServer(
        match_id="budget-test",
        seats={"A": _FreeSeat()},
        transcript=Transcript(tmp_path / "b.db"),
        decision_timeout=2.0,
        budget=tiny,
    )
    stopped = threading.Event()
    server.stop_engine = stopped.set
    endpoint, _ = server.bind()
    server.start()
    host, port = endpoint.split(":")

    try:
        with socket.create_connection((host, int(port)), timeout=5) as sock:
            stream = sock.makefile("rwb")
            answered = 0
            for i in range(1, 40):
                stream.write(WIRE.replace('"id": 1', f'"id": {i}').encode() + b"\n")
                stream.flush()
                reply = json.loads(stream.readline())
                if reply["choice"] is None:
                    break
                answered += 1

        # A cap that permits a 50% overrun is not a cap. One decision may
        # cross the line, because the check runs before the spend, but the run
        # must stop there rather than drift past it.
        assert answered > 0, "the budget stopped the run before it did anything"
        one_decision = 1000 / 1e6 * 1.0 + 50 / 1e6 * 5.0
        assert tiny.spent_usd <= tiny.max_usd + one_decision, (
            f"overran the cap: spent {tiny.spent_usd} against {tiny.max_usd}"
        )
        deadline = time.monotonic() + 5
        while not stopped.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stopped.is_set(), "an over-budget run left the engine going"
    finally:
        server.shutdown()


# --------------------------------------------------------------- the cost gate


class _Refused(Exception):
    pass


def _gate(monkeypatch, *, tty: bool, **kw):
    """Run the cost gate with a known stdin, returning the budget or raising."""
    import sys as _sys

    import typer

    from gauntlet import cli

    monkeypatch.setattr(_sys.stdin, "isatty", lambda: tty, raising=False)

    def boom(msg: str) -> None:
        raise _Refused(msg)

    monkeypatch.setattr(cli, "_fail", boom)
    monkeypatch.setattr(typer, "echo", lambda *a, **k: None)
    return cli._preflight(**kw)


BASE = {"games": 40, "model": "claude-haiku-4-5", "max_cost": 5.0}


def test_free_run_is_never_gated(monkeypatch) -> None:
    b = _gate(monkeypatch, tty=False, seats=["forge", "forge"], yes=False, **BASE)
    assert b.max_usd == float("inf")


def test_paid_run_without_a_terminal_refuses_rather_than_hanging(monkeypatch) -> None:
    """The regression. typer.confirm blocks forever on a pipe, so a scripted
    run hung for as long as anyone let it instead of refusing."""
    with pytest.raises(_Refused, match="no terminal"):
        _gate(monkeypatch, tty=False, seats=["sdk", "sdk"], yes=False, **BASE)


def test_explicit_yes_passes_without_a_terminal(monkeypatch) -> None:
    b = _gate(monkeypatch, tty=False, seats=["sdk", "sdk"], yes=True, **BASE)
    assert b.max_usd == 5.0


def test_projection_over_the_cap_refuses_even_with_yes(monkeypatch) -> None:
    with pytest.raises(_Refused, match="over the"):
        _gate(
            monkeypatch,
            tty=False,
            seats=["api", "api"],
            yes=True,
            games=160,
            model="claude-haiku-4-5",
            max_cost=5.0,
        )


def test_declining_the_prompt_refuses(monkeypatch) -> None:
    import typer

    monkeypatch.setattr(typer, "confirm", lambda *a, **k: False)
    with pytest.raises(_Refused, match="cancelled"):
        _gate(monkeypatch, tty=True, seats=["sdk", "sdk"], yes=False, **BASE)


def test_the_default_cap_is_five_dollars() -> None:
    from gauntlet.budget import DEFAULT_MAX_USD

    assert DEFAULT_MAX_USD == 5.00


# ------------------------------------------------ the budget's own machinery


def test_an_unknown_model_prices_at_the_most_expensive_known_rate() -> None:
    """Mutating max() to min() survived. Guessing low on an unfamiliar model is
    how a budget stops being a budget."""
    from gauntlet.budget import PRICING, price

    unknown = price("claude-something-unreleased")
    assert unknown == max(PRICING.values())
    assert unknown > PRICING["claude-haiku-4-5"]


def test_the_decision_ceiling_fires_independently_of_dollars() -> None:
    """It exists for a drifted pricing table or a seat with no pricing at all,
    and had never been observed to fire."""
    from gauntlet.budget import Budget, BudgetExceeded

    b = Budget(max_usd=float("inf"), max_decisions=3)
    for _ in range(3):
        b.check()
        b.charge(input_tokens=1, output_tokens=1)
    with pytest.raises(BudgetExceeded, match="decision limit"):
        b.check()


def test_an_unbilled_reservation_still_counts_against_the_cap() -> None:
    """Deterministic, so it cannot pass on timing.

    A decision in flight has not been billed yet, and if the cap only looks at
    billed spend then every worker in a sweep passes it at once. Three
    reservations fit, the fourth must not.
    """
    from gauntlet.budget import Budget, BudgetExceeded, estimate_usd

    model = "claude-haiku-4-5"
    b = Budget(max_usd=estimate_usd(1, model) * 3, model=model)

    for _ in range(3):
        b.reserve()
    assert b.in_flight == 3
    assert b.spent_usd == 0.0, "nothing has been billed yet, which is the point"

    with pytest.raises(BudgetExceeded):
        b.reserve()


def test_concurrent_reservations_cannot_all_pass_one_slot() -> None:
    """The same property under real threads.

    An earlier version ran sixteen threads against a cap that only binds above
    thirty-one, so both its assertions held whether reservations existed or not.
    """
    import threading

    from gauntlet.budget import Budget, BudgetExceeded, estimate_usd

    model = "claude-haiku-4-5"
    b = Budget(max_usd=estimate_usd(1, model) * 3, model=model)

    passed: list[int] = []
    lock = threading.Lock()
    ready = threading.Barrier(16)
    release = threading.Event()

    def worker() -> None:
        ready.wait(timeout=5)
        try:
            b.reserve()
        except BudgetExceeded:
            return
        with lock:
            passed.append(1)
        # Hold the reservation until every thread has tried, so the test is
        # about the cap rather than about who finished first.
        release.wait(timeout=5)
        b.charge(input_tokens=1030, output_tokens=60)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 5
    while len(passed) + 0 < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    holding = len(passed)
    release.set()
    for t in threads:
        t.join(timeout=10)

    assert holding <= 4, f"{holding} of 16 threads passed a cap with room for three"
    assert passed, "nobody got through at all"


def test_a_reservation_is_released_when_the_decision_is_billed() -> None:
    """A reservation that is never released leaks the cap away."""
    from gauntlet.budget import Budget

    b = Budget(max_usd=5.0, model="claude-haiku-4-5")
    b.reserve()
    assert b.in_flight == 1
    b.charge(input_tokens=1000, output_tokens=50)
    assert b.in_flight == 0
    assert b.decisions == 1


def test_spend_without_real_counts_is_marked_estimated() -> None:
    """A budget that reports an estimate as measured invites someone to trust
    a figure nobody counted."""
    from gauntlet.budget import Budget

    b = Budget(max_usd=5.0)
    b.charge()
    assert b.estimated is True

    exact = Budget(max_usd=5.0)
    exact.charge(input_tokens=10, output_tokens=2)
    assert exact.estimated is False
