"""The seat that spends real money, which no test had ever executed.

Every mutation of `ApiSeat.decide` survived: never raising on a dead key,
throwing the reply away, dropping the cache breakpoint, sending no board state
at all. The client is faked here, so nothing reaches the network.
"""

from __future__ import annotations

import pytest

from gauntlet.budget import Budget
from gauntlet.protocol import Option, Request
from gauntlet.seats import ApiSeat, SeatExhausted, SeatTimeout


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self, **kw) -> None:
        self.input_tokens = kw.get("input_tokens", 900)
        self.output_tokens = kw.get("output_tokens", 40)
        self.cache_read_input_tokens = kw.get("cache_read_input_tokens", 0)
        self.cache_creation_input_tokens = kw.get("cache_creation_input_tokens", 0)


class _Message:
    def __init__(self, text: str, usage=None) -> None:
        self.content = [_Block(text)]
        self.usage = usage if usage is not None else _Usage()


class _FakeClient:
    """Records what it was asked and returns what the test scripted."""

    def __init__(self, reply="CHOICE: 1\nWHY: ramping into the commander", raises=None):
        self.reply = reply
        self.raises = raises
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.raises is not None:
            raise self.raises
        return self.reply if isinstance(self.reply, _Message) else _Message(self.reply)


def _seat(client) -> ApiSeat:
    seat = ApiSeat()
    seat._client = client
    return seat


def _request() -> Request:
    return Request(
        id=7,
        seat="A",
        kind="cast_or_pass",
        prompt="You have priority.",
        options=(Option(index=0, label="Pass priority"), Option(index=1, label="Cast Cultivate")),
        state={"turn": 4, "phase": "main1", "active": "A", "you": "A", "me": {"life": 40}},
        new_cards={"cultivate": {"name": "Cultivate", "text": "Search your library."}},
    )


def test_a_reply_becomes_a_response() -> None:
    client = _FakeClient()
    response = _seat(client).decide(_request(), timeout=30)
    assert response.choice == 1
    assert "ramping" in response.why
    assert response.id == 7


def test_the_board_state_is_actually_sent() -> None:
    """Sending `request.prompt` alone survived as a mutation. The seat would
    play blind on every decision and nothing would notice."""
    client = _FakeClient()
    _seat(client).decide(_request(), timeout=30)

    sent = client.calls[0]["messages"][0]["content"]
    assert "40 life" in sent
    assert "Cultivate" in sent, "the seat was not told what its cards do"
    assert "[1]" in sent, "the seat was not given its options"


def test_the_system_prompt_carries_a_cache_breakpoint() -> None:
    """It is identical on every call and a game makes hundreds. Dropping it
    roughly doubles the input bill, silently."""
    client = _FakeClient()
    _seat(client).decide(_request(), timeout=30)

    system = client.calls[0]["system"]
    assert isinstance(system, list)
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_real_token_counts_reach_the_budget() -> None:
    """`last_usage = None` survived, which billed every run at the estimate and
    left the cap trusting a guess."""
    client = _FakeClient()
    client.reply = _Message(
        "CHOICE: 0\nWHY: holding",
        _Usage(input_tokens=1000, output_tokens=100, cache_read_input_tokens=500),
    )
    seat = _seat(client)
    seat.decide(_request(), timeout=30)

    assert seat.last_usage == (1500, 100), "cached input was not counted"

    budget = Budget(max_usd=5.0, model="claude-haiku-4-5")
    budget.charge(input_tokens=seat.last_usage[0], output_tokens=seat.last_usage[1])
    assert not budget.estimated, "a known usage was recorded as an estimate"
    assert budget.spent_usd == pytest.approx(1500 / 1e6 * 1.0 + 100 / 1e6 * 5.0)


