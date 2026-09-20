"""Turning a decision request into something an agent reads, and back.

Shared by the API seat and by ``gauntlet act``, so a human reading a transcript
and a model answering a question are looking at the same thing. Two renderings
of the same state would eventually disagree, and the disagreement would be
invisible.

The format is plain text rather than the raw JSON. An agent given JSON spends
its attention parsing, and the board is easier to read laid out.
"""

from __future__ import annotations

import re
from typing import Any

from .protocol import ProtocolError, Request

_CHOICE = re.compile(r"^\s*CHOICE\s*:\s*(\d+)", re.MULTILINE | re.IGNORECASE)
# Stops at the next labelled line rather than running to the end of the reply.
# With DOTALL a WHY-first answer swallowed its own "CHOICE: 1" and put wire
# syntax into the transcript's reasoning column.
_WHY = re.compile(
    r"^\s*WHY\s*:\s*(.+?)(?=^\s*CHOICE\s*:|\Z)",
    re.MULTILINE | re.IGNORECASE | re.DOTALL,
)


def _card_line(slug: str, cards: dict[str, Any]) -> str:
    """One card as name plus cost, falling back to the slug.

    A slug with no entry means the seat was never sent its text, which happens
    when a card enters a zone the seat can see for the first time in the same
    message it is asked about. Showing the slug beats showing nothing.
    """
    detail = cards.get(slug)
    if not detail:
        return slug
    name = detail.get("name", slug)
    cost = detail.get("cost")
    return f"{name} {cost}" if cost else name


def _permanent_line(entry: dict[str, Any], cards: dict[str, Any]) -> str:
    # Name only. A permanent has already been paid for, printing its mana cost
    # next to it is noise on the line a seat reads most often.
    slug = entry.get("c", "?")
    name = (cards.get(slug) or {}).get("name", slug)
    bits = []
    if entry.get("pt"):
        bits.append(entry["pt"])
    if entry.get("tapped"):
        bits.append("tapped")
    if entry.get("sick"):
        bits.append("summoning sick")
    if entry.get("dmg"):
        bits.append(f"{entry['dmg']} damage")
    return f"{name} ({', '.join(bits)})" if bits else name


def render_state(state: dict[str, Any], cards: dict[str, Any]) -> str:
    """The board, as prose an agent can scan."""
    me = state.get("me", {})
    # Turn 0 with no phase is the pre-game window, where mulligans happen.
    # Rendering it as "Turn 0, ?, ? is the active player" invites a seat to
    # reason about a board that does not exist yet.
    turn = state.get("turn", 0)
    phase = state.get("phase") or "?"
    if not turn or phase == "?":
        header = "Before the game starts."
    else:
        header = f"Turn {turn}, {phase}, {state.get('active', '?')} is the active player."

    lines = [
        header,
        "",
        f"YOU ({state.get('you', '?')}) - {me.get('life', '?')} life, "
        f"{me.get('library', '?')} cards in library",
    ]

    hand = me.get("hand") or []
    lines.append(f"  Hand ({len(hand)}): " + (", ".join(_card_line(c, cards) for c in hand) or "-"))

    board = me.get("battlefield") or []
    lines.append(
        "  Battlefield: " + (", ".join(_permanent_line(p, cards) for p in board) or "empty")
    )

    for zone, label in (("command", "Command zone"), ("graveyard", "Graveyard")):
        entries = me.get(zone) or []
        if entries:
            lines.append(f"  {label}: " + ", ".join(_card_line(c, cards) for c in entries))

    for opp in state.get("opponents") or []:
        lines += [
            "",
            f"{opp.get('name', '?')} - {opp.get('life', '?')} life, "
            f"{opp.get('hand_size', '?')} cards in hand, {opp.get('library', '?')} in library",
        ]
        board = opp.get("battlefield") or []
        lines.append(
            "  Battlefield: " + (", ".join(_permanent_line(p, cards) for p in board) or "empty")
        )
        gy = opp.get("graveyard") or []
        if gy:
            lines.append("  Graveyard: " + ", ".join(_card_line(c, cards) for c in gy))
        cmd = opp.get("command") or []
        if cmd:
            lines.append("  Command zone: " + ", ".join(_card_line(c, cards) for c in cmd))
        dealt = opp.get("cmd_damage_to_me") or {}
        if dealt:
            got = ", ".join(f"{k} {v}" for k, v in dealt.items())
            lines.append(f"  Commander damage dealt to you: {got}")

    stack = state.get("stack") or []
    if stack:
        lines += ["", "Stack (top last): " + " | ".join(stack)]

    attackers = state.get("combat") or []
    if attackers:
        lines += ["", "In combat:"]
        for entry in attackers:
            name = (cards.get(entry.get("c", "")) or {}).get("name", entry.get("c", "?"))
            pt = f" {entry['pt']}" if entry.get("pt") else ""
            target = entry.get("attacking")
            line = f"  {name}{pt} attacking {target}" if target else f"  {name}{pt} attacking"
            blockers = entry.get("blocked_by") or []
            if blockers:
                named = ", ".join((cards.get(b) or {}).get("name", b) for b in blockers)
                line += f", blocked by {named}"
            lines.append(line)

    return "\n".join(lines)


