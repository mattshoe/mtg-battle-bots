"""Match recording and play-by-play rendering.

A match writes one row per decision as it happens, so a run that crashes on turn
nine still has turns one through eight on disk. The reasoning strings are what
this file exists for. A win-rate says a deck lost, the transcript says why.

One SQLite file holds every match. Reads open their own connection, the writer
holds one connection behind a lock because the socket thread calls it while a
game is running.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .protocol import KINDS, VERSION, Request, Response

__all__ = ["Transcript", "default_db_path", "list_matches", "render", "summarise"]


# Bumped when the shape of a stored row changes in a way a reader must know
# about. The table definitions are additive so far, so this has never moved.
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    id               TEXT PRIMARY KEY,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    format           TEXT NOT NULL DEFAULT '',
    seed             TEXT,
    forge_version    TEXT NOT NULL DEFAULT '',
    bridge_revision  TEXT NOT NULL DEFAULT '',
    policy           TEXT NOT NULL DEFAULT '{}',
    protocol_version INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS seats (
    match_id    TEXT NOT NULL,
    seat        TEXT NOT NULL,
    deck_name   TEXT NOT NULL DEFAULT '',
    deck_source TEXT NOT NULL DEFAULT '',
    controller  TEXT NOT NULL DEFAULT '',
    decklist    TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (match_id, seat)
);

CREATE TABLE IF NOT EXISTS decisions (
    match_id        TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    game_no         INTEGER NOT NULL DEFAULT 1,
    request_id      INTEGER NOT NULL DEFAULT 0,
    seat            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    turn            INTEGER,
    phase           TEXT,
    active          TEXT,
    prompt          TEXT NOT NULL DEFAULT '',
    options         TEXT NOT NULL DEFAULT '[]',
    -- The state blob dominates the file. See the note on _dump_state.
    state           TEXT NOT NULL DEFAULT '{}',
    chosen          INTEGER,
    chosen_label    TEXT NOT NULL DEFAULT '',
    why             TEXT NOT NULL DEFAULT '',
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    fallback        INTEGER NOT NULL DEFAULT 0,
    fallback_reason TEXT NOT NULL DEFAULT '',
    -- Anything the bridge sent that this version does not model as a column,
    -- notably the `since` event feed. Kept so a transcript stays complete when
    -- one side has been upgraded and the other has not.
    extra           TEXT NOT NULL DEFAULT '{}',
    at              TEXT NOT NULL,
    PRIMARY KEY (match_id, seq)
);

CREATE TABLE IF NOT EXISTS games (
    match_id    TEXT NOT NULL,
    game_no     INTEGER NOT NULL,
    winner_seat TEXT,
    draw        INTEGER NOT NULL DEFAULT 0,
    turns       INTEGER,
    ms          INTEGER,
    PRIMARY KEY (match_id, game_no)
);

CREATE TABLE IF NOT EXISTS events (
    match_id TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    payload  TEXT NOT NULL DEFAULT '{}',
    at       TEXT NOT NULL,
    PRIMARY KEY (match_id, seq)
);

CREATE INDEX IF NOT EXISTS decisions_by_match ON decisions (match_id, seq);
CREATE INDEX IF NOT EXISTS games_by_match ON games (match_id);
CREATE INDEX IF NOT EXISTS events_by_match ON events (match_id, seq);
"""


def default_db_path() -> Path:
    """One file per installation, under XDG_DATA_HOME when the user set it."""
    root = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(root) / "gauntlet" / "transcripts.db"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _connect(path: Path, *, create: bool) -> sqlite3.Connection:
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL lets `gauntlet replay` read a match that is still being played.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except ValueError:
        return fallback


def _dump_state(state: Mapping[str, Any]) -> str:
    """Serialise the state block a decision was made against.

    This column is the bulk of the database, a few kilobytes on every decision
    against a few hundred bytes for everything else. It stays because a
    reasoning string is unreadable without the board it describes. To prune,
    run `UPDATE decisions SET state='{}' WHERE match_id=?` on matches you have
    already read, or keep state only where `why` is non-empty.
    """
    return _dump(state)


