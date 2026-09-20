"""The command line.

Two audiences, and they want opposite things.

A person wants readable output and sensible defaults. An agent wants one
command per decision, machine-readable, with no state to carry between calls.
Commands meant for agents take ``--json`` and say everything the next call needs
in their output.
"""

from __future__ import annotations

import dataclasses
import json as jsonlib
import sys
from pathlib import Path
from typing import Annotated

import typer

from . import decks as deckmod
from . import match as matchmod
from . import paths, prompt, sweep, transcript
from .server import call_match

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Play Magic: The Gathering games between agents, with a transcript.",
)


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _emit(payload: dict, as_json: bool, human: str) -> None:
    if as_json:
        typer.echo(jsonlib.dumps(payload, ensure_ascii=False, indent=1))
    else:
        typer.echo(human)


# --------------------------------------------------------------------- decks


@app.command("decks")
def list_decks(
    owner: Annotated[str | None, typer.Option(help="Only this owner's decks.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable.")] = False,
) -> None:
    """List the decks available from the collection database."""
    try:
        rows = deckmod.list_collection_decks(owner=owner)
    except Exception as exc:
        _fail(f"could not read the collection: {exc}")
        return

    if as_json:
        # DeckRow uses slots, so it has no __dict__ to read.
        typer.echo(
            jsonlib.dumps([dataclasses.asdict(r) for r in rows], ensure_ascii=False, indent=1)
        )
        return
    if not rows:
        typer.echo("no decks found")
        return
    width = max(len(r.slug) for r in rows)
    for r in rows:
        typer.echo(f"{r.slug:<{width}}  {r.owner:<6} {r.card_count:>4}  {r.commander}")


