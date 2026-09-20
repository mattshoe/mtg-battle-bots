"""Who answers for a player.

A seat is the only thing in the system that exercises judgment. Everything else
is bookkeeping, and the point of the design is to keep it that way.

Three implementations, interchangeable per player:

``ForgeSeat``
    Never reached. A player with no bridge is played by Forge's AI inside the
    JVM and never appears here. It exists as a name in configuration so a match
    can say who is playing what without a special case.

``InteractiveSeat``
    A rendezvous between the thread serving the engine and an agent calling
    ``gauntlet act``. The engine thread blocks on an answer, the agent blocks on
    a question, and the two hand off through this object.

``ApiSeat``
    Calls the Claude API and answers from the reply. Unattended, for when the
    question is statistical rather than specific.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .protocol import ProtocolError, Request, Response


class SeatTimeout(Exception):
    """Nobody answered in time. The bridge will let Forge decide instead."""


class Seat(ABC):
    """Answers decisions for one player."""

    #: What the transcript records as having played this seat.
    controller: str = "unknown"

    @abstractmethod
    def decide(self, request: Request, timeout: float) -> Response:
        """Answer, or raise :class:`SeatTimeout`.

        Called on the thread serving that player's bridge connection. It may
        block for the full timeout, and nothing else in the match waits on it -
        Forge is single-threaded per game, so the game is already stopped.
        """

    def close(self) -> None:  # noqa: B027 - a no-op default, not every seat holds anything
        """Release anything held. Safe to call more than once."""


class ForgeSeat(Seat):
    """A placeholder for a player Forge's own AI is running.

    No request ever reaches it, because a player with no bridge never opens one.
    Asking it to decide means the wiring is wrong, so it says so loudly rather
    than inventing an answer.
    """

    controller = "forge"

    def decide(self, request: Request, timeout: float) -> Response:
        raise RuntimeError(
            f"a forge seat was asked to decide {request.kind!r}; "
            "that player should not have been given a bridge"
        )


@dataclass(slots=True)
class _Outstanding:
    request: Request
    answered: threading.Event
    response: Response | None = None
    error: str = ""


class InteractiveSeat(Seat):
    """A seat an agent drains one decision at a time.

    The engine thread calls :meth:`decide` and blocks. The agent calls
    :meth:`take` to collect the question and :meth:`answer` to submit. Both
    sides can time out independently without corrupting the other.

    Taking a question does not consume it. An agent may call :meth:`take` as
    often as it likes and keep getting the same question until it answers, which
    is what makes a crashed or confused agent able to just ask again. An earlier
    version used a semaphore and the second look came back empty, which read as
    "nothing to do" at exactly the moment there was something to do.

    The other subtlety is a late answer. An agent that thinks past the engine's
    patience will submit for a decision the engine has already resolved by
    falling back. That answer is rejected, because applying it would attach one
    seat's reasoning to a play Forge actually made.
    """

    controller = "interactive"

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._outstanding: _Outstanding | None = None
        self._closed = False
        #: The last question id actually handed to the agent. An answer that
        #: does not name an id is taken to mean this one, and only this one.
        self._handed: int = 0

    # ------------------------------------------------------- engine side

    def decide(self, request: Request, timeout: float) -> Response:
        pending = _Outstanding(request=request, answered=threading.Event())
        with self._cond:
            if self._closed:
                raise SeatTimeout("seat closed")
            # An unanswered previous question can only mean the engine gave up
            # on it. Drop it rather than letting it shadow this one, and wake
            # whoever was waiting on it instead of leaving them parked for a
            # full timeout on a question nobody will ever answer.
            displaced = self._outstanding
            self._outstanding = pending
            self._cond.notify_all()
        if displaced is not None:
            displaced.error = "the engine moved on before this was answered"
            displaced.answered.set()

        if not pending.answered.wait(timeout):
            with self._cond:
                if self._outstanding is pending:
                    self._outstanding = None
            raise SeatTimeout(f"no answer for {request.kind} within {timeout:.0f}s")

        if pending.response is None:
            raise SeatTimeout(pending.error or "seat gave up")
        return pending.response

    # -------------------------------------------------------- agent side

    def take(self, timeout: float) -> Request | None:
        """The open question, waiting up to ``timeout`` for one to appear.

        Idempotent. Calling it twice without answering returns the same
        question both times.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._outstanding is None and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            if self._outstanding is None:
                return None
            self._handed = self._outstanding.request.id
            return self._outstanding.request

    def open_id(self) -> int | None:
        """The id an unqualified answer would be applied to, if it is safe.

        None when the question on the table is not the one this agent was last
        shown. That happens when its previous answer arrived too late, the
        engine fell back, and the game moved on. Answering the new question with
        the old question's choice would be a different play made for the wrong
        reasons, so the caller is made to look again instead.
        """
        with self._cond:
            if self._outstanding is None:
                return None
            open_id = self._outstanding.request.id
            return open_id if open_id == self._handed else None

    def answer(self, response: Response) -> None:
        """Submit an answer.

        Raises :class:`ProtocolError` if it does not fit the question on the
        table, so a confused agent finds out immediately instead of the engine
        silently discarding it.
        """
        with self._cond:
            pending = self._outstanding
            if pending is None:
                raise ProtocolError("nothing to answer")
            if pending.request.id != response.id:
                raise ProtocolError(
                    f"answered {response.id}, the open question is {pending.request.id}"
                )
            response.validate_against(pending.request)
            pending.response = response
            self._outstanding = None
            self._cond.notify_all()
        pending.answered.set()

    def current(self) -> Request | None:
        """The open question without waiting. For status output."""
        with self._cond:
            return self._outstanding.request if self._outstanding else None

    def close(self) -> None:
        with self._cond:
            self._closed = True
            pending = self._outstanding
            self._outstanding = None
            self._cond.notify_all()
        if pending is not None:
            pending.error = "match ended"
            pending.answered.set()