class Transcript:
    """The writer. Safe to call from the thread serving the socket."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self._lock = threading.Lock()
        self._conn = _connect(self.db_path, create=True)
        self._game_no: dict[str, int] = {}
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    #: Columns added after the first release, as (table, column, definition).
    #: `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists,
    #: so a database written by an older version keeps its old shape until it is
    #: altered. Transcripts are the record of every game ever played here, so
    #: they are migrated in place rather than rebuilt.
    _ADDED_COLUMNS = (("decisions", "extra", "TEXT NOT NULL DEFAULT '{}'"),)

    def _migrate(self) -> None:
        for table, column, definition in self._ADDED_COLUMNS:
            existing = {
                row[1] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def start_match(
        self,
        *,
        match_id: str,
        # Shadows the builtin. The keyword is the caller's vocabulary and the
        # CLI flag is --format, renaming it here would only move the friction.
        format: str,
        seed: int | str | None,
        seats: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
        policy: Mapping[str, Any] | None = None,
        forge_version: str = "",
        bridge_revision: str = "",
    ) -> None:
        rows = list(_seat_rows(match_id, seats))
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO matches
                   (id, started_at, finished_at, format, seed, forge_version,
                    bridge_revision, policy, protocol_version, status)
                   VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'running')""",
                (
                    match_id,
                    _now(),
                    format,
                    None if seed is None else str(seed),
                    forge_version,
                    bridge_revision,
                    _dump(dict(policy or {})),
                    VERSION,
                ),
            )
            self._conn.executemany(
                """INSERT OR REPLACE INTO seats
                   (match_id, seat, deck_name, deck_source, controller, decklist)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )
        self._game_no[match_id] = 1

    def record_decision(
        self,
        *,
        match_id: str,
        seat: str,
        request: Request,
        response: Response | None,
        latency_ms: int,
        fallback: bool = False,
        fallback_reason: str = "",
    ) -> int:
        state = request.state or {}
        chosen = response.choice if response is not None else None
        options = [
            {"i": o.index, "label": o.label, "card": o.card, "cost": o.cost}
            for o in request.options
        ]
        label = ""
        if chosen is not None:
            for opt in request.options:
                if opt.index == chosen:
                    label = _option_text(opt.label, opt.cost)
                    break

        with self._lock, self._conn:
            seq = self._next_seq(match_id)
            self._conn.execute(
                """INSERT INTO decisions
                   (match_id, seq, game_no, request_id, seat, kind, turn, phase, active,
                    prompt, options, state, chosen, chosen_label, why, latency_ms,
                    fallback, fallback_reason, extra, at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    match_id,
                    seq,
                    self._current_game(match_id),
                    request.id,
                    seat or request.seat,
                    request.kind,
                    _as_int(state.get("turn")),
                    _as_str(state.get("phase")),
                    _as_str(state.get("active")),
                    request.prompt,
                    _dump(options),
                    _dump_state(state),
                    chosen,
                    label,
                    response.why if response is not None else "",
                    int(latency_ms),
                    1 if fallback else 0,
                    fallback_reason,
                    _dump(dict(request.extra or {})),
                    _now(),
                ),
            )
        return seq

    def record_event(self, *, match_id: str, kind: str, payload: Mapping[str, Any]) -> None:
        with self._lock, self._conn:
            seq = self._next_seq(match_id)
            self._conn.execute(
                "INSERT INTO events (match_id, seq, kind, payload, at) VALUES (?,?,?,?,?)",
                (match_id, seq, kind, _dump(dict(payload)), _now()),
            )

    def record_game(
        self,
        *,
        match_id: str,
        game_no: int,
        winner_seat: str | None,
        draw: bool = False,
        turns: int | None = None,
        ms: int | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO games
                   (match_id, game_no, winner_seat, draw, turns, ms)
                   VALUES (?,?,?,?,?,?)""",
                (match_id, int(game_no), winner_seat, 1 if draw else 0, turns, ms),
            )
        # Decisions after this one belong to the next game, whose turn counter
        # restarts at one. Without this the render would interleave two games.
        self._game_no[match_id] = int(game_no) + 1

    def finish_match(self, *, match_id: str, status: str = "finished") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE matches SET finished_at = ?, status = ? WHERE id = ?",
                (_now(), status, match_id),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Transcript:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # Decisions and events share one counter so the render can interleave them
    # in the order they happened. Caller holds the lock.
    def _next_seq(self, match_id: str) -> int:
        row = self._conn.execute(
            """SELECT MAX(s) AS s FROM (
                   SELECT MAX(seq) AS s FROM decisions WHERE match_id = ?
                   UNION ALL SELECT MAX(seq) FROM events WHERE match_id = ?)""",
            (match_id, match_id),
        ).fetchone()
        return (row["s"] or 0) + 1

    def _current_game(self, match_id: str) -> int:
        cached = self._game_no.get(match_id)
        if cached is not None:
            return cached
        # A process that did not start this match still needs the right number.
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM games WHERE match_id = ?", (match_id,)
        ).fetchone()
        game_no = int(row["n"]) + 1
        self._game_no[match_id] = game_no
        return game_no


def _seat_rows(
    match_id: str,
    seats: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
) -> Iterable[tuple[Any, ...]]:
    # Callers hold seats either as a list of dicts or keyed by seat letter.
    items: list[Mapping[str, Any]]
    if isinstance(seats, Mapping):
        items = [{"seat": k, **dict(v)} for k, v in seats.items()]
    else:
        items = [dict(s) for s in seats]
    for s in items:
        yield (
            match_id,
            str(s.get("seat", "")),
            str(s.get("deck_name", "")),
            str(s.get("deck_source", "")),
            str(s.get("controller", "")),
            _dump(s.get("decklist") or []),
        )


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _option_text(label: str, cost: str | None) -> str:
    return f"{label} {cost}" if cost else label


# --------------------------------------------------------------------------
# Reading


def _read(db_path: Path | None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else default_db_path()
    conn = _connect(path, create=True)
    conn.executescript(_SCHEMA)
    return conn


def list_matches(db_path: Path | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
    conn = _read(db_path)
    try:
        rows = conn.execute(
            """SELECT m.*,
                      (SELECT COUNT(*) FROM decisions d WHERE d.match_id = m.id) AS decisions,
                      (SELECT COUNT(*) FROM games g WHERE g.match_id = m.id) AS games
               FROM matches m ORDER BY m.started_at DESC, m.id DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            seats = conn.execute(
                "SELECT seat, deck_name, controller FROM seats WHERE match_id = ? ORDER BY seat",
                (row["id"],),
            ).fetchall()
            item = dict(row)
            item["policy"] = _load(row["policy"], {})
            item["seats"] = [dict(s) for s in seats]
            out.append(item)
        return out
    finally:
        conn.close()


def summarise(match_id: str, db_path: Path | None = None) -> dict[str, Any]:
    conn = _read(db_path)
    try:
        match = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
        decisions = conn.execute(
            "SELECT * FROM decisions WHERE match_id = ? ORDER BY seq", (match_id,)
        ).fetchall()
        games = conn.execute(
            "SELECT * FROM games WHERE match_id = ? ORDER BY game_no", (match_id,)
        ).fetchall()
        events = conn.execute("SELECT kind FROM events WHERE match_id = ?", (match_id,)).fetchall()

        by_seat: dict[str, dict[str, int]] = {}
        by_kind: dict[str, int] = {}
        latencies: list[int] = []
        fallbacks = 0
        unknown_kinds: set[str] = set()
        for d in decisions:
            seat = by_seat.setdefault(d["seat"], {"decisions": 0, "fallbacks": 0, "why": 0})
            seat["decisions"] += 1
            by_kind[d["kind"]] = by_kind.get(d["kind"], 0) + 1
            if d["kind"] not in KINDS:
                unknown_kinds.add(d["kind"])
            if d["fallback"]:
                seat["fallbacks"] += 1
                fallbacks += 1
            if d["why"]:
                seat["why"] += 1
            latencies.append(int(d["latency_ms"]))

        wins: dict[str, int] = {}
        draws = 0
        for g in games:
            if g["draw"]:
                draws += 1
            elif g["winner_seat"]:
                wins[g["winner_seat"]] = wins.get(g["winner_seat"], 0) + 1

        winner = None
        if wins:
            best = max(wins.values())
            leaders = [s for s, n in wins.items() if n == best]
            if len(leaders) == 1:
                winner = leaders[0]

        return {
            "match_id": match_id,
            "exists": match is not None,
            "status": match["status"] if match else "unknown",
            "format": match["format"] if match else "",
            "seed": match["seed"] if match else None,
            "started_at": match["started_at"] if match else None,
            "finished_at": match["finished_at"] if match else None,
            "decisions": len(decisions),
            "events": len(events),
            "games": len(games),
            "wins": wins,
            "draws": draws,
            "winner": winner,
            "fallbacks": fallbacks,
            "by_seat": by_seat,
            "by_kind": by_kind,
            "unknown_kinds": sorted(unknown_kinds),
            "latency_ms": _latency_stats(latencies),
        }
    finally:
        conn.close()


def _latency_stats(values: list[int]) -> dict[str, int]:
    if not values:
        return {"n": 0, "mean": 0, "p50": 0, "p90": 0, "max": 0}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "mean": round(sum(ordered) / len(ordered)),
        "p50": _pct(ordered, 0.50),
        "p90": _pct(ordered, 0.90),
        "max": ordered[-1],
    }


