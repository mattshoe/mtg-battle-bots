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