class ApiSeat(Seat):
    """A seat played by the Claude API.

    Imports the SDK lazily. Most runs never use this seat, and a hard dependency
    on an API client for a harness that can run entirely offline would be wrong.
    """

    controller = "api"

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-5",
        system: str | None = None,
        max_tokens: int = 700,
        deck_note: str = "",
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.deck_note = deck_note
        self.system = system or DEFAULT_SYSTEM
        self._client = None
        self._seen_cards: dict[str, dict] = {}

    def _ensure_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on install
                raise RuntimeError(
                    "the api seat needs the anthropic package: uv pip install anthropic"
                ) from exc
            self._client = anthropic.Anthropic()
        return self._client

    def decide(self, request: Request, timeout: float) -> Response:
        from .prompt import build_prompt, parse_reply

        client = self._ensure_client()
        self._seen_cards.update(request.new_cards)
        prompt = build_prompt(request, self._seen_cards, deck_note=self.deck_note)

        try:
            message = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self.system,
                messages=[{"role": "user", "content": prompt}],
                timeout=timeout,
            )
        except Exception as exc:
            raise SeatTimeout(f"api call failed: {exc}") from exc

        text = "".join(block.text for block in message.content if block.type == "text")
        try:
            choice, why = parse_reply(text, request)
        except ProtocolError as exc:
            raise SeatTimeout(f"unusable reply: {exc}") from exc
        return Response(id=request.id, choice=choice, why=why)


DEFAULT_SYSTEM = """You are playing a game of Magic: The Gathering.

You will be given the board state and a numbered list of legal options. Pick
one. The rules engine has already filtered the list, so every option is legal
and payable, and you do not need to check that.

Answer in exactly this form and nothing else:

CHOICE: <number>
WHY: <one or two sentences on what you are playing for>

The reasoning is read later by someone trying to work out why the deck won or
lost, so say what you are playing for, not what the card does."""


def build_seat(kind: str, **kwargs) -> Seat:
    """Make a seat from its configuration name."""
    match kind:
        case "forge":
            return ForgeSeat()
        case "interactive":
            return InteractiveSeat()
        case "api":
            return ApiSeat(**kwargs)
        case _:
            raise ValueError(f"unknown seat kind {kind!r}, expected forge, interactive or api")


__all__ = [
    "ApiSeat",
    "ForgeSeat",
    "InteractiveSeat",
    "Seat",
    "SeatTimeout",
    "build_seat",
]
