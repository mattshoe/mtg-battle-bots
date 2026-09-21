"""A seat played by a Claude Agent SDK session.

The point of this over :class:`gauntlet.seats.ApiSeat` is credentials. The API
seat needs an ``ANTHROPIC_API_KEY`` and bills separately. This one runs on
whatever auth Claude Code is already using, which means a long unattended run
costs nothing extra.

The tradeoff is latency. The SDK drives a full agent harness per call, so a
decision costs roughly ten seconds against one or two for a raw API request.
For a few hundred games overnight that is the right trade, for tens of thousands
it is not.

One session is kept open per seat for the life of the match. Reconnecting per
decision would add several seconds to each one.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
from typing import Any

from .prompt import build_prompt, parse_reply
from .protocol import ProtocolError, Request, Response
from .seats import Seat, SeatExhausted, SeatTimeout

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

#: Unusable replies in a row before a seat is called finished. Three is far
#: enough from a one-off formatting slip, and cheap enough that a genuinely
#: dead session costs three calls rather than a thousand.
MAX_CONSECUTIVE_FAILURES = 3

SYSTEM = """You are playing a game of Magic: The Gathering, to win.

You will be given the board, what has happened since your last decision, and a
numbered list of legal options. The rules engine has already filtered that list,
so every option is legal and payable and you do not need to check.

Answer in exactly this form and nothing else:

CHOICE: <number>
WHY: <one or two sentences on what you are playing for>