def _pct(ordered: list[int], q: float) -> int:
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def render(match_id: str, db_path: Path | None = None, *, verbose: bool = False) -> str:
    """Markdown play-by-play, grouped by game and turn.

    Works on a match that is still running and on one that crashed, because it
    reads whatever rows are on disk and never assumes a final game row.
    """
    conn = _read(db_path)
    try:
        match = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
        seats = conn.execute(
            "SELECT * FROM seats WHERE match_id = ? ORDER BY seat", (match_id,)
        ).fetchall()
        decisions = conn.execute(
            "SELECT * FROM decisions WHERE match_id = ? ORDER BY seq", (match_id,)
        ).fetchall()
        games = conn.execute(
            "SELECT * FROM games WHERE match_id = ? ORDER BY game_no", (match_id,)
        ).fetchall()
        events = conn.execute(
            "SELECT * FROM events WHERE match_id = ? ORDER BY seq", (match_id,)
        ).fetchall()
    finally:
        conn.close()

    if match is None:
        return f"# {match_id}\n\nNo such match in this database.\n"

    lines: list[str] = []
    lines += _render_header(match, seats, decisions)
    lines += _render_body(decisions, events, multi_game=len(games) > 1, verbose=verbose)
    lines += _render_games(games, match)
    return "\n".join(lines).rstrip() + "\n"


