"""The wire format between the Forge bridge and whoever holds a seat.

Both sides of this are load-bearing for a long time, so the rules are strict and
few:

* One line of JSON per message, UTF-8, no embedded newlines.
* Every message carries ``v``. A peer that sees a ``v`` it does not know refuses
  the message rather than guessing.
* ``id`` is a positive integer for a request that wants an answer, and ``0`` for
  a notification that does not. A response always echoes the ``id`` it answers.
* An answer that never arrives, arrives late, or does not parse is not an error.
  The bridge decides for itself and records that it had to.

Adding a field is a compatible change. Removing one, renaming one, or changing
what a value means is not, and bumps ``VERSION``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

VERSION = 1


class ProtocolError(Exception):
    """A message that cannot be trusted enough to act on."""


# Decision kinds the bridge can route outward. A seat may be configured to
# receive any subset; anything not routed is answered by Forge's own AI.
#
# Keep in sync with PlayerControllerGauntlet. A kind named here that the Java
# side does not raise is harmless. A kind the Java side raises that is not named
# here is a bug, and `unknown_kinds` in the transcript will show it.
KINDS: frozenset[str] = frozenset(
    {
        "mulligan",  # keep this opening hand, or take another
        "cast_or_pass",  # play a spell or ability, or pass priority
        "attack",  # declare attackers
        "block",  # declare blockers
    }
)

# Messages that flow the other way and expect no answer.
NOTIFICATIONS: frozenset[str] = frozenset({"game_over", "game_result"})


@dataclass(frozen=True, slots=True)
class Option:
    """One thing a seat may choose. ``index`` is what goes back on the wire."""

    index: int
    label: str
    card: str | None = None
    cost: str | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Option:
        return cls(
            index=int(raw["i"]),
            label=str(raw.get("label", "")),
            card=raw.get("card"),
            cost=raw.get("cost"),
        )


@dataclass(frozen=True, slots=True)
class Request:
    """A question the engine is blocking on."""

    id: int
    seat: str
    kind: str
    prompt: str
    options: tuple[Option, ...]
    state: dict[str, Any]
    new_cards: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_answer(self) -> bool:
        return self.id > 0

    @classmethod
    def parse(cls, line: str | bytes) -> Request:
        try:
            raw = json.loads(line)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"not JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ProtocolError("message was not an object")

        version = raw.get("v")
        if version != VERSION:
            raise ProtocolError(f"unsupported protocol version {version!r}, expected {VERSION}")

        kind = raw.get("kind")
        if not isinstance(kind, str):
            raise ProtocolError("message has no kind")

        known = {"v", "id", "seat", "kind", "prompt", "options", "state", "new_cards"}
        return cls(
            id=int(raw.get("id", 0)),
            seat=str(raw.get("seat", "")),
            kind=kind,
            prompt=str(raw.get("prompt", "")),
            options=tuple(Option.parse(o) for o in raw.get("options", [])),
            state=raw.get("state") or {},
            new_cards=raw.get("new_cards") or {},
            # Anything the Java side added that this version does not model.
            # Kept rather than dropped so a transcript stays complete across an
            # upgrade on one side only.
            extra={k: v for k, v in raw.items() if k not in known},
        )


@dataclass(frozen=True, slots=True)
class Response:
    """A seat's answer. ``choice`` indexes into the request's options."""

    id: int
    choice: int
    why: str = ""

    def encode(self) -> bytes:
        return (
            json.dumps(
                {"v": VERSION, "id": self.id, "choice": self.choice, "why": self.why},
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")

    def validate_against(self, request: Request) -> None:
        """Rejects an answer the engine could not act on.

        Catching this here rather than in Java keeps the failure legible: the
        transcript records which seat sent what, instead of the bridge quietly
        falling back and the run looking like the AI played it.
        """
        if self.id != request.id:
            raise ProtocolError(f"answered {self.id}, was asked {request.id}")
        if not request.options:
            return
        valid = {o.index for o in request.options}
        if self.choice not in valid:
            raise ProtocolError(
                f"choice {self.choice} is not one of {sorted(valid)} for {request.kind}"
            )