Play the deck's actual plan rather than taking the biggest thing on offer. The
reasoning is read afterwards by someone working out why the deck won or lost, so
say what you are playing for, not what the card does."""


#: A well-formed answer, which makes a reply a play rather than a failure.
_CHOICE_LINE = re.compile(r"^\s*CHOICE\s*:\s*\d+", re.MULTILINE | re.IGNORECASE)

#: Phrases that mean this seat is finished, not merely confused.
#:
#: Every one was seen in a real run. They are matched only against a reply that
#: carries no usable answer, because "rate limit" and "quota" appear in ordinary
#: Magic reasoning and an earlier version killed healthy runs on them.
_TERMINAL_PATTERNS = (
    "session limit",
    "usage limit",
    "rate limit",
    "rate_limit",
    "quota",
    "credit balance",
    "insufficient",
    "unable to respond",
    "cannot continue",
    "can't continue",
    "will not respond",
    "no further responses",
    "overloaded_error",
    "service unavailable",
    "authentication",
    "unauthorized",
)


def _terminal_reply(text: str) -> str:
    """Whether a reply means this seat can no longer play at all.

    Only consulted once a reply has failed to parse. A well-formed answer is
    never terminal no matter what words its reasoning contains, which is what
    stops "hold up Rate Limit to counter their draw spell" from ending a run
    that had hours of capacity left.
    """
    # A reply carrying a real answer is never terminal, whatever its reasoning
    # mentions. decide() only calls this after parsing failed, but a function
    # that is safe only because of where it is called breaks the first time
    # somebody calls it somewhere else.
    if _CHOICE_LINE.search(text):
        return ""

    stripped = text.strip()
    if not stripped:
        # Ambiguous alone. One empty reply is a blip, a run of them is a dead
        # session, and the consecutive-failure guard in decide() tells them
        # apart without this function having to guess.
        return ""

    lowered = stripped.lower()
    for marker in _TERMINAL_PATTERNS:
        if marker in lowered:
            return " ".join(stripped.split())[:200]
    return ""


class SdkSeat(Seat):
    """Answers decisions through a persistent Claude Agent SDK session.

    Not thread safe by design. One seat belongs to one player, and Forge asks
    that player one question at a time.
    """

    controller = "sdk"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        deck_note: str = "",
        system: str | None = None,
    ) -> None:
        self.model = model
        self.deck_note = deck_note
        self.system = system or SYSTEM

        self._seen_cards: dict[str, Any] = {}
        self._client = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False
        #: Distinct from _closed. A seat closed at end of match is fine to
        #: reuse conceptually, one that ran out of capacity is not, and the
        #: two used to share a flag so close() skipped its own cleanup.
        self._exhausted = False
        #: Released by close(), so shutdown can tell whether it ran.
        self._released = False
        #: Unusable replies in a row. No phrase list can name every way a
        #: session dies, and the one that got through said only "unable to
        #: respond right now". A seat that cannot produce a usable answer
        #: several times running is finished whatever it said.
        self._consecutive_failures = 0
        #: The Agent SDK does not report token usage, so the budget falls
        #: back to its measured per-decision estimate. None means estimate.
        self.last_usage: tuple[int, int] | None = None

    # The SDK is async and Forge's bridge thread is not, so the session lives on
    # a dedicated event loop in its own thread and decisions are handed to it.
    # Opening a fresh loop per decision would throw away the session.

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        ready = threading.Event()

        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            loop.run_forever()

        self._thread = threading.Thread(target=run, name="gauntlet-sdk", daemon=True)
        self._thread.start()
        ready.wait(10)
        if self._loop is None:
            raise SeatTimeout("could not start the SDK event loop")
        return self._loop

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
        except ImportError as exc:  # pragma: no cover - depends on install
            raise SeatTimeout(
                "the sdk seat needs claude-agent-sdk: uv pip install claude-agent-sdk"
            ) from exc

        options = ClaudeAgentOptions(
            system_prompt=self.system,
            model=self.model,
            # No tools. The seat's only job is to answer the question in front
            # of it, and a harness that can read files or search the web would
            # both slow every decision down and let it see things a player
            # cannot.
            allowed_tools=[],
            max_turns=1,
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.__aenter__()
        return self._client

    async def _ask(self, prompt: str) -> str:
        client = await self._ensure_client()
        await client.query(prompt)
        chunks: list[str] = []
        async for message in client.receive_response():
            for block in getattr(message, "content", None) or []:
                text = getattr(block, "text", None)
                if text:
                    chunks.append(text)
        return "".join(chunks)

    def decide(self, request: Request, timeout: float) -> Response:
        with self._lock:
            if self._closed:
                if self._exhausted:
                    raise SeatExhausted("seat ran out of capacity earlier")
                raise SeatTimeout("seat is closed")
            loop = self._ensure_loop()

        self._seen_cards.update(request.new_cards)
        prompt = build_prompt(request, self._seen_cards, deck_note=self.deck_note)

        future = asyncio.run_coroutine_threadsafe(self._ask(prompt), loop)
        try:
            text = future.result(timeout=timeout)
        except TimeoutError as exc:
            future.cancel()
            raise SeatTimeout(f"sdk did not answer within {timeout:.0f}s") from exc
        except Exception as exc:
            raise SeatTimeout(f"sdk call failed: {type(exc).__name__}: {exc}") from exc

        try:
            choice, why = parse_reply(text, request)
        except ProtocolError as exc:
            with self._lock:
                self._consecutive_failures += 1
                run_length = self._consecutive_failures
            # Only now ask whether this was a dead session rather than a badly
            # formatted answer. Checking first meant a perfectly good reply
            # whose reasoning mentioned a rate limit ended the run.
            blocker = _terminal_reply(text)
            if not blocker and run_length >= MAX_CONSECUTIVE_FAILURES:
                blocker = (
                    f"{run_length} unusable replies in a row, last was "
                    f"{' '.join(text.split())[:120]!r}"
                )
            if blocker:
                # Closed so the next decision fails immediately rather than
                # paying a round trip to be told the same thing.
                with self._lock:
                    self._closed = True
                    self._exhausted = True
                raise SeatExhausted(blocker) from exc
            raise SeatTimeout(f"unusable reply: {exc}") from exc

        with self._lock:
            self._consecutive_failures = 0
        return Response(id=request.id, choice=choice, why=why)

    def close(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
            self._closed = True
            loop, client = self._loop, self._client

        if loop is not None and client is not None:
            # A session that will not close cleanly is not worth failing a
            # finished match over. The loop stops either way.
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(client.__aexit__(None, None, None), loop).result(
                    timeout=15
                )
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