def _render_header(
    match: sqlite3.Row, seats: Sequence[sqlite3.Row], decisions: Sequence[sqlite3.Row]
) -> list[str]:
    title = f"# Match {match['id']}"
    if match["format"]:
        title += f" — {match['format']}"
    lines = [title, ""]

    for s in seats:
        who = " · ".join(x for x in [s["deck_name"], s["controller"], s["deck_source"]] if x)
        lines.append(f"**{s['seat']}** — {who}" if who else f"**{s['seat']}**")
    if seats:
        lines.append("")

    facts = [f"status {match['status']}"]
    if match["seed"] is not None:
        facts.append(f"seed {match['seed']}")
    if match["forge_version"]:
        facts.append(f"forge {match['forge_version']}")
    if match["bridge_revision"]:
        facts.append(f"bridge {match['bridge_revision']}")
    facts.append(f"{len(decisions)} decisions")
    fallbacks = sum(1 for d in decisions if d["fallback"])
    if fallbacks:
        facts.append(f"{fallbacks} fallback" + ("s" if fallbacks != 1 else ""))
    lines += [" · ".join(facts), ""]
    if match["status"] == "running":
        lines += ["_Match still running, this is everything recorded so far._", ""]
    elif match["status"] == "crashed":
        lines += ["_Match crashed, this is everything recorded before it died._", ""]
    return lines


