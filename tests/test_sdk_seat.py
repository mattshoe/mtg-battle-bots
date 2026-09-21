"""The SDK seat, without ever reaching the network.

The client is faked throughout. These tests are about the seat's own logic: how
it decides a reply is unusable, how it tells a bad answer from a dead session,
and whether it cleans up after itself. None of that needs a model.
"""

from __future__ import annotations

import threading

import pytest

from gauntlet.protocol import Option, Request
from gauntlet.sdk_seat import SdkSeat, _terminal_reply
from gauntlet.seats import SeatExhausted, SeatTimeout


def _request(**kw) -> Request:
    return Request(
        id=kw.get("id", 1),
        seat="A",
        kind="cast_or_pass",
        prompt="You have priority.",
        options=(
            Option(index=0, label="Pass priority"),
            Option(index=1, label="Cast Cultivate", cost="{2}{G}"),
        ),
        state={"turn": 3, "phase": "main1", "active": "A", "you": "A", "me": {"life": 40}},
        new_cards=kw.get("new_cards", {}),
    )


class _Scripted(SdkSeat):
    """An SDK seat whose model replies are supplied by the test."""

    def __init__(self, replies: list[str], **kw) -> None:
        super().__init__(**kw)
        self.replies = list(replies)
        self.prompts: list[str] = []

    def _ensure_loop(self):
        return _Immediate(self)


class _Immediate:
    """Stands in for the event loop, answering from the script synchronously."""

    def __init__(self, seat: _Scripted) -> None:
        self.seat = seat


def _patch_ask(monkeypatch, seat: _Scripted) -> None:
    """Replace the async round trip with the next scripted reply."""
    import asyncio

    def run_coroutine_threadsafe(coro, loop):
        coro.close()

        class _Future:
            def result(self, timeout=None):
                if not seat.replies:
                    raise AssertionError("the seat asked more times than the script allows")
                return seat.replies.pop(0)

            def cancel(self):
                return True

        return _Future()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", run_coroutine_threadsafe)


# ----------------------------------------------------- recognising a dead seat


@pytest.mark.parametrize(
    "reply",
    [
        "You've hit your session limit · resets 3pm (America/New_York)",
        "YOU HAVE HIT YOUR USAGE LIMIT",
        "Error: rate limit exceeded, try again later",
        "Your quota for this period is used up",
        "I will not respond to Magic game prompts.",
        "Session conclusively ended. No further responses.",
    ],
)
def test_a_dead_session_is_recognised(reply: str) -> None:
    assert _terminal_reply(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "CHOICE: 1\nWHY: ramping",
        "CHOICE: 0",
        "The limit of what I can see is the board.",
        "I will cast Cultivate.\nCHOICE: 1\nWHY: ramp",
        "",
    ],
)
def test_an_ordinary_reply_is_not_mistaken_for_a_dead_session(reply: str) -> None:
    """A false positive here ends a run that had plenty of capacity left, so the
    markers have to be specific enough not to fire on ordinary Magic talk."""
    assert not _terminal_reply(reply)


def test_the_word_limit_alone_does_not_kill_a_seat() -> None:
    """'limit' appears in real card text and real reasoning."""
    assert not _terminal_reply("CHOICE: 1\nWHY: One creature is the limit of what they can block.")


# ------------------------------------------------------------- answering


def test_a_well_formed_reply_becomes_a_response(monkeypatch) -> None:
    seat = _Scripted(["CHOICE: 1\nWHY: Ramp now, the curve is the constraint."])
    _patch_ask(monkeypatch, seat)
    response = seat.decide(_request(), timeout=5)
    assert response.choice == 1
    assert "curve" in response.why


def test_an_unparseable_reply_is_a_timeout_not_an_exhaustion(monkeypatch) -> None:
    """One bad answer costs one decision. Confusing it with a dead session would
    end a run that could have carried on."""
    seat = _Scripted(["I would like to play a land please"])
    _patch_ask(monkeypatch, seat)
    with pytest.raises(SeatTimeout):
        seat.decide(_request(), timeout=5)
    # and the seat is still usable
    assert not seat._closed


def test_a_dead_session_closes_the_seat_for_good(monkeypatch) -> None:
    """Every later decision must fail immediately rather than pay a round trip
    to be told the same thing."""
    seat = _Scripted(["You've hit your session limit · resets 3pm"])
    _patch_ask(monkeypatch, seat)

    with pytest.raises(SeatExhausted):
        seat.decide(_request(), timeout=5)
    assert seat._closed

    # Second call does not reach the model at all, so the empty script is fine.
    with pytest.raises(SeatExhausted):
        seat.decide(_request(id=2), timeout=5)


def test_an_out_of_range_choice_is_rejected(monkeypatch) -> None:
    seat = _Scripted(["CHOICE: 7\nWHY: not an option"])
    _patch_ask(monkeypatch, seat)
    with pytest.raises(SeatTimeout, match="options are"):
        seat.decide(_request(), timeout=5)


def test_card_text_is_remembered_across_decisions(monkeypatch) -> None:
    """The bridge sends a card's text once. A seat that forgot it would be
    reasoning about slugs for the rest of the game."""
    seat = _Scripted(["CHOICE: 0\nWHY: one", "CHOICE: 0\nWHY: two"])
    _patch_ask(monkeypatch, seat)

    seat.decide(_request(new_cards={"cultivate": {"name": "Cultivate"}}), timeout=5)
    assert "cultivate" in seat._seen_cards

    seat.decide(_request(id=2), timeout=5)
    assert "cultivate" in seat._seen_cards


def test_deciding_on_a_closed_seat_raises_exhausted() -> None:
    seat = SdkSeat()
    seat.close()
    with pytest.raises(SeatExhausted):
        seat.decide(_request(), timeout=1)


def test_close_is_safe_to_call_twice() -> None:
    """Shutdown runs it, and a caller may too."""
    seat = SdkSeat()
    seat.close()
    seat.close()


def test_the_seat_reports_no_usage_so_the_budget_estimates() -> None:
    """The Agent SDK does not expose token counts. The budget has to know that
    rather than assume zero, or a run would look free."""
    assert SdkSeat().last_usage is None


def test_the_event_loop_thread_is_a_daemon() -> None:
    """A non-daemon thread here would keep the process alive after a match."""
    seat = SdkSeat()
    try:
        seat._ensure_loop()
        assert seat._thread is not None
        assert seat._thread.daemon
    finally:
        seat.close()


def test_two_seats_do_not_share_a_loop() -> None:
    """One session per seat. Sharing would serialise two players' decisions
    behind each other."""
    a, b = SdkSeat(), SdkSeat()
    try:
        a._ensure_loop()
        b._ensure_loop()
        assert a._loop is not b._loop
    finally:
        a.close()
        b.close()


def test_closing_stops_the_loop_thread() -> None:
    seat = SdkSeat()
    seat._ensure_loop()
    thread = seat._thread
    seat.close()
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive(), "the seat left its event loop running"


def test_concurrent_close_and_decide_do_not_deadlock(monkeypatch) -> None:
    """Shutdown races the last decision at the end of every match."""
    seat = _Scripted(["CHOICE: 0\nWHY: fine"] * 5)
    _patch_ask(monkeypatch, seat)
    errors: list[BaseException] = []

    def decide():
        try:
            seat.decide(_request(), timeout=2)
        except (SeatTimeout, SeatExhausted):
            pass
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=decide) for _ in range(3)]
    for t in threads:
        t.start()
    seat.close()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()
    assert not errors, errors