def test_usage_with_null_cache_fields_does_not_raise() -> None:
    """The SDK sends None when caching is not in play, and getattr defaults only
    on a missing attribute. This raised, was recorded as a fallback, and left
    the budget at zero while the call was billed."""
    client = _FakeClient()
    client.reply = _Message("CHOICE: 0\nWHY: x", _Usage(cache_read_input_tokens=None))
    seat = _seat(client)
    seat.decide(_request(), timeout=30)
    assert seat.last_usage == (900, 40)


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 401 authentication_error",
        "Error code: 403 permission_error",
        "Error code: 429 rate_limit_error",
        "Error code: 529 overloaded_error",
        "Your credit balance is too low to access the API",
        "Connection error.",
    ],
)
def test_a_fatal_api_failure_ends_the_seat(message: str) -> None:
    """Treating a dead key as one bad decision meant the rest of the run was
    Forge's, reported as the agent's."""
    seat = _seat(_FakeClient(raises=RuntimeError(message)))
    with pytest.raises(SeatExhausted):
        seat.decide(_request(), timeout=30)


@pytest.mark.parametrize(
    "message",
    ["Error code: 400 invalid_request_error", "Read timed out"],
)
def test_a_recoverable_failure_costs_one_decision(message: str) -> None:
    """Matched on the reason as well as the type.

    decide funnels every non-fatal failure into SeatTimeout, so a bare
    pytest.raises here passes for reasons unrelated to the name, including the
    request never reaching the client.
    """
    seat = _seat(_FakeClient(raises=RuntimeError(message)))
    with pytest.raises(SeatTimeout, match="api call failed"):
        seat.decide(_request(), timeout=30)


def test_an_unparseable_reply_is_not_fatal() -> None:
    seat = _seat(_FakeClient(reply="I would like to play a land"))
    with pytest.raises(SeatTimeout, match="unusable"):
        seat.decide(_request(), timeout=30)


def test_an_out_of_range_choice_is_refused() -> None:
    seat = _seat(_FakeClient(reply="CHOICE: 9\nWHY: not offered"))
    with pytest.raises(SeatTimeout, match="options are"):
        seat.decide(_request(), timeout=30)


def test_the_seat_declares_that_it_costs_money() -> None:
    """The budget bills only seats that say so, so a paid seat that forgot
    would run free and uncapped."""
    assert ApiSeat.costs_money is True


# ----------------------------------- the values, not just the shape

# The fake client records every kwarg and the tests above read only two of
# them, so pinning the model, ignoring max_tokens, or replacing the system
# prompt all survived. Each is a silent cost or quality change.


def test_the_model_asked_for_is_the_model_billed() -> None:
    """Pinning a model here bills up to five times per token with --model
    becoming decorative.

    The model asked for is deliberately one nobody would hardcode. An earlier
    version asked for the same model a plausible mutation pins to, so the
    mutation passed the test it was supposed to fail.
    """
    client = _FakeClient()
    seat = _seat(client)
    seat.model = "claude-sonnet-5"
    seat.decide(_request(), timeout=30)
    assert client.calls[0]["model"] == "claude-sonnet-5"


def test_the_default_model_is_the_cheap_one() -> None:
    """Picking from a numbered list a thousand times does not want Sonnet at
    five times the price."""
    assert ApiSeat().model == "claude-haiku-4-5"


def test_max_tokens_is_the_one_asked_for() -> None:
    """One choice and one sentence. A large ceiling here is a bill, not a
    better answer.

    Asserting it equals the seat's own attribute passed against a hardcoded
    value, so this asks for an unusual one.
    """
    client = _FakeClient()
    seat = _seat(client)
    seat.max_tokens = 123
    seat.decide(_request(), timeout=30)
    assert client.calls[0]["max_tokens"] == 123
    assert ApiSeat().max_tokens <= 1000, "the default ceiling is a bill, not an answer"


def test_the_system_prompt_tells_the_seat_how_to_answer() -> None:
    client = _FakeClient()
    _seat(client).decide(_request(), timeout=30)
    system = client.calls[0]["system"][0]["text"]
    assert "CHOICE:" in system
    assert "WHY:" in system


def test_the_timeout_is_passed_to_the_call() -> None:
    """Without it a hung call blocks the match past every clock the run set."""
    client = _FakeClient()
    _seat(client).decide(_request(), timeout=42)
    assert client.calls[0]["timeout"] == 42
