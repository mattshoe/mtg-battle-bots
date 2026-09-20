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
import threading
from typing import Any

from .prompt import build_prompt, parse_reply
from .protocol import ProtocolError, Request, Response
from .seats import Seat, SeatExhausted, SeatTimeout

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

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


#: Replies that mean the seat is done for good rather than confused about one
#: question. Every one of these was observed in a real run that then spent two
#: and a half hours falling back to Forge on every single decision and
#: reporting the result as though agents had played it.
_TERMINAL_MARKERS = (
    "session limit",
    "usage limit",
    "rate limit",
    "quota",
    "will not respond",
    "no further responses",
)


def _terminal_reply(text: str) -> str:
    """Whether a reply means this seat can no longer play at all."""
    lowered = text.lower()
    for marker in _TERMINAL_MARKERS:
        if marker in lowered:
            return " ".join(text.split())[:200]
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
                raise SeatExhausted("seat is closed, it ran out of capacity earlier")
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

        blocker = _terminal_reply(text)
        if blocker:
            # Mark the seat closed so the next decision fails immediately
            # instead of paying the round trip to be told the same thing.
            with self._lock:
                self._closed = True
            raise SeatExhausted(blocker)

        try:
            choice, why = parse_reply(text, request)
        except ProtocolError as exc:
            raise SeatTimeout(f"unusable reply: {exc}") from exc
        return Response(id=request.id, choice=choice, why=why)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, client = self._loop, self._client

        if loop is not None and client is not None:
            # A session that will not close cleanly is not worth failing a
            # finished match over. The loop stops either way.
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(
                    client.__aexit__(None, None, None), loop
                ).result(timeout=15)
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
