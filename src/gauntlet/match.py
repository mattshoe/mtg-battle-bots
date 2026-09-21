"""Putting a match together and running it.

The one place that knows the whole sequence: resolve decks, write them where
Forge can read them, open the sockets, start the JVM, serve decisions until it
exits, close the transcript.

Split out from the CLI so a script can run a match without going through argv,
which is what any batch of more than one game ends up wanting.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import decks as deckmod
from . import engine, paths
from .seats import Seat, build_seat
from .server import MatchResult, MatchServer
from .transcript import Transcript

#: Decision kinds routed to a seat unless a run says otherwise. Matches the
#: Java default. Trimming this is the main lever on how long a match takes.
DEFAULT_ROUTED: tuple[str, ...] = ("mulligan", "cast_or_pass", "attack", "block")


def new_match_id() -> str:
    """Short, sortable, and unique enough for a directory of them.

    Time first so a listing is in play order, random suffix so two matches
    started in the same second do not collide.
    """
    return time.strftime("%m%d-%H%M") + "-" + secrets.token_hex(2)


@dataclass(slots=True)
class SeatSpec:
    """How the caller asked for one seat to be set up."""

    seat: str
    deck: str
    controller: str = "forge"
    routed: tuple[str, ...] = DEFAULT_ROUTED
    #: Passed through to an api seat, ignored otherwise.
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MatchPlan:
    """A match, resolved and ready to run."""

    match_id: str
    specs: list[SeatSpec]
    decklists: dict[str, deckmod.DeckList]
    deck_paths: dict[str, Path]
    game_format: str = "Commander"
    seed: int | None = None
    games: int = 1
    decision_timeout: int = 300
    game_timeout: int = 900
    trace: bool = False

    @property
    def has_interactive(self) -> bool:
        return any(s.controller == "interactive" for s in self.specs)


def resolve_deck(ref: str, *, owner: str | None = None) -> deckmod.DeckList:
    """Find a deck by collection slug, collection name, or path to a file.

    A path wins over a name, because someone who typed a path meant it.
    """
    candidate = Path(ref).expanduser()
    if candidate.exists():
        if candidate.suffix.lower() == ".dck":
            # Already in Forge's own format. Parsing it into a DeckList and
            # writing it back out would only risk changing it.
            return deckmod.from_dck(candidate)
        return deckmod.from_text(candidate.read_text(encoding="utf-8"), candidate.stem)
    return deckmod.from_collection(ref, owner=owner)


def plan(
    specs: list[SeatSpec],
    *,
    owner: str | None = None,
    game_format: str = "Commander",
    seed: int | None = None,
    games: int = 1,
    decision_timeout: int = 300,
    game_timeout: int = 900,
    trace: bool = False,
    match_id: str | None = None,
) -> MatchPlan:
    """Resolve every deck and write the .dck files Forge will load."""
    match_id = match_id or new_match_id()
    decklists: dict[str, deckmod.DeckList] = {}
    deck_paths: dict[str, Path] = {}

    out_dir = paths.deck_cache() / match_id
    out_dir.mkdir(parents=True, exist_ok=True)

    for spec in specs:
        deck = resolve_deck(spec.deck, owner=owner)
        problems = deckmod.validate(deck)
        if problems:
            # Loud but not fatal. Forge will refuse a deck it cannot legally
            # build, and a deck that is one card short is still worth testing.
            # stderr, not stdout - a warning on stdout lands in the middle of
            # `--json` output and makes it unparseable.
            for p in problems:
                print(f"warning: seat {spec.seat} deck {deck.name}: {p}", file=sys.stderr)
        decklists[spec.seat] = deck
        deck_paths[spec.seat] = deckmod.write_dck(deck, out_dir / f"{spec.seat}.dck")

    return MatchPlan(
        match_id=match_id,
        specs=specs,
        decklists=decklists,
        deck_paths=deck_paths,
        game_format=game_format,
        seed=seed,
        games=games,
        decision_timeout=decision_timeout,
        game_timeout=game_timeout,
        trace=trace,
    )


def _bridge_revision() -> str:
    """Identify the bridge build so a transcript can be trusted later.

    The jar's mtime is a poor version but an honest one. Anything derived from
    git would lie the moment someone builds without committing.
    """
    try:
        jar = paths.bridge_jar()
    except FileNotFoundError:
        return "unbuilt"
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(jar.stat().st_mtime))


def build_seats(plan_: MatchPlan) -> dict[str, Seat]:
    seats: dict[str, Seat] = {}
    for spec in plan_.specs:
        if spec.controller == "forge":
            continue  # played inside the JVM, never reaches a Python seat
        options = dict(spec.options)
        if spec.controller in ("api", "sdk"):
            options.setdefault("deck_note", plan_.decklists[spec.seat].name)
        seats[spec.seat] = build_seat(spec.controller, **options)
    return seats


def run(plan_: MatchPlan, *, transcript: Transcript | None = None) -> MatchResult:
    """Run a planned match to completion. Blocks until Forge exits."""
    own_transcript = transcript is None
    transcript = transcript or Transcript()

    seats = build_seats(plan_)
    server = MatchServer(
        match_id=plan_.match_id,
        seats=seats,
        transcript=transcript,
        decision_timeout=float(plan_.decision_timeout),
    )
    endpoint, _ = server.bind()
    server.start()

    transcript.start_match(
        match_id=plan_.match_id,
        format=plan_.game_format,
        seed=plan_.seed,
        seats=[
            {
                "seat": s.seat,
                "deck_name": plan_.decklists[s.seat].name,
                "deck_source": plan_.decklists[s.seat].source,
                "controller": s.controller,
                "decklist": {
                    "commanders": list(plan_.decklists[s.seat].commanders),
                    "main": [list(x) for x in plan_.decklists[s.seat].main],
                },
            }
            for s in plan_.specs
        ],
        policy={
            "routed": {s.seat: list(s.routed) for s in plan_.specs},
            "decision_timeout": plan_.decision_timeout,
            "game_timeout": plan_.game_timeout,
            "games": plan_.games,
        },
        forge_version=paths.forge_version(),
        bridge_revision=_bridge_revision(),
    )

    cmd = engine.build_command(
        [
            engine.SeatConfig(
                seat=s.seat,
                deck_path=plan_.deck_paths[s.seat],
                bridge_endpoint=None if s.controller == "forge" else endpoint,
                routed_kinds=() if s.controller == "forge" else s.routed,
            )
            for s in plan_.specs
        ],
        game_format=plan_.game_format,
        seed=plan_.seed,
        games=plan_.games,
        decision_timeout=plan_.decision_timeout,
        game_timeout=plan_.game_timeout,
    )

    log_path = paths.state_dir() / f"{plan_.match_id}.forge.log"
    _write_meta(plan_, endpoint, cmd, log_path)

    def on_line(line: str) -> None:
        # Forge prints one result object per game. This is the only channel a
        # match with no bridged seat has, so it cannot be left to the bridges.
        if '"kind":"game_result"' not in line:
            return
        # A truncated line is possible while Forge is still writing, and the
        # next game's result will arrive intact anyway.
        with contextlib.suppress(ValueError):
            server.record_game_result(json.loads(line))

    forge = engine.launch(cmd, log_path, on_line=on_line, trace=plan_.trace)
    # Now that the process exists, give the server a way to end it. An
    # exhausted seat has to stop Forge, not just stop answering it.
    server.stop_engine = forge.stop
    status = "finished"
    try:
        code = forge.wait()
        if code not in (0, None):
            status = "crashed"
            server.result.crashed = True
            server.result.error = f"forge exited {code}, see {log_path}"
        if server.exhausted:
            # Not a crash, but the result is not trustworthy either. Say so
            # loudly enough that a caller cannot use the numbers by accident.
            status = "exhausted"
            server.result.exhausted = dict(server.exhausted)
            detail = "; ".join(f"{k}: {v}" for k, v in server.exhausted.items())
            server.result.error = f"seat ran out of capacity mid-run ({detail})"
    except KeyboardInterrupt:
        status = "crashed"
        server.result.error = "interrupted"
        forge.stop()
        raise
    finally:
        server.finished.set()
        server.shutdown()
        transcript.finish_match(match_id=plan_.match_id, status=status)
        paths.match_meta(plan_.match_id).unlink(missing_ok=True)
        if own_transcript:
            transcript.close()

    return server.result


def run_detached(plan_: MatchPlan) -> str:
    """Start a match in a background process and return its id.

    Interactive seats need the match to outlive the command that started it, so
    an agent can come back and answer. Anything else is better run in the
    foreground where its output is visible.
    """
    import sys

    cmd = [
        sys.executable,
        "-m",
        "gauntlet.daemon",
        paths.match_meta(plan_.match_id).with_suffix(".plan.json").as_posix(),
    ]
    _dump_plan(plan_)
    subprocess.Popen(
        cmd,
        stdout=(paths.state_dir() / f"{plan_.match_id}.daemon.log").open("w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(paths.REPO_ROOT / "src")},
    )

    # Wait for the control socket to appear rather than returning an id that
    # does not answer yet. An agent's first `act` would otherwise race the
    # daemon and fail for no reason the agent can act on.
    sock = paths.match_socket(plan_.match_id)
    for _ in range(300):
        if sock.exists():
            return plan_.match_id
        time.sleep(0.1)
    raise TimeoutError(f"match {plan_.match_id} did not come up, see {paths.state_dir()}")


def _dump_plan(plan_: MatchPlan) -> Path:
    path = paths.match_meta(plan_.match_id).with_suffix(".plan.json")
    path.write_text(
        json.dumps(
            {
                "match_id": plan_.match_id,
                "game_format": plan_.game_format,
                "seed": plan_.seed,
                "games": plan_.games,
                "decision_timeout": plan_.decision_timeout,
                "game_timeout": plan_.game_timeout,
                "trace": plan_.trace,
                "specs": [
                    {
                        "seat": s.seat,
                        "deck": s.deck,
                        "controller": s.controller,
                        "routed": list(s.routed),
                        "options": s.options,
                    }
                    for s in plan_.specs
                ],
                "deck_paths": {k: str(v) for k, v in plan_.deck_paths.items()},
                "decklists": {
                    k: {
                        "name": v.name,
                        "commanders": list(v.commanders),
                        "main": [list(x) for x in v.main],
                        "source": v.source,
                    }
                    for k, v in plan_.decklists.items()
                },
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    return path


def load_plan(path: Path) -> MatchPlan:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return MatchPlan(
        match_id=raw["match_id"],
        specs=[
            SeatSpec(
                seat=s["seat"],
                deck=s["deck"],
                controller=s["controller"],
                routed=tuple(s["routed"]),
                options=s.get("options", {}),
            )
            for s in raw["specs"]
        ],
        decklists={
            k: deckmod.DeckList(
                name=v["name"],
                commanders=tuple(v["commanders"]),
                main=tuple((int(q), n) for q, n in v["main"]),
                source=v["source"],
            )
            for k, v in raw["decklists"].items()
        },
        deck_paths={k: Path(v) for k, v in raw["deck_paths"].items()},
        game_format=raw["game_format"],
        seed=raw["seed"],
        games=raw["games"],
        decision_timeout=raw["decision_timeout"],
        game_timeout=raw["game_timeout"],
        trace=raw["trace"],
    )


def _write_meta(plan_: MatchPlan, endpoint: str, cmd: list[str], log_path: Path) -> None:
    """Leave enough on disk to work out what a stuck match is doing."""
    import shlex

    paths.match_meta(plan_.match_id).write_text(
        json.dumps(
            {
                "match": plan_.match_id,
                "pid": os.getpid(),
                "endpoint": endpoint,
                "socket": str(paths.match_socket(plan_.match_id)),
                "forge_log": str(log_path),
                "command": " ".join(shlex.quote(c) for c in cmd),
                "seats": {s.seat: s.controller for s in plan_.specs},
                "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            indent=1,
        ),
        encoding="utf-8",
    )


__all__ = [
    "DEFAULT_ROUTED",
    "MatchPlan",
    "MatchResult",
    "SeatSpec",
    "load_plan",
    "new_match_id",
    "plan",
    "resolve_deck",
    "run",
    "run_detached",
]
