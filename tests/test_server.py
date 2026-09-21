"""Tests for the match daemon, driven over real sockets with no Forge involved.

A fake bridge client speaks the TCP side and ``call_match`` speaks the control
side, which is the same pair of paths a real match uses. Mocking either one
would test the mock, and the parts of this file worth testing are exactly the
parts where two sockets and three threads meet.

Nothing here starts a JVM and nothing here takes longer than a few hundred
milliseconds.
"""

from __future__ import annotations

import contextlib
import json
import socket
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gauntlet import paths
from gauntlet.protocol import VERSION
from gauntlet.seats import ForgeSeat, InteractiveSeat
from gauntlet.server import MatchServer, call_match
from gauntlet.transcript import Transcript

MATCH_ID = "srv"
# Long enough for a handoff between two threads, short enough that a deadlock
# fails the test instead of hanging the suite.
PATIENT = 5.0
BRIEF = 0.1

CULTIVATE = {
    "name": "Cultivate",
    "cost": "{2}{G}",
    "type": "Sorcery",
    "text": "Search your library for up to two basic land cards.",
}
FOREST = {"name": "Forest", "type": "Basic Land - Forest", "text": "{T}: Add {G}."}


def wire(
    *,
    req_id: int,
    seat: str = "A",
    kind: str = "cast_or_pass",
    new_cards: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "v": VERSION,
        "id": req_id,
        "seat": seat,
        "kind": kind,
        "prompt": "Main phase 1. Choose a spell or ability to play.",
        "options": [
            {"i": 0, "label": "Pass priority"},
            {"i": 1, "label": "Cast Cultivate", "cost": "{2}{G}", "card": "cultivate"},
        ],
        "state": {
            "turn": 7,
            "phase": "main1",
            "active": "A",
            "you": seat,
            "me": {"life": 34, "library": 71, "hand": ["cultivate", "forest"]},
            "opponents": [{"name": "B", "life": 28, "hand_size": 4, "library": 68}],
        },
        "new_cards": new_cards or {},
        **extra,
    }


class Bridge:
    """A stand-in for one bridged player's TCP connection."""

    def __init__(self, endpoint: str) -> None:
        host, port = endpoint.rsplit(":", 1)
        self.sock = socket.create_connection((host, int(port)), timeout=PATIENT)
        self.stream = self.sock.makefile("rwb")

    def send(self, message: dict[str, Any]) -> None:
        self.stream.write(json.dumps(message).encode() + b"\n")
        self.stream.flush()

    def recv(self) -> dict[str, Any]:
        line = self.stream.readline()
        assert line, "the daemon closed the connection without answering"
        return json.loads(line)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.stream.close()
        with contextlib.suppress(OSError):
            self.sock.close()


@dataclass
class Harness:
    server: MatchServer
    seat: InteractiveSeat
    endpoint: str
    ctl_path: Path
    db_path: Path

    def bridge(self) -> Bridge:
        return Bridge(self.endpoint)

    def act(self, timeout: float = PATIENT, **fields: Any) -> dict[str, Any]:
        return call_match(
            MATCH_ID, {"op": "act", "seat": "A", "timeout": timeout, **fields}, timeout=PATIENT
        )

    def decisions(self) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM decisions WHERE match_id = ? ORDER BY seq", (MATCH_ID,)
            ).fetchall()
        finally:
            conn.close()

    def games(self) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM games WHERE match_id = ? ORDER BY game_no", (MATCH_ID,)
            ).fetchall()
        finally:
            conn.close()