@app.command("export")
def export_deck(
    deck: Annotated[str, typer.Argument(help="Collection slug, name, or path to a text list.")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Where to write the .dck.")],
    owner: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Write one deck out as a Forge .dck, without playing anything."""
    try:
        resolved = matchmod.resolve_deck(deck, owner=owner)
    except Exception as exc:
        _fail(str(exc))
        return
    for problem in deckmod.validate(resolved):
        typer.secho(f"warning: {problem}", fg=typer.colors.YELLOW, err=True)
    written = deckmod.write_dck(resolved, out)
    typer.echo(f"{written}  ({resolved.size} cards)")


# --------------------------------------------------------------------- match


@app.command("run")
def run_match(
    a: Annotated[str, typer.Option("--a", help="Seat A deck: slug, name, or file.")],
    b: Annotated[str, typer.Option("--b", help="Seat B deck.")],
    seat_a: Annotated[str, typer.Option(help="forge, sdk, api, or interactive.")] = "interactive",
    seat_b: Annotated[str, typer.Option(help="forge, sdk, api, or interactive.")] = "interactive",
    games: Annotated[int, typer.Option(help="Games to play.")] = 1,
    seed: Annotated[int | None, typer.Option(help="RNG seed, for a reproducible game.")] = None,
    game_format: Annotated[str, typer.Option("--format", help="Forge game type.")] = "Commander",
    routed: Annotated[
        str,
        typer.Option(
            help=(
                "Decision kinds sent to a seat, comma separated. Fewer is faster. "
                "Prefix with SEAT= to set one seat, e.g. 'A=attack,block'."
            )
        ),
    ] = ",".join(matchmod.DEFAULT_ROUTED),
    decision_timeout: Annotated[int, typer.Option(help="Seconds a seat may think.")] = 300,
    game_timeout: Annotated[int, typer.Option(help="Seconds before a draw is called.")] = 900,
    model: Annotated[str, typer.Option(help="Model for api seats.")] = "claude-sonnet-5",
    owner: Annotated[str | None, typer.Option(help="Collection owner for deck lookup.")] = None,
    trace: Annotated[bool, typer.Option(help="Log every decision point Forge reaches.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Start a match.

    A match with an interactive seat is detached, because the agent holding that
    seat has to come back to it with `gauntlet act`. Anything else runs in the
    foreground where you can watch the result.
    """
    try:
        per_seat = _parse_routed(routed)
    except ValueError as exc:
        _fail(str(exc))
        return

    def seat_opts(kind: str) -> dict:
        return {"model": model} if kind in ("api", "sdk") else {}

    specs = [
        matchmod.SeatSpec(
            seat="A",
            deck=a,
            controller=seat_a,
            routed=per_seat.get("A", per_seat["*"]),
            options=seat_opts(seat_a),
        ),
        matchmod.SeatSpec(
            seat="B",
            deck=b,
            controller=seat_b,
            routed=per_seat.get("B", per_seat["*"]),
            options=seat_opts(seat_b),
        ),
    ]

    try:
        planned = matchmod.plan(
            specs,
            owner=owner,
            game_format=game_format,
            seed=seed,
            games=games,
            decision_timeout=decision_timeout,
            game_timeout=game_timeout,
            trace=trace,
        )
    except Exception as exc:
        _fail(str(exc))
        return

    if planned.has_interactive:
        match_id = matchmod.run_detached(planned)
        # Name every interactive seat, not just the first. A two-agent match
        # where only one seat was told to start sits there until it times out.
        loops = "\n".join(
            f"  gauntlet act --match {match_id} --seat {s.seat}"
            for s in specs
            if s.controller == "interactive"
        )
        _emit(
            {
                "match": match_id,
                "detached": True,
                "seats": {s.seat: s.controller for s in specs},
                "decision_timeout": decision_timeout,
            },
            as_json,
            f"match {match_id} started\n"
            f"  seat A  {seat_a:<12} {planned.decklists['A'].name}\n"
            f"  seat B  {seat_b:<12} {planned.decklists['B'].name}\n\n"
            f"a seat has {decision_timeout}s to answer before Forge decides for it.\n"
            f"every interactive seat needs its own loop, starting now:\n{loops}",
        )
        return

    result = matchmod.run(planned)
    _emit(
        {
            "match": result.match_id,
            "games": result.games,
            "wins": result.wins_by_seat(),
            "crashed": result.crashed,
            "error": result.error,
        },
        as_json,
        f"match {result.match_id}: {result.wins_by_seat() or 'no decisive games'}"
        + (f"\n{result.error}" if result.error else "")
        + f"\n\ngauntlet replay {result.match_id}",
    )


@app.command("act")
def act(
    match: Annotated[str, typer.Option(help="Match id.")],
    seat: Annotated[str, typer.Option(help="Which seat you are playing.")],
    choice: Annotated[int | None, typer.Option(help="Your answer to the open question.")] = None,
    why: Annotated[str, typer.Option(help="One sentence on what you are playing for.")] = "",
    decision_id: Annotated[
        int | None, typer.Option("--id", help="The id you are answering. Defaults to the open one.")
    ] = None,
    timeout: Annotated[float, typer.Option(help="Seconds to wait for the next question.")] = 120.0,
    as_json: Annotated[bool, typer.Option("--json", help="Raw request instead of prose.")] = False,
) -> None:
    """Submit a decision and wait for the next one.

    Call it with no --choice to pick up the question already on the table. Then
    keep calling it with the answer to the last question, and it hands you the
    next. It blocks rather than spinning, so an idle seat costs one call.
    """
    payload: dict = {"op": "act", "seat": seat, "timeout": timeout}
    if choice is not None:
        if not why.strip():
            # Not fatal, but the reasoning is the thing a transcript is for. A
            # run of blank answers produces a play-by-play that says what
            # happened and nothing about why, which is the same as no data.
            typer.secho(
                "warning: no --why given, this decision will have no reasoning in the transcript",
                fg=typer.colors.YELLOW,
                err=True,
            )
        # The id is left out deliberately when the caller did not give one. The
        # daemon knows which question it last showed this seat, and resolving it
        # there is what stops a late answer landing on a different question.
        # Same 500 character limit the model path applies in parse_reply.
        # One column, one limit.
        payload |= {"choice": choice, "why": " ".join(why.split())[:500]}
        if decision_id is not None:
            payload["id"] = decision_id

    try:
        reply = call_match(match, payload, timeout=timeout + 30)
    except (FileNotFoundError, ConnectionError) as exc:
        _fail(str(exc))
        return

    status = reply.get("status")
    if status == "error":
        _fail(reply.get("error", "unknown error"))
    elif status == "stale":
        _fail(reply.get("error", "the question you answered is no longer open"))
    elif status == "decide":
        req = reply["request"]
        if as_json:
            typer.echo(jsonlib.dumps(reply, ensure_ascii=False, indent=1))
        else:
            typer.echo(_render_decision(req))
    elif status == "game_over":
        _emit(reply, as_json, f"match over. wins: {reply.get('wins', {})}")
    else:
        _emit(
            reply,
            as_json,
            f"nothing to decide yet after {timeout:.0f}s. The other seat is thinking, "
            "or Forge is resolving. Call again.",
        )


def _render_decision(req: dict) -> str:
    """The question as an agent should read it."""
    from .protocol import Option, Request

    # The daemon splits these for us. `cards` names everything on the board so
    # this call can render it, `new_cards` carries rules text for what this seat
    # has not been shown before. Each `act` is a fresh process, so it cannot
    # work either of those out for itself.
    cards = req.get("cards") or {}
    parsed = Request(
        id=req["id"],
        seat=req["seat"],
        kind=req["kind"],
        prompt=req["prompt"],
        options=tuple(Option.parse(o) for o in req.get("options", [])),
        state=req.get("state") or {},
        new_cards=req.get("new_cards") or {},
        extra={k: v for k, v in req.items() if k in ("proposed", "since")},
    )
    body = prompt.build_prompt(parsed, cards)
    return f"decision {parsed.id} ({parsed.kind})\n\n{body}"


@app.command("status")
def status(
    match: Annotated[str, typer.Option(help="Match id.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """What a running match is doing."""
    try:
        reply = call_match(match, {"op": "status"}, timeout=10)
    except (FileNotFoundError, ConnectionError) as exc:
        _fail(str(exc))
        return
    _emit(reply, as_json, _human_status(reply))


@app.command("stop")
def stop(match: Annotated[str, typer.Option(help="Match id.")]) -> None:
    """End a running match early."""
    try:
        call_match(match, {"op": "stop"}, timeout=10)
    except (FileNotFoundError, ConnectionError) as exc:
        _fail(str(exc))
        return
    typer.echo(f"stopped {match}")


@app.command("sweep")
def sweep_cmd(
    deck: Annotated[str, typer.Option("--deck", help="The deck under test. Always seat A.")],
    against: Annotated[
        str, typer.Option(help="Comma separated opponents, or 'all' for every other deck.")
    ] = "all",
    games: Annotated[int, typer.Option(help="Games per opponent.")] = 3,
    seed: Annotated[int | None, typer.Option(help="Seed, so the sweep reproduces.")] = None,
    workers: Annotated[int | None, typer.Option(help="Parallel JVMs. Each holds ~1 GB.")] = None,
    owner: Annotated[str | None, typer.Option(help="Collection owner.")] = None,
    game_format: Annotated[str, typer.Option("--format")] = "Commander",
    game_timeout: Annotated[int, typer.Option(help="Seconds before a draw is called.")] = 900,
    seat_a: Annotated[str, typer.Option(help="Who plays the deck under test.")] = "forge",
    seat_b: Annotated[str, typer.Option(help="Who plays each opponent.")] = "forge",
    decision_timeout: Annotated[int, typer.Option(help="Seconds a seat may think.")] = 300,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Play one deck against a field of opponents and report win rates.

    Forge AI on both sides by default, which is fast and free. Pass
    `--seat-a sdk --seat-b sdk` to have agents play instead, which is far better
    Magic and roughly a hundred times slower.
    """
    if against.strip().lower() == "all":
        try:
            rows = deckmod.list_collection_decks(owner=owner)
        except Exception as exc:
            _fail(f"could not read the collection: {exc}")
            return
        opponents = [r.slug for r in rows if r.slug != deck]
    else:
        opponents = [o.strip() for o in against.split(",") if o.strip()]

    if not opponents:
        _fail("no opponents to play against")
        return

    if not as_json:
        typer.echo(
            f"{deck} against {len(opponents)} decks, {games} games each "
            f"({len(opponents) * games} total)"
        )

    def progress(pairing, done: int, total: int) -> None:
        if as_json:
            return
        outcome = pairing.error or f"{pairing.wins}-{pairing.losses}-{pairing.draws}"
        typer.echo(f"  [{done}/{total}] {pairing.opponent}  {outcome}", err=True)

    results, elapsed = sweep.timed_sweep(
        deck,
        opponents,
        games=games,
        seed=seed,
        workers=workers,
        owner=owner,
        game_format=game_format,
        game_timeout=game_timeout,
        seat_deck=seat_a,
        seat_opponent=seat_b,
        decision_timeout=decision_timeout,
        on_done=progress,
    )

    if as_json:
        typer.echo(
            jsonlib.dumps(
                {
                    "deck": deck,
                    "elapsed_s": round(elapsed, 1),
                    # A void run must be detectable without reading prose.
                    "valid": not any(p.exhausted for p in results),
                    "exhausted": [p.opponent for p in results if p.exhausted],
                    "pairings": [
                        {
                            "opponent": p.opponent,
                            "wins": p.wins,
                            "losses": p.losses,
                            "draws": p.draws,
                            "win_rate": round(p.win_rate, 3),
                            "median_turns": p.median_turns,
                            "match": p.match_id,
                            "error": p.error,
                            "exhausted": p.exhausted,
                        }
                        for p in results
                    ],
                },
                indent=1,
            )
        )
    else:
        typer.echo("")
        typer.echo(sweep.format_table(deck, results, elapsed))


# ---------------------------------------------------------------- transcript


@app.command("matches")
def matches(limit: Annotated[int, typer.Option()] = 20) -> None:
    """Recent matches."""
    for row in transcript.list_matches(limit=limit):
        typer.echo(
            f"{row.get('id')}  {row.get('status', '?'):<9} {row.get('format', '?'):<10} "
            f"{row.get('started_at', '')}"
        )


@app.command("replay")
def replay(
    match: Annotated[str, typer.Argument(help="Match id.")],
    verbose: Annotated[bool, typer.Option("-v", help="Include options and full state.")] = False,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Write to a file.")] = None,
) -> None:
    """Render a match as a readable play-by-play."""
    text = transcript.render(match, verbose=verbose)
    if out is not None:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"{out}")
    else:
        typer.echo(text)


@app.command("summary")
def summary(match: Annotated[str, typer.Argument(help="Match id.")]) -> None:
    """Counts and timings for one match."""
    typer.echo(jsonlib.dumps(transcript.summarise(match), indent=1, ensure_ascii=False))


# -------------------------------------------------------------------- checks


@app.command("doctor")
def doctor() -> None:
    """Check the install before blaming the harness."""
    ok = True

    def check(label: str, fn) -> None:
        nonlocal ok
        try:
            typer.echo(f"  ok    {label}: {fn()}")
        except Exception as exc:
            ok = False
            typer.secho(f"  FAIL  {label}: {exc}", fg=typer.colors.RED)

    typer.echo("forge")
    check("install", lambda: paths.vendor_dir())
    check("jar", lambda: paths.forge_jar().name)
    check("gson", lambda: paths.gson_jar().name)
    check("card data", lambda: f"{len(list((paths.vendor_dir() / 'res').iterdir()))} resource dirs")

    typer.echo("bridge")
    check("jar", lambda: paths.bridge_jar().name)

    typer.echo("java")

    def java_version() -> str:
        import subprocess

        out = subprocess.run(
            ["java", "-version"], capture_output=True, text=True, check=True
        ).stderr
        return out.splitlines()[0]

    check("runtime", java_version)

    typer.echo("collection")
    check("decks", lambda: f"{len(deckmod.list_collection_decks())} decks")

    typer.echo("storage")
    check("transcripts", lambda: paths.transcripts_db())
    check("state", lambda: paths.state_dir())

    if not ok:
        raise typer.Exit(1)


def main() -> None:  # pragma: no cover - console script shim
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())


def _human_status(reply: dict) -> str:
    """A running match's state in a line or two, rather than raw JSON."""
    seats = ", ".join(f"{k}={v}" for k, v in (reply.get("seats") or {}).items())
    games = reply.get("games") or []
    wins = reply.get("wins") or {}
    state = "finished" if reply.get("finished") else "running"
    lines = [f"{reply.get('match', '?')}  {state}  {seats}"]
    if games:
        lines.append(f"{len(games)} game(s) played, wins {wins or 'none yet'}")
    else:
        lines.append("no games finished yet")
    return "\n".join(lines)


def _parse_routed(spec: str) -> dict[str, tuple[str, ...]]:
    """Parse --routed into per-seat kind tuples.

    Two forms. A bare list applies to every seat, and `SEAT=list` entries
    separated by spaces or semicolons override individual seats. The wire and
    the Java side have always been per-seat, only the CLI was not.

    A typo routes nothing and looks like a slow agent hours later, so unknown
    kinds are rejected here rather than discovered in a transcript.
    """
    from .protocol import KINDS

    out: dict[str, tuple[str, ...]] = {}
    default: tuple[str, ...] | None = None

    for chunk in spec.replace(";", " ").split():
        seat, sep, rest = chunk.partition("=")
        kinds = tuple(k.strip() for k in (rest if sep else chunk).split(",") if k.strip())
        unknown = [k for k in kinds if k not in KINDS]
        if unknown:
            raise ValueError(f"unknown decision kind(s) {unknown}, known kinds are {sorted(KINDS)}")
        if sep:
            out[seat.strip().upper()] = kinds
        else:
            default = kinds

    out["*"] = default if default is not None else tuple(matchmod.DEFAULT_ROUTED)
    return out