def render_new_cards(new_cards: dict[str, Any]) -> str:
    """Oracle text for cards the seat has not been shown before."""
    if not new_cards:
        return ""
    out = ["Cards you have not seen yet this game:"]
    for detail in new_cards.values():
        name = detail.get("name", "?")
        cost = detail.get("cost", "")
        head = f"  {name} {cost}".rstrip()
        pt = detail.get("pt")
        type_line = detail.get("type", "")
        out.append(f"{head} - {type_line} {pt}".rstrip() if pt else f"{head} - {type_line}")
        text = (detail.get("text") or "").strip()
        if text:
            for line in text.splitlines():
                if line.strip():
                    out.append(f"      {line.strip()}")
    return "\n".join(out)


def render_options(request: Request) -> str:
    lines = ["Your options:"]
    for opt in request.options:
        label = opt.label
        if opt.cost:
            label = f"{label}  cost {opt.cost}"
        lines.append(f"  [{opt.index}] {label}")
    extra = request.extra.get("proposed")
    if extra:
        lines += ["", f"Forge proposes: {', '.join(str(x) for x in extra)}"]
    return "\n".join(lines)


def render_since(events: list[str]) -> str:
    """Forge's own account of what happened since this seat last acted.

    The board shows the result, this shows the cause. A removal spell that
    fizzled and a removal spell that killed something look identical in a board
    state that no longer contains the creature either way.
    """
    if not events:
        return ""
    return "\n".join(["Since your last decision:", *(f"  {e}" for e in events)])


def build_prompt(request: Request, cards: dict[str, Any], *, deck_note: str = "") -> str:
    """Everything an agent needs to answer one question."""
    parts = []
    if deck_note:
        parts.append(f"Your deck: {deck_note}")
    happened = render_since(request.extra.get("since") or [])
    if happened:
        parts.append(happened)
    parts.append(render_state(request.state, cards))
    fresh = render_new_cards(request.new_cards)
    if fresh:
        parts.append(fresh)
    parts.append(request.prompt)
    parts.append(render_options(request))
    return "\n\n".join(parts)


def parse_reply(text: str, request: Request) -> tuple[int, str]:
    """Pull a choice and a reason out of a model's answer.

    Deliberately forgiving about everything except the number. A model that
    wandered off format but named a legal option should still get to play, and
    the alternative is Forge quietly taking the turn.
    """
    match = _CHOICE.search(text)
    if match is None:
        # A bare number on its own line is common enough to accept.
        loose = re.search(r"^\s*\[?(\d+)\]?\s*$", text, re.MULTILINE)
        if loose is None:
            raise ProtocolError(f"no CHOICE in reply: {text[:200]!r}")
        match = loose

    choice = int(match.group(1))
    valid = {o.index for o in request.options}
    if valid and choice not in valid:
        raise ProtocolError(f"chose {choice}, options are {sorted(valid)}")

    why_match = _WHY.search(text)
    why = why_match.group(1).strip() if why_match else ""
    # One line is enough for a transcript. A model that wrote an essay gets
    # trimmed rather than filling the database with it.
    return choice, " ".join(why.split())[:500]
