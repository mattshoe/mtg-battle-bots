"""Tests for the transcript writer and renderer.

Requests are built with ``Request.parse`` on real wire lines rather than by
constructing the dataclass, so a change to the protocol breaks these too.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from gauntlet.protocol import VERSION, Request, Response
from gauntlet.transcript import Transcript, list_matches, render, summarise

SEATS = [
    {
        "seat": "A",
        "deck_name": "Atraxa Superfriends",
        "deck_source": "decks/atraxa.txt",
        "controller": "interactive",
        "decklist": ["atraxa", "cultivate"],
    },
    {
        "seat": "B",
        "deck_name": "Mono-U Control",
        "deck_source": "decks/control.txt",
        "controller": "forge",
        "decklist": ["counterspell"],
    },
]


def line(
    *,
    id: int,
    seat: str,
    kind: str = "cast_or_pass",
    prompt: str = "Main phase 1. Choose a spell or ability to play.",
    options: list[dict[str, Any]] | None = None,
    turn: int | None = 7,
    phase: str = "main1",
    active: str = "A",
) -> str:
    state: dict[str, Any] = {}
    if turn is not None:
        state = {
            "turn": turn,
            "phase": phase,
            "active": active,
            "priority": seat,
            "you": {"life": 34, "hand": ["cultivate", "forest"], "library": 71},
            "opponents": [{"seat": "B", "life": 28, "hand_size": 4}],
            "stack": [],
        }
    return json.dumps(
        {
            "v": VERSION,
            "id": id,
            "seat": seat,
            "kind": kind,
            "prompt": prompt,
            "options": options
            if options is not None
            else [
                {"i": 0, "label": "Pass priority"},
                {"i": 1, "label": "Cast Cultivate", "cost": "{2}{G}", "card": "cultivate"},
            ],
            "state": state,
            "new_cards": {},
        }
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "transcripts.db"


def started(db: Path, match_id: str = "m1") -> Transcript:
    t = Transcript(db)
    t.start_match(
        match_id=match_id,
        format="commander",
        seed=90210,
        seats=SEATS,
        policy={"attack": "judgment"},
        forge_version="2.0.01",
        bridge_revision="abc1234",
    )
    return t


def test_schema_creation_is_idempotent(db: Path) -> None:
    first = Transcript(db)
    first.close()
    second = Transcript(db)
    second.start_match(
        match_id="m1",
        format="commander",
        seed=1,
        seats=SEATS,
        policy={},
        forge_version="",
        bridge_revision="",
    )
    second.close()
    third = Transcript(db)
    third.close()

    conn = sqlite3.connect(db)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert {"matches", "seats", "decisions", "games", "events"} <= names
    assert mode == "wal"
    assert len(list_matches(db)) == 1


def test_decision_round_trips(db: Path) -> None:
    t = started(db)
    request = Request.parse(line(id=47, seat="A"))
    response = Response(id=47, choice=1, why="Ramp now. I am one land short of double-spelling.")
    seq = t.record_decision(
        match_id="m1", seat="A", request=request, response=response, latency_ms=812
    )
    t.close()

    assert seq == 1
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM decisions WHERE match_id='m1' AND seq=1").fetchone()
        seat = conn.execute("SELECT * FROM seats WHERE match_id='m1' AND seat='A'").fetchone()
    finally:
        conn.close()

    assert row["seat"] == "A"
    assert row["kind"] == "cast_or_pass"
    assert row["turn"] == 7
    assert row["phase"] == "main1"
    assert row["chosen"] == 1
    assert row["chosen_label"] == "Cast Cultivate {2}{G}"
    assert row["why"] == response.why
    assert row["latency_ms"] == 812
    assert row["fallback"] == 0
    assert json.loads(row["state"])["you"]["life"] == 34
    assert json.loads(row["options"])[1]["cost"] == "{2}{G}"
    assert json.loads(seat["decklist"]) == ["atraxa", "cultivate"]


def test_render_groups_by_turn_and_seat(db: Path) -> None:
    t = started(db)
    t.record_decision(
        match_id="m1",
        seat="A",
        request=Request.parse(line(id=1, seat="A")),
        response=Response(id=1, choice=1, why="Ramp now."),
        latency_ms=700,
    )
    t.record_decision(
        match_id="m1",
        seat="B",
        request=Request.parse(line(id=2, seat="B")),
        response=Response(id=2, choice=0, why="Holding up Counterspell."),
        latency_ms=650,
    )
    t.record_decision(
        match_id="m1",
        seat="A",
        request=Request.parse(
            line(
                id=3,
                seat="A",
                kind="attack",
                phase="combat",
                turn=8,
                options=[
                    {"i": 0, "label": "No attacks"},
                    {"i": 1, "label": "Attack with Llanowar Elves"},
                ],
            )
        ),
        response=Response(id=3, choice=1, why="B has no untapped blockers."),
        latency_ms=900,
    )
    t.record_game(match_id="m1", game_no=1, winner_seat="A", draw=False, turns=8, ms=41000)
    t.finish_match(match_id="m1")
    t.close()

    out = render("m1", db)
    assert "## Turn 7 — A's turn" in out
    assert "## Turn 8 — A's turn" in out
    assert out.index("## Turn 7") < out.index("## Turn 8")
    assert "**A** main1 · Cast Cultivate {2}{G}" in out
    assert "> Ramp now." in out
    assert "**B** main1 · Pass priority" in out
    assert "> Holding up Counterspell." in out
    assert "**A** combat · Attack with Llanowar Elves" in out
    assert "- Game 1: A won (8 turns, 41.0s)" in out
    # Turn 7 holds both seats, turn 8 only the attack.
    turn7 = out[out.index("## Turn 7") : out.index("## Turn 8")]
    assert "**A**" in turn7 and "**B**" in turn7
    assert "Llanowar" not in turn7

    # The default render stays skimmable, verbose carries the evidence.
    assert "```json" not in out
    assert "Pass priority" in out  # as a choice, not as an option dump
    loud = render("m1", db, verbose=True)
    assert "```json" in loud
    assert "- [x] 1 Cast Cultivate {2}{G}" in loud
    assert "- [ ] 0 Pass priority" in loud


def test_fallback_is_marked_in_render(db: Path) -> None:
    t = started(db)
    t.record_decision(
        match_id="m1",
        seat="A",
        request=Request.parse(line(id=9, seat="A")),
        response=None,
        latency_ms=30000,
        fallback=True,
        fallback_reason="timed out",
    )
    t.close()

    out = render("m1", db)
    assert "(Forge AI decided — timed out)" in out
    # A fallback must never read as the seat's own play.
    assert "> " not in out
    assert summarise("m1", db)["fallbacks"] == 1


def test_a_fallback_carrying_a_response_is_refused(db: Path) -> None:
    """The invariant, enforced where the row is written.

    A fallback is Forge's play. Recording one with a seat's response would put
    Forge's choice and a seat's reasoning in the same row, which is the single
    thing this transcript must never say. The previous version of this test
    asserted the rendered output of exactly that row, so it documented the hole
    rather than closing it.
    """
    t = started(db)
    with pytest.raises(ValueError, match="fallback cannot carry a response"):
        t.record_decision(
            match_id="m1",
            seat="B",
            request=Request.parse(line(id=10, seat="B")),
            response=Response(id=10, choice=0, why="my own reasoning"),
            latency_ms=12,
            fallback=True,
            fallback_reason="invalid answer",
        )
    t.close()


def test_summarise_counts(db: Path) -> None:
    t = started(db)
    for i, (seat, latency) in enumerate([("A", 100), ("B", 200), ("A", 300), ("B", 900)], start=1):
        t.record_decision(
            match_id="m1",
            seat=seat,
            request=Request.parse(line(id=i, seat=seat)),
            # A fallback carries no response, which the writer now enforces.
            response=None if i == 4 else Response(id=i, choice=0, why="thinking"),
            latency_ms=latency,
            fallback=(i == 4),
            fallback_reason="timed out" if i == 4 else "",
        )
    t.record_event(match_id="m1", kind="game_over", payload={"winner": "A"})
    t.record_game(match_id="m1", game_no=1, winner_seat="A", draw=False, turns=11, ms=52000)
    t.record_game(match_id="m1", game_no=2, winner_seat="A", draw=False, turns=9, ms=40000)
    t.finish_match(match_id="m1")
    t.close()

    s = summarise("m1", db)
    assert s["exists"] is True
    assert s["status"] == "finished"
    assert s["format"] == "commander"
    assert s["seed"] == "90210"
    assert s["decisions"] == 4
    assert s["events"] == 1
    assert s["games"] == 2
    assert s["fallbacks"] == 1
    assert s["wins"] == {"A": 2}
    assert s["winner"] == "A"
    assert s["draws"] == 0
    assert s["by_seat"]["A"]["decisions"] == 2
    assert s["by_seat"]["B"]["fallbacks"] == 1
    assert s["by_kind"] == {"cast_or_pass": 4}
    assert s["unknown_kinds"] == []
    assert s["latency_ms"]["n"] == 4
    assert s["latency_ms"]["max"] == 900
    assert s["latency_ms"]["mean"] == 375


def test_unknown_kind_is_reported(db: Path) -> None:
    t = started(db)
    t.record_decision(
        match_id="m1",
        seat="A",
        request=Request.parse(line(id=1, seat="A", kind="assign_damage")),
        response=Response(id=1, choice=0, why=""),
        latency_ms=10,
    )
    t.close()
    assert summarise("m1", db)["unknown_kinds"] == ["assign_damage"]


def test_render_works_on_an_unfinished_match(db: Path) -> None:
    t = started(db)
    t.record_decision(
        match_id="m1",
        seat="A",
        request=Request.parse(line(id=1, seat="A", kind="mulligan", turn=None)),
        response=Response(id=1, choice=0, why="Two lands and a rock, keep."),
        latency_ms=400,
    )
    t.record_decision(
        match_id="m1",
        seat="A",
        request=Request.parse(line(id=2, seat="A")),
        response=Response(id=2, choice=1, why="Ramp now."),
        latency_ms=500,
    )

    out = render("m1", db)
    assert "status running" in out
    assert "still running" in out
    assert "## Pre-game" in out
    assert "> Two lands and a rock, keep." in out
    assert "## Turn 7 — A's turn" in out
    assert "## Result" not in out
    assert summarise("m1", db)["status"] == "running"

    # Rows written before a crash survive, and the render says what happened.
    t.finish_match(match_id="m1", status="crashed")
    t.close()
    crashed = render("m1", db)
    assert "status crashed" in crashed
    assert "> Ramp now." in crashed


def test_render_separates_games(db: Path) -> None:
    t = started(db)
    t.record_decision(
        match_id="m1",
        seat="A",
        request=Request.parse(line(id=1, seat="A", turn=3)),
        response=Response(id=1, choice=1, why="Game one play."),
        latency_ms=100,
    )
    t.record_game(match_id="m1", game_no=1, winner_seat="B", draw=False, turns=3, ms=9000)
    t.record_decision(
        match_id="m1",
        seat="A",
        request=Request.parse(line(id=2, seat="A", turn=3)),
        response=Response(id=2, choice=1, why="Game two play."),
        latency_ms=100,
    )
    t.record_game(match_id="m1", game_no=2, winner_seat="A", draw=False, turns=3, ms=8000)
    t.finish_match(match_id="m1")
    t.close()

    out = render("m1", db)
    assert "# Game 1" in out
    assert "# Game 2" in out
    assert out.index("Game one play") < out.index("# Game 2") < out.index("Game two play")


def test_missing_match_renders_a_message(db: Path) -> None:
    Transcript(db).close()
    assert "No such match" in render("nope", db)
    assert summarise("nope", db)["exists"] is False


def test_list_matches_newest_first(db: Path) -> None:
    t = Transcript(db)
    for i in (1, 2, 3):
        t.start_match(
            match_id=f"m{i}",
            format="commander",
            seed=i,
            seats=SEATS,
            policy={},
            forge_version="2.0.01",
            bridge_revision="abc1234",
        )
    t.close()
    rows = list_matches(db, limit=2)
    assert [r["id"] for r in rows] == ["m3", "m2"]
    assert rows[0]["seats"][0]["deck_name"] == "Atraxa Superfriends"


def test_writes_land_before_close(db: Path) -> None:
    # A crashed run must leave everything up to the crash on disk, so the row
    # has to be readable by another connection without a close().
    t = started(db)
    t.record_decision(
        match_id="m1",
        seat="A",
        request=Request.parse(line(id=1, seat="A")),
        response=Response(id=1, choice=1, why="Committed."),
        latency_ms=10,
    )
    assert "> Committed." in render("m1", db)
    t.close()


def test_the_extra_column_keeps_what_this_version_does_not_model(db: Path) -> None:
    """`since` and `proposed` live here.

    The forward-compatibility guarantee the protocol tests establish at the
    parse layer was dropped at the storage layer, and blanking the column
    survived as a mutation.
    """
    import json as jsonlib
    import sqlite3

    t = started(db)
    request = Request.parse(
        jsonlib.dumps(
            {
                "v": VERSION,
                "id": 3,
                "seat": "A",
                "kind": "cast_or_pass",
                "prompt": "priority",
                "options": [{"i": 0, "label": "Pass priority"}],
                "state": {"turn": 2},
                "since": ["A draws a card", "A plays Forest"],
                "proposed": ["tempest-hawk"],
            }
        )
    )
    t.record_decision(
        match_id="m1",
        seat="A",
        request=request,
        response=Response(id=3, choice=0, why="holding"),
        latency_ms=5,
    )
    t.close()

    conn = sqlite3.connect(db)
    raw = conn.execute("SELECT extra FROM decisions WHERE request_id = 3").fetchone()[0]
    conn.close()

    stored = jsonlib.loads(raw)
    assert stored["since"] == ["A draws a card", "A plays Forest"]
    assert stored["proposed"] == ["tempest-hawk"]


def test_a_drawn_match_names_no_winner(db: Path) -> None:
    """Every summarise test was 2-0. Dropping the tie check survived, and a
    drawn match would have been reported as a win for whoever sorted first."""
    t = started(db)
    t.record_game(match_id="m1", game_no=1, winner_seat="A", draw=False, turns=10, ms=1)
    t.record_game(match_id="m1", game_no=2, winner_seat="B", draw=False, turns=11, ms=1)
    t.close()

    assert summarise("m1", db)["winner"] is None


def test_events_appear_in_the_play_by_play(db: Path) -> None:
    """Events are where seat_exhausted, budget_exceeded and protocol_error
    land. They are the only in-transcript signal that a run was halted rather
    than played out, and dropping them from the render survived, leaving a
    match that simply stops."""
    t = started(db)
    t.record_event(
        match_id="m1",
        kind="budget_exceeded",
        payload={"seat": "A", "reason": "spend limit reached"},
    )
    t.close()

    out = render("m1", db)
    assert "budget_exceeded" in out
    assert "spend limit" in out


def test_a_database_written_before_the_extra_column_still_works(tmp_path) -> None:
    """The migration path, which every test avoided by building a fresh file.

    On an install upgraded over an existing transcript, every record_decision
    would raise. The server would catch it and tally a fallback, so the run
    reads as entirely Forge's, one step removed from the real cause.
    """
    import json as jsonlib
    import sqlite3

    db = tmp_path / "old.db"
    t = Transcript(db)
    t.close()

    # Rewind to the pre-migration shape.
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE decisions DROP COLUMN extra")
    conn.commit()
    columns = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
    conn.close()
    assert "extra" not in columns, "the fixture did not actually rewind the schema"

    reopened = Transcript(db)
    reopened.start_match(
        match_id="old",
        format="Commander",
        seed=1,
        seats=[{"seat": "A", "controller": "forge"}],
    )
    reopened.record_decision(
        match_id="old",
        seat="A",
        request=Request.parse(
            jsonlib.dumps(
                {
                    "v": VERSION,
                    "id": 1,
                    "seat": "A",
                    "kind": "cast_or_pass",
                    "prompt": "p",
                    "options": [{"i": 0, "label": "Pass priority"}],
                    "state": {},
                }
            )
        ),
        response=Response(id=1, choice=0, why="after the migration"),
        latency_ms=1,
    )
    reopened.close()

    conn = sqlite3.connect(db)
    why = conn.execute("SELECT why FROM decisions WHERE match_id='old'").fetchone()[0]
    conn.close()
    assert why == "after the migration"
