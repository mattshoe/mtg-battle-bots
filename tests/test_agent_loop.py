"""The path an agent actually takes, end to end.

`gauntlet run` defaults to two interactive seats, which detaches into
`gauntlet.daemon`, and the agent then calls `gauntlet act` once per decision.
It is the documented primary mode, the one the skill drives, and the one that
spends session quota. Fifteen of fifteen mutations to it survived, because
`daemon.py` was at zero coverage and `act`'s success path was never executed.

A fake bridge stands in for Forge. No JVM, no model, no spend.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
from typer.testing import CliRunner

from gauntlet import match as matchmod
from gauntlet.cli import app
from gauntlet.seats import InteractiveSeat
from gauntlet.server import MatchServer
from gauntlet.transcript import Transcript

runner = CliRunner()

REQUEST = {
    "v": 1,
    "id": 1,
    "seat": "A",
    "kind": "cast_or_pass",
    "prompt": "You have priority. Choose something to play, or pass.",
    "options": [
        {"i": 0, "label": "Pass priority"},
        {"i": 1, "label": "Cultivate - Sorcery", "card": "cultivate", "cost": "{2}{G}"},
    ],
    "state": {
        "turn": 4,
        "phase": "main1",
        "active": "A",
        "you": "A",
        "me": {"life": 40, "hand": ["cultivate"], "library": 90, "battlefield": []},
        "opponents": [{"name": "B", "life": 38, "hand_size": 5, "library": 89}],
    },
    "new_cards": {
        "cultivate": {
            "name": "Cultivate",
            "cost": "{2}{G}",
            "type": "Sorcery",
            "text": "Search your library for up to two basic land cards.",
        }
    },
    "since": ["A's Draw Step", "A draws a card"],
}


@pytest.fixture
def live_match(tmp_path):
    """A running match with one interactive seat and a fake bridge attached."""
    server = MatchServer(
        match_id="agentloop",
        seats={"A": InteractiveSeat()},
        transcript=Transcript(tmp_path / "t.db"),
        decision_timeout=5.0,
    )
    server.result.expected_seats = {"A"}
    endpoint, _ = server.bind()
    server.start()

    host, port = endpoint.split(":")
    sock = socket.create_connection((host, int(port)), timeout=5)
    stream = sock.makefile("rwb")

    def ask(rid: int, **overrides) -> None:
        payload = {**REQUEST, "id": rid, **overrides}
        stream.write((json.dumps(payload) + "\n").encode())
        stream.flush()

    def answer_seen(timeout: float = 5.0):
        sock.settimeout(timeout)
        return json.loads(stream.readline())

    try:
        yield server, ask, answer_seen
    finally:
        sock.close()
        server.shutdown()


def test_an_agent_reads_a_real_question_and_answers_it(live_match) -> None:
    """One full round trip through the command an agent runs for every play.

    Rendering a blank question, dropping the card names, and losing the event
    feed all survived as mutations, because the success path of `act` had never
    been executed.
    """
    _server, ask, answer_seen = live_match
    ask(1)

    shown = runner.invoke(app, ["act", "--match", "agentloop", "--seat", "A", "--timeout", "5"])
    assert shown.exit_code == 0, shown.output

    # The board, in the words a seat reasons about rather than slugs.
    assert "Cultivate" in shown.output, "the agent was shown slugs, not card names"
    assert "Search your library" in shown.output, "rules text never reached the agent"
    assert "[1]" in shown.output and "Pass priority" in shown.output
    assert "40 life" in shown.output
    assert "Since your last decision:" in shown.output, "the event feed was dropped"
    # And nothing it must not see.
    assert "89" in shown.output, "the opponent's library count is public"
    assert "hand_size" not in shown.output

    answered = runner.invoke(
        app,
        [
            "act",
            "--match",
            "agentloop",
            "--seat",
            "A",
            "--choice",
            "1",
            "--why",
            "Ramp now, the curve is the constraint.",
            "--timeout",
            "1",
        ],
    )
    assert answered.exit_code == 0, answered.output

    reply = answer_seen()
    assert reply["choice"] == 1, "the agent's choice never reached the engine"


def test_the_reasoning_survives_the_trip_to_the_transcript(live_match, tmp_path) -> None:
    """--why is the product. Dropping it between the CLI and the daemon
    survived, and every transcript would have come back empty."""
    import sqlite3

    _server, ask, answer_seen = live_match
    ask(1)
    runner.invoke(app, ["act", "--match", "agentloop", "--seat", "A", "--timeout", "5"])
    runner.invoke(
        app,
        [
            "act",
            "--match",
            "agentloop",
            "--seat",
            "A",
            "--choice",
            "0",
            "--why",
            "Holding up interaction for their commander.",
            "--timeout",
            "1",
        ],
    )
    answer_seen()

    conn = sqlite3.connect(tmp_path / "t.db")
    why = conn.execute("SELECT why FROM decisions WHERE match_id = 'agentloop'").fetchone()[0]
    conn.close()
    assert "Holding up interaction" in why


def test_taking_the_same_question_twice_shows_it_twice(live_match) -> None:
    """An agent that lost its place asks again. That is the recovery path."""
    _server, ask, _seen = live_match
    ask(1)

    first = runner.invoke(app, ["act", "--match", "agentloop", "--seat", "A", "--timeout", "5"])
    second = runner.invoke(app, ["act", "--match", "agentloop", "--seat", "A", "--timeout", "5"])

    assert "Cultivate" in first.output
    assert "Cultivate" in second.output, "asking again lost the card text"
    assert "decision 1" in second.output


def test_a_stale_answer_is_refused_rather_than_applied(tmp_path) -> None:
    """The late-answer guard, at the layer `gauntlet act` goes through.

    It is tested thoroughly on the seat and was tested nowhere on the daemon,
    so resolving an unqualified answer with `current()` instead of `open_id()`
    survived. That applies an agent's stale choice to a different question with
    a different option list, under the agent's own reasoning.
    """
    # Its own server, with a timeout short enough that the engine really does
    # give up while the agent is still thinking.
    server = MatchServer(
        match_id="stale",
        seats={"A": InteractiveSeat()},
        transcript=Transcript(tmp_path / "t.db"),
        decision_timeout=0.3,
    )
    endpoint, _ = server.bind()
    server.start()
    host, port = endpoint.split(":")

    try:
        with socket.create_connection((host, int(port)), timeout=5) as sock:
            stream = sock.makefile("rwb")

            stream.write((json.dumps({**REQUEST, "id": 1}) + "\n").encode())
            stream.flush()
            shown = runner.invoke(app, ["act", "--match", "stale", "--seat", "A", "--timeout", "3"])
            assert "decision 1" in shown.output

            # The engine waits its 0.3s, gives up, and asks the next question.
            deferred = json.loads(stream.readline())
            assert deferred["choice"] is None, "the engine did not fall back"

            stream.write((json.dumps({**REQUEST, "id": 2}) + "\n").encode())
            stream.flush()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                current = server.seats["A"].current()
                if current is not None and current.id == 2:
                    break
                time.sleep(0.02)

            late = runner.invoke(
                app,
                [
                    "act",
                    "--match",
                    "stale",
                    "--seat",
                    "A",
                    "--choice",
                    "1",
                    "--why",
                    "for the board that existed a moment ago",
                    "--timeout",
                    "1",
                ],
            )
            assert late.exit_code != 0, "a stale answer was applied to a later question"
            assert "no longer open" in late.output
    finally:
        server.shutdown()


def test_an_answer_that_does_not_fit_is_reported_to_the_agent(live_match) -> None:
    """Swallowing the error left an agent looping forever, believing it had
    played."""
    _server, ask, _seen = live_match
    ask(1)
    runner.invoke(app, ["act", "--match", "agentloop", "--seat", "A", "--timeout", "5"])

    bad = runner.invoke(
        app,
        [
            "act",
            "--match",
            "agentloop",
            "--seat",
            "A",
            "--choice",
            "99",
            "--why",
            "not an option",
            "--timeout",
            "1",
        ],
    )
    assert bad.exit_code != 0
    assert "99" in bad.output


def test_acting_without_a_reason_warns(live_match) -> None:
    """The transcript is the product, and a blank --why makes it worthless."""
    _server, ask, _seen = live_match
    ask(1)
    runner.invoke(app, ["act", "--match", "agentloop", "--seat", "A", "--timeout", "5"])

    result = runner.invoke(
        app, ["act", "--match", "agentloop", "--seat", "A", "--choice", "0", "--timeout", "1"]
    )
    assert "no --why" in result.output


# ----------------------------------------------------------------- the daemon


def test_the_daemon_runs_a_plan_and_honours_its_budget(_isolated, tmp_path, monkeypatch) -> None:
    """`daemon.py` was at zero coverage.

    Dropping the budget from the call it makes survived, and detaching is what
    every interactive run does, so that is the configuration where an uncapped
    run spends real money.
    """
    from gauntlet import daemon
    from gauntlet.budget import Budget

    deck = tmp_path / "d.txt"
    deck.write_text(
        "Commander: Jetmir, Nexus of Revels\n" + "\n".join(f"1 C{i}" for i in range(99))
    )

    planned = matchmod.plan(
        [matchmod.SeatSpec(seat="A", deck=str(deck), controller="forge")],
        budget=Budget(max_usd=2.5, max_decisions=99, model="claude-opus-5"),
    )
    plan_path = matchmod._dump_plan(planned)

    seen: dict = {}

    def fake_run(plan_, *, budget=None, **kw):
        from gauntlet.server import MatchResult

        seen["budget"] = budget
        return MatchResult(match_id=plan_.match_id)

    monkeypatch.setattr(daemon, "run", fake_run)
    code = daemon.main([str(plan_path)])

    assert code == 0
    assert seen["budget"] is not None, "the detached run was uncapped"
    assert seen["budget"].max_usd == 2.5
    assert seen["budget"].max_decisions == 99
    assert not plan_path.exists(), "the plan file was left behind and reads as a live match"


def test_the_daemon_reports_a_crashed_match_as_a_failure(_isolated, tmp_path, monkeypatch) -> None:
    from gauntlet import daemon

    deck = tmp_path / "d.txt"
    deck.write_text("Commander: Jetmir, Nexus of Revels\n1 Sol Ring\n")
    plan_path = matchmod._dump_plan(
        matchmod.plan([matchmod.SeatSpec(seat="A", deck=str(deck), controller="forge")])
    )

    def crashed(plan_, **kw):
        from gauntlet.server import MatchResult

        return MatchResult(match_id=plan_.match_id, crashed=True, error="forge exited 1")

    monkeypatch.setattr(daemon, "run", crashed)
    assert daemon.main([str(plan_path)]) == 1


def test_run_detached_waits_for_the_match_to_answer(_isolated, tmp_path, monkeypatch) -> None:
    """Returning before the control socket exists means the agent's first act
    races the daemon and fails for no reason it can act on."""
    deck = tmp_path / "d.txt"
    deck.write_text("Commander: Jetmir, Nexus of Revels\n1 Sol Ring\n")
    planned = matchmod.plan([matchmod.SeatSpec(seat="A", deck=str(deck), controller="interactive")])

    from gauntlet import paths

    started = threading.Event()

    class _Popen:
        def __init__(self, *a, **kw) -> None:
            # Whatever starts the daemon, the socket must exist before the
            # caller is told the match is up.
            def create_socket() -> None:
                time.sleep(0.2)
                paths.match_socket(planned.match_id).write_text("")
                started.set()

            threading.Thread(target=create_socket, daemon=True).start()

    monkeypatch.setattr(matchmod.subprocess, "Popen", _Popen)
    match_id = matchmod.run_detached(planned)

    assert match_id == planned.match_id
    assert started.is_set(), "run_detached returned before the match could answer"
    assert paths.match_socket(match_id).exists()


def test_every_interactive_seat_is_told_to_start(_isolated, tmp_path, monkeypatch) -> None:
    """A two-agent match where only one seat is named sits until the other
    times out. The hint is the only place the second agent is mentioned."""
    from typer.testing import CliRunner as _Runner

    deck = tmp_path / "d.txt"
    deck.write_text(
        "Commander: Jetmir, Nexus of Revels\n" + "\n".join(f"1 C{i}" for i in range(99))
    )

    monkeypatch.setattr(matchmod, "run_detached", lambda p: p.match_id)
    out = _Runner().invoke(app, ["run", "--a", str(deck), "--b", str(deck)]).output

    assert "--seat A" in out
    assert "--seat B" in out, "the second agent was never told to start"


def test_card_names_come_from_the_daemon_not_from_nowhere(live_match) -> None:
    """`cards = req.get("cards") or {}` → `{}` survived.

    Each act is a fresh process, so the daemon carries the card memory. With it
    dropped the agent reads slugs for the whole game, which is the regression in
    CLAUDE.md's table.
    """
    _server, ask, _seen = live_match
    # Second decision: the text was sent with the first, so only the daemon's
    # memory can supply a name here.
    ask(1)
    runner.invoke(app, ["act", "--match", "agentloop", "--seat", "A", "--timeout", "5"])
    runner.invoke(
        app,
        [
            "act",
            "--match",
            "agentloop",
            "--seat",
            "A",
            "--choice",
            "0",
            "--why",
            "x",
            "--timeout",
            "1",
        ],
    )
    _seen()

    ask(2, new_cards={})
    shown = runner.invoke(app, ["act", "--match", "agentloop", "--seat", "A", "--timeout", "5"])

    assert "Cultivate" in shown.output, "the agent was shown a slug it had already been taught"
    assert "cultivate" not in shown.output.replace("Cultivate", "")
