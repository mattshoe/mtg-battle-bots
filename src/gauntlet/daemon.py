"""Runs one match in the background.

Only entry point is ``python -m gauntlet.daemon <plan.json>``, started by
``gauntlet run`` when a seat is interactive and the match has to outlive the
command that launched it.

Deliberately thin. Everything it does is in :mod:`gauntlet.match`, so a match
run in the foreground and a match run detached take the same path and cannot
drift apart.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .match import load_plan, run


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m gauntlet.daemon <plan.json>", file=sys.stderr)
        return 2

    plan_path = Path(args[0])
    plan = load_plan(plan_path)
    try:
        result = run(plan)
    finally:
        # The plan file is scaffolding. Leaving it behind makes a stale match
        # look live to anything listing the state directory.
        plan_path.unlink(missing_ok=True)

    print(f"match {result.match_id} finished, wins: {result.wins_by_seat()}")
    return 1 if result.crashed else 0


if __name__ == "__main__":
    raise SystemExit(main())