@pytest.fixture
def state_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A short XDG_STATE_HOME.

    A unix socket path is capped near 104 bytes and pytest's tmp_path already
    spends most of that, so the control socket has to live somewhere shallow.
    """
    base = "/tmp" if Path("/tmp").is_dir() else None
    root = Path(tempfile.mkdtemp(prefix="gauntlet-", dir=base))
    monkeypatch.setenv("XDG_STATE_HOME", str(root))
    yield root
    for child in sorted(root.rglob("*"), reverse=True):
        with contextlib.suppress(OSError):
            child.unlink() if child.is_file() or child.is_socket() else child.rmdir()
    with contextlib.suppress(OSError):
        root.rmdir()


@pytest.fixture
def harness(tmp_path: Path, state_root: Path) -> Iterator[Harness]:
    db_path = tmp_path / "transcripts.db"
    transcript = Transcript(db_path)
    seat = InteractiveSeat()
    server = MatchServer(
        match_id=MATCH_ID,
        # "F" exists but is not interactive, "Z" does not exist at all.
        seats={"A": seat, "F": ForgeSeat()},
        transcript=transcript,
        decision_timeout=1.0,
    )
    endpoint, ctl_path = server.bind()
    server.start()

    yield Harness(server, seat, endpoint, ctl_path, db_path)

    server.shutdown()
    # Let any bridge thread finish its fallback row before the writer closes,
    # otherwise teardown races the transcript. The two accept loops are skipped,
    # they sit in accept() for up to a second after shutdown and have nothing
    # left to write.
    for thread in list(server._threads):
        if not thread.name.startswith("gauntlet-"):
            thread.join(2.0)
    transcript.close()


# ------------------------------------------------------------------- binding


def test_bind_returns_a_loopback_endpoint_and_opens_the_control_socket(harness: Harness) -> None:
    host, port = harness.endpoint.rsplit(":", 1)

    assert host == "127.0.0.1"
    # Port 0 is asked for and the assignment is read back, so a real port must
    # have come out the other side.
    assert 0 < int(port) < 65536
    assert harness.ctl_path.is_socket()

    bridge = harness.bridge()
    bridge.close()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as ctl:
        ctl.settimeout(PATIENT)
        ctl.connect(str(harness.ctl_path))


def test_shutdown_removes_the_control_socket(harness: Harness) -> None:
    harness.server.shutdown()

    assert not harness.ctl_path.exists()


# ------------------------------------------------------------ the round trip


def test_a_bridge_request_reaches_an_interactive_seat_and_the_answer_comes_back(
    harness: Harness,
) -> None:
    bridge = harness.bridge()
    try:
        bridge.send(wire(req_id=47))

        offered = harness.act()
        assert offered["status"] == "decide"
        assert offered["request"]["id"] == 47
        assert offered["request"]["seat"] == "A"
        assert offered["request"]["kind"] == "cast_or_pass"
        assert [o["i"] for o in offered["request"]["options"]] == [0, 1]

        # Submitting and collecting are one call. With nothing else pending the
        # collect half comes back empty.
        submitted = harness.act(timeout=BRIEF, id=47, choice=1, why="Ramp, curve is the constraint")
        assert submitted["status"] == "waiting"

        assert bridge.recv() == {
            "v": VERSION,
            "id": 47,
            "choice": 1,
            "why": "Ramp, curve is the constraint",
        }
    finally:
        bridge.close()

    row = harness.decisions()[0]
    assert row["seat"] == "A"
    assert row["chosen"] == 1
    assert row["chosen_label"] == "Cast Cultivate {2}{G}"
    assert row["why"] == "Ramp, curve is the constraint"
    assert row["fallback"] == 0


def test_an_unknown_seat_gets_a_defer_and_the_transcript_records_a_fallback(
    harness: Harness,
) -> None:
    # A seat nobody is holding is not an error. Forge plays the turn and the
    # transcript says so, so a run where the agent was missing cannot be
    # mistaken for one where it played badly.
    bridge = harness.bridge()
    try:
        bridge.send(wire(req_id=12, seat="Z"))

        assert bridge.recv() == {"v": VERSION, "id": 12, "choice": None}
    finally:
        bridge.close()

    row = harness.decisions()[0]
    assert row["seat"] == "Z"
    assert row["chosen"] is None
    assert row["fallback"] == 1
    assert "no seat configured for 'Z'" in row["fallback_reason"]


def test_a_seat_that_never_answers_defers_and_says_why(harness: Harness) -> None:
    bridge = harness.bridge()
    try:
        # decision_timeout is 1.0s on this harness and nobody calls act.
        bridge.send(wire(req_id=13))

        assert bridge.recv() == {"v": VERSION, "id": 13, "choice": None}
    finally:
        bridge.close()

    row = harness.decisions()[0]
    assert row["fallback"] == 1
    assert "no answer for cast_or_pass" in row["fallback_reason"]


def test_an_unreadable_line_is_recorded_and_drops_the_connection(harness: Harness) -> None:
    # Two sides that disagree about the protocol must stop talking. Carrying on
    # would corrupt the transcript quietly.
    bridge = harness.bridge()
    try:
        bridge.stream.write(b"{not json at all\n")
        bridge.stream.flush()

        assert bridge.stream.readline() == b""
    finally:
        bridge.close()

    conn = sqlite3.connect(harness.db_path)
    conn.row_factory = sqlite3.Row
    try:
        events = conn.execute("SELECT * FROM events WHERE match_id = ?", (MATCH_ID,)).fetchall()
    finally:
        conn.close()

    assert [e["kind"] for e in events] == ["protocol_error"]


# -------------------------------------------------------------- card splitting


def test_cards_carry_names_every_time_and_rules_text_only_once(harness: Harness) -> None:
    # The bridge sends oracle text once per game per seat, but `gauntlet act` is
    # a fresh process every call, so the daemon has to hand back the names for
    # whatever is on the board right now. Text is the expensive half and goes
    # out once.
    bridge = harness.bridge()
    try:
        bridge.send(wire(req_id=1, new_cards={"cultivate": CULTIVATE, "forest": FOREST}))

        first = harness.act()["request"]
        assert sorted(first["cards"]) == ["cultivate", "forest"]
        assert first["cards"]["cultivate"] == {
            "name": "Cultivate",
            "cost": "{2}{G}",
            "type": "Sorcery",
        }
        assert "text" not in first["cards"]["forest"]
        assert first["new_cards"]["cultivate"]["text"] == CULTIVATE["text"]
        assert first["new_cards"]["forest"]["text"] == FOREST["text"]

        assert harness.act(timeout=BRIEF, id=1, choice=0)["status"] == "waiting"
        bridge.recv()

        # Same cards, second question, and the bridge does not resend the text.
        bridge.send(wire(req_id=2))
        second = harness.act()["request"]
        assert sorted(second["cards"]) == ["cultivate", "forest"]
        assert second["cards"]["cultivate"]["name"] == "Cultivate"
        assert "text" not in second["cards"]["cultivate"]
        assert second["new_cards"] == {}

        assert harness.act(timeout=BRIEF, id=2, choice=0)["status"] == "waiting"
        bridge.recv()
    finally:
        bridge.close()


def test_unknown_request_fields_pass_through_to_the_agent(harness: Harness) -> None:
    bridge = harness.bridge()
    try:
        bridge.send(wire(req_id=3, proposed=[1]))

        offered = harness.act()["request"]
        assert offered["proposed"] == [1]

        harness.act(timeout=BRIEF, id=3, choice=1)
        bridge.recv()
    finally:
        bridge.close()


# ------------------------------------------------------------- game results


def test_record_game_result_is_idempotent_for_one_game_number(harness: Harness) -> None:
    # Two channels report the end of a game and both are needed. Forge prints it
    # on stdout, which is the only channel a match with no bridged seat has, and
    # it also pushes it over each bridge. Whichever lands first wins.
    payload = {"game": 1, "winner": "A", "turns": 9, "ms": 1234}

    harness.server.record_game_result(payload)
    harness.server.record_game_result(dict(payload, winner="B"))

    assert harness.server.result.games == [payload]
    assert harness.server.result.wins_by_seat() == {"A": 1}
    assert [(g["game_no"], g["winner_seat"]) for g in harness.games()] == [(1, "A")]


def test_a_game_result_notification_over_the_bridge_is_recorded(harness: Harness) -> None:
    bridge = harness.bridge()
    try:
        # id 0, so nothing is blocking on it and no answer goes back.
        bridge.send(
            {"v": VERSION, "id": 0, "seat": "A", "kind": "game_result", "game": 2, "winner": "B"}
        )
        # The second report of the same game is the stdout channel arriving late.
        harness.server.record_game_result({"game": 2, "winner": "B"})

        status = call_match(MATCH_ID, {"op": "status"}, timeout=PATIENT)
    finally:
        bridge.close()

    assert status["games"] == [{"game": 2, "winner": "B"}]
    assert status["wins"] == {"B": 1}


# ------------------------------------------------------------- control ops


def test_status_reports_the_seats_and_their_controllers(harness: Harness) -> None:
    status = call_match(MATCH_ID, {"op": "status"}, timeout=PATIENT)

    assert status == {
        "match": MATCH_ID,
        "finished": False,
        "games": [],
        "wins": {},
        "seats": {"A": "interactive", "F": "forge"},
    }


def test_an_unknown_op_is_an_error_and_not_a_crash(harness: Harness) -> None:
    reply = call_match(MATCH_ID, {"op": "frobnicate"}, timeout=PATIENT)

    assert reply == {"status": "error", "error": "unknown op 'frobnicate'"}


def test_a_control_message_that_is_not_json_is_an_error(harness: Harness) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(PATIENT)
        sock.connect(str(harness.ctl_path))
        sock.sendall(b"hello?\n")
        with sock.makefile("rb") as stream:
            reply = json.loads(stream.readline())

    assert reply["status"] == "error"
    assert reply["error"].startswith("not JSON")


def test_acting_on_a_non_interactive_seat_is_an_error(harness: Harness) -> None:
    reply = call_match(MATCH_ID, {"op": "act", "seat": "F", "timeout": BRIEF}, timeout=PATIENT)

    assert reply["status"] == "error"
    assert "is not interactive" in reply["error"]


def test_an_answer_that_does_not_fit_the_open_question_is_an_error(harness: Harness) -> None:
    bridge = harness.bridge()
    try:
        bridge.send(wire(req_id=4))
        harness.act()

        reply = harness.act(timeout=BRIEF, id=4, choice=9)
        assert reply["status"] == "error"
        assert "choice 9 is not one of [0, 1]" in reply["error"]

        # The question survives the bad answer, so the agent can try again.
        assert harness.act(timeout=BRIEF, id=4, choice=1)["status"] == "waiting"
        assert bridge.recv()["choice"] == 1
    finally:
        bridge.close()


def test_act_reports_game_over_once_the_match_has_finished(harness: Harness) -> None:
    harness.server.finished.set()

    reply = harness.act(timeout=BRIEF)

    assert reply["status"] == "game_over"
    assert reply["finished"] is True


def test_call_match_refuses_a_match_that_is_not_running(state_root: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no running match 'nobody'"):
        call_match("nobody", {"op": "status"}, timeout=BRIEF)

    assert not paths.match_socket("nobody").exists()


def test_card_text_is_shown_again_in_a_new_game(tmp_path) -> None:
    """Rules text is sent once per game, not once per match.

    Each `gauntlet act` is a fresh process with no memory, so the daemon
    remembers on the agent's behalf. Holding that memory across a game boundary
    meant an agent played games two onward reading slugs.
    """
    import json
    import socket

    from gauntlet.seats import InteractiveSeat
    from gauntlet.server import MatchServer, call_match
    from gauntlet.transcript import Transcript

    def wire(rid: int) -> bytes:
        return (
            json.dumps(
                {
                    "v": 1,
                    "id": rid,
                    "seat": "A",
                    "kind": "cast_or_pass",
                    "prompt": "priority",
                    "options": [{"i": 0, "label": "Pass priority"}],
                    "state": {"turn": 1, "me": {"hand": ["cultivate"]}},
                    "new_cards": {"cultivate": {"name": "Cultivate", "text": "Search."}},
                }
            )
            + "\n"
        ).encode()

    server = MatchServer(
        match_id="newgame",
        seats={"A": InteractiveSeat()},
        transcript=Transcript(tmp_path / "t.db"),
        decision_timeout=2.0,
    )
    endpoint, _ = server.bind()
    server.start()
    host, port = endpoint.split(":")
    try:
        with socket.create_connection((host, int(port)), timeout=5) as sock:
            stream = sock.makefile("rwb")

            stream.write(wire(1))
            stream.flush()
            first = call_match("newgame", {"op": "act", "seat": "A", "timeout": 3})
            assert "cultivate" in first["request"]["new_cards"]
            call_match(
                "newgame", {"op": "act", "seat": "A", "choice": 0, "why": "x", "timeout": 0.2}
            )
            stream.readline()

            server.record_game_result({"game": 1, "winner": "A", "draw": False})

            stream.write(wire(2))
            stream.flush()
            second = call_match("newgame", {"op": "act", "seat": "A", "timeout": 3})
            assert "cultivate" in second["request"]["new_cards"], (
                "a new game started and the agent was never told what its cards do"
            )
    finally:
        server.shutdown()
