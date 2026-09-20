"""An example seat driver, and the integration test the harness needs anyway.

Plays a whole game through the same control socket an agent uses, with a policy
simple enough to read in one sitting. It exists for two reasons.

It shows what an agent loop looks like without the CLI in the way. And it plays
a full game unattended, which is the only way to find the bugs that only appear
on turn forty.

    python examples/heuristic_agent.py --match 0920-0003-7dbe --seat A
"""

from __future__ import annotations

import argparse
import sys
import time

from gauntlet.server import call_match

#: Words in an option label that suggest a play worth making early. Crude on
#: purpose, the point is to exercise the loop rather than to play well.
PREFERRED = ("play land", "ramp", "search your library")


def choose(request: dict) -> tuple[int, str]:
    """Pick an option. Returns the index and a reason for the transcript."""
    kind = request["kind"]
    options = request.get("options", [])

    if kind == "mulligan":
        # Keep any hand with two to five lands. Counting land options is not
        # available here, so count the word in the hand's rendered names.
        hand = request.get("state", {}).get("me", {}).get("hand", [])
        cards = request.get("cards", {})
        lands = sum(
            1 for slug in hand if "land" in (cards.get(slug, {}).get("type", "")).lower()
        )
        keep = 2 <= lands <= 5
        index = 1 if keep else 0
        return index, f"{lands} lands in seven, {'keeping' if keep else 'mulliganing'}"

    if kind in {"attack", "block"}:
        return 0, "taking Forge's proposed combat"

    # cast_or_pass. Prefer a land drop, then the most expensive thing offered,
    # on the theory that the biggest castable spell is usually the play.
    for opt in options:
        if any(word in opt["label"].lower() for word in PREFERRED):
            return opt["i"], "land drop first"

    real = [o for o in options if o["i"] != 0]
    if not real:
        return 0, "nothing to do"
    pick = max(real, key=lambda o: len(o.get("cost") or ""))
    return pick["i"], f"largest castable option, {pick['label']}"


def play(match: str, seat: str, *, timeout: float = 120.0, max_decisions: int = 5000) -> int:
    """Drive one seat until the match ends. Returns the number of decisions."""
    answered = 0
    pending: dict | None = None

    while answered < max_decisions:
        payload: dict = {"op": "act", "seat": seat, "timeout": timeout}
        if pending is not None:
            index, why = choose(pending)
            payload |= {"id": pending["id"], "choice": index, "why": why}

        try:
            reply = call_match(match, payload, timeout=timeout + 30)
        except (FileNotFoundError, ConnectionError) as exc:
            print(f"match gone: {exc}", file=sys.stderr)
            return answered

        if pending is not None:
            answered += 1

        status = reply.get("status")
        if status == "decide":
            pending = reply["request"]
        elif status == "game_over":
            print(f"game over after {answered} decisions, wins {reply.get('wins')}")
            return answered
        elif status == "waiting":
            pending = None
            # Nothing on the table. The other seat is thinking, or Forge is
            # resolving something. Come straight back rather than sleeping,
            # the call itself blocks.
            continue
        else:
            print(f"stopping: {reply}", file=sys.stderr)
            return answered

    print(f"hit the decision cap at {answered}", file=sys.stderr)
    return answered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match", required=True)
    parser.add_argument("--seat", required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    started = time.monotonic()
    count = play(args.match, args.seat, timeout=args.timeout)
    print(f"{count} decisions in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