def _render_body(
    decisions: Sequence[sqlite3.Row],
    events: Sequence[sqlite3.Row],
    *,
    multi_game: bool,
    verbose: bool,
) -> list[str]:
    if not decisions and not events:
        return ["_Nothing recorded yet._", ""]

    rows = sorted(
        [("decision", d) for d in decisions] + [("event", e) for e in events],
        key=lambda r: r[1]["seq"],
    )

    lines: list[str] = []
    game_shown: int | None = None
    group_shown: object = object()
    for kind, row in rows:
        if kind == "event":
            lines += _render_event(row)
            continue

        if multi_game and row["game_no"] != game_shown:
            game_shown = row["game_no"]
            lines += [f"# Game {game_shown}", ""]
        group = (row["game_no"], row["turn"])
        if group != group_shown:
            group_shown = group
            lines += [_turn_heading(row), ""]
        lines += _render_decision(row, verbose=verbose)
    return lines


def _turn_heading(row: sqlite3.Row) -> str:
    if row["turn"] is None:
        return "## Pre-game"
    if row["active"]:
        return f"## Turn {row['turn']} — {row['active']}'s turn"
    return f"## Turn {row['turn']}"


def _render_decision(row: sqlite3.Row, *, verbose: bool) -> list[str]:
    parts = [p for p in (row["phase"], row["chosen_label"] or row["kind"]) if p]
    head = f"**{row['seat']}** " + " · ".join(parts)
    if row["fallback"]:
        # Never let a Forge AI decision read as the agent's own play.
        reason = row["fallback_reason"] or "no answer"
        head += f" (Forge AI decided — {reason})"
    lines = [head]
    if row["why"]:
        lines += [f"> {line}" for line in str(row["why"]).splitlines() if line.strip()]
    lines.append("")

    if verbose:
        lines += _render_verbose(row)
    return lines


def _render_verbose(row: sqlite3.Row) -> list[str]:
    lines = [f"<sub>seq {row['seq']} · {row['kind']} · {row['latency_ms']}ms</sub>", ""]
    if row["prompt"] and row["prompt"] != row["chosen_label"]:
        lines += [f"_{row['prompt']}_", ""]
    options = _load(row["options"], [])
    if options:
        for opt in options:
            mark = "x" if opt.get("i") == row["chosen"] else " "
            text = _option_text(opt.get("label", ""), opt.get("cost"))
            lines.append(f"- [{mark}] {opt.get('i')} {text}")
        lines.append("")
    state = _load(row["state"], {})
    if state:
        lines += ["```json", json.dumps(state, indent=2, sort_keys=False), "```", ""]
    return lines


def _render_event(row: sqlite3.Row) -> list[str]:
    payload = _load(row["payload"], {})
    body = ", ".join(f"{k}={v}" for k, v in payload.items()) if payload else ""
    return [f"*{row['kind']}*" + (f" — {body}" if body else ""), ""]


def _render_games(games: Sequence[sqlite3.Row], match: sqlite3.Row) -> list[str]:
    if not games:
        return []
    lines = ["## Result", ""]
    for g in games:
        if g["draw"]:
            outcome = "draw"
        elif g["winner_seat"]:
            outcome = f"{g['winner_seat']} won"
        else:
            outcome = "unfinished"
        detail = []
        if g["turns"] is not None:
            detail.append(f"{g['turns']} turns")
        if g["ms"] is not None:
            detail.append(f"{g['ms'] / 1000:.1f}s")
        suffix = f" ({', '.join(detail)})" if detail else ""
        lines.append(f"- Game {g['game_no']}: {outcome}{suffix}")
    lines.append("")
    if match["status"] != "finished":
        lines += [f"_Match {match['status']}._", ""]
    return lines
