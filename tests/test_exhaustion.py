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
        assert server.exhausted == {"A": "You've hit your session limit"}
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
