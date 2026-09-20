"""Tests for the wire format.

Both sides of this are pinned for a long time, so these tests are deliberately
literal about bytes and field names. A change that breaks one of them is a
protocol change and needs a VERSION bump, not a test edit.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gauntlet.protocol import NOTIFICATIONS, VERSION, Option, ProtocolError, Request, Response

# The request shape from ARCHITECTURE.md, as the Java bridge writes it.
WIRE_LINE = json.dumps(
    {
        "v": 1,
        "id": 47,
        "seat": "A",
        "kind": "cast_or_pass",
        "prompt": "Main phase 1. Choose a spell or ability to play.",
        "options": [
            {"i": 0, "label": "Pass priority"},
            {"i": 1, "label": "Cast Cultivate", "cost": "{2}{G}", "card": "cultivate"},
            {"i": 2, "label": "Play Forest", "card": "forest"},
        ],
        "state": {"turn": 7, "phase": "main1", "active": "A"},
        "new_cards": {"cultivate": {"name": "Cultivate", "cost": "{2}{G}"}},
    }
)


def wire(**overrides: Any) -> str:
    raw = json.loads(WIRE_LINE)
    raw.update(overrides)
    return json.dumps(raw)


def test_parse_reads_every_field_of_a_realistic_request() -> None:
    request = Request.parse(WIRE_LINE)

    assert request.id == 47
    assert request.seat == "A"
    assert request.kind == "cast_or_pass"
    assert request.prompt == "Main phase 1. Choose a spell or ability to play."
    assert request.state == {"turn": 7, "phase": "main1", "active": "A"}
    assert request.new_cards == {"cultivate": {"name": "Cultivate", "cost": "{2}{G}"}}
    assert request.extra == {}


def test_parse_reads_options_as_an_indexed_tuple() -> None:
    options = Request.parse(WIRE_LINE).options

    assert options == (
        Option(index=0, label="Pass priority", card=None, cost=None),
        Option(index=1, label="Cast Cultivate", card="cultivate", cost="{2}{G}"),
        Option(index=2, label="Play Forest", card="forest", cost=None),
    )


def test_parse_accepts_bytes_as_well_as_str() -> None:
    # The socket reader hands over the raw line, it does not decode first.
    assert Request.parse(WIRE_LINE.encode()).id == 47


def test_parse_rejects_a_version_it_does_not_know() -> None:
    with pytest.raises(ProtocolError, match="unsupported protocol version"):
        Request.parse(wire(v=VERSION + 1))


def test_parse_rejects_a_message_with_no_version_at_all() -> None:
    raw = json.loads(WIRE_LINE)
    del raw["v"]
    with pytest.raises(ProtocolError, match="unsupported protocol version"):
        Request.parse(json.dumps(raw))


def test_parse_rejects_a_line_that_is_not_json() -> None:
    with pytest.raises(ProtocolError, match="not JSON"):
        Request.parse("Exception in thread main java.lang.NullPointerException\n")


def test_parse_rejects_json_that_is_not_an_object() -> None:
    with pytest.raises(ProtocolError, match="not an object"):
        Request.parse("[1, 2, 3]")


def test_parse_rejects_a_message_with_no_kind() -> None:
    raw = json.loads(WIRE_LINE)
    del raw["kind"]
    with pytest.raises(ProtocolError, match="no kind"):
        Request.parse(json.dumps(raw))


def test_parse_keeps_unknown_fields_in_extra() -> None:
    # The forward-compatibility guarantee. A newer Java side can add fields and
    # an older Python side must carry them into the transcript rather than drop
    # them, otherwise an upgrade on one side alone loses data silently.
    request = Request.parse(wire(proposed=[1, 2], targets={"cultivate": ["forest"]}, mode="split"))

    assert request.extra == {
        "proposed": [1, 2],
        "targets": {"cultivate": ["forest"]},
        "mode": "split",
    }


def test_parse_keeps_known_fields_out_of_extra() -> None:
    # The flip side. A field this version models must not be duplicated into
    # extra, or the control reply would emit it twice.
    assert "state" not in Request.parse(WIRE_LINE).extra


def test_wants_answer_is_false_for_a_notification() -> None:
    # id 0 means nobody is blocking on this, so the server must not try to
    # route it to a seat.
    notification = Request.parse(
        json.dumps({"v": VERSION, "id": 0, "kind": "game_result", "game": 1, "winner": "A"})
    )

    assert notification.wants_answer is False
    assert notification.kind in NOTIFICATIONS
    assert notification.extra == {"game": 1, "winner": "A"}


def test_wants_answer_is_true_for_a_positive_id() -> None:
    assert Request.parse(WIRE_LINE).wants_answer is True


def test_encode_round_trips_through_json() -> None:
    encoded = Response(id=47, choice=1, why="Ramp now, curve is the constraint.").encode()

    assert isinstance(encoded, bytes)
    assert encoded.endswith(b"\n")
    # One line, no embedded newlines, or the reader on the far side desyncs.
    assert encoded.count(b"\n") == 1
    assert json.loads(encoded) == {
        "v": VERSION,
        "id": 47,
        "choice": 1,
        "why": "Ramp now, curve is the constraint.",
    }


def test_encode_keeps_non_ascii_reasoning_readable() -> None:
    # ensure_ascii is off on purpose, the transcript is read by people.
    encoded = Response(id=1, choice=0, why="hold up Négation").encode()

    assert "Négation" in encoded.decode("utf-8")


def test_validate_against_accepts_an_offered_choice() -> None:
    request = Request.parse(WIRE_LINE)

    assert Response(id=47, choice=2).validate_against(request) is None


def test_validate_against_rejects_an_out_of_range_choice() -> None:
    request = Request.parse(WIRE_LINE)

    with pytest.raises(ProtocolError, match=r"choice 9 is not one of \[0, 1, 2\]"):
        Response(id=47, choice=9).validate_against(request)


def test_validate_against_rejects_an_id_mismatch() -> None:
    # Catching this here is what keeps a stale answer from being attributed to
    # the question it happens to land on.
    request = Request.parse(WIRE_LINE)

    with pytest.raises(ProtocolError, match="answered 46, was asked 47"):
        Response(id=46, choice=1).validate_against(request)


def test_validate_against_allows_any_choice_when_no_options_were_offered() -> None:
    # Some kinds carry their answer in a payload field rather than an index,
    # so an empty option list is not a reason to reject.
    request = Request.parse(wire(options=[]))

    assert Response(id=47, choice=99).validate_against(request) is None
