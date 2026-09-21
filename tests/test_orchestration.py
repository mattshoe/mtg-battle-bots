"""Planning and running matches, and sweeping a field.

This is the layer every incident so far has lived in, and it was the least
tested. A fake engine stands in for Forge throughout, so these run in
milliseconds and spend nothing.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from gauntlet import match as matchmod
from gauntlet import sweep as sweepmod
from gauntlet.budget import Budget

DECK_TEXT = "1 Sol Ring\n1 Command Tower\n" + "\n".join(f"1 Card {i}" for i in range(97))


@pytest.fixture
def deck_file(tmp_path) -> Path:
    p = tmp_path / "testdeck.txt"
    p.write_text("Commander: Jetmir, Nexus of Revels\n" + DECK_TEXT)
    return p


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    """Keep every path this writes to inside tmp_path."""
    from gauntlet import paths

    for name, sub in (
        ("deck_cache", "decks"),
        ("state_dir", "state"),
        ("data_dir", "data"),
    ):
        target = tmp_path / sub
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(paths, name, lambda t=target: t)
    monkeypatch.setattr(paths, "transcripts_db", lambda: tmp_path / "t.db")
    # match_socket is deliberately left alone. pytest's tmp_path is long
    # enough to exceed the AF_UNIX limit, which is exactly the case the real
    # function handles, so overriding it here would hide the behaviour.
    monkeypatch.setattr(paths, "match_meta", lambda m: tmp_path / "state" / f"{m}.json")
    monkeypatch.setattr(paths, "forge_version", lambda: "test")
    # Every jar is faked, so these run on a machine with no Forge install.
    # Three of them failed in CI for exactly this reason while passing locally.
    for name in ("bridge_jar", "forge_jar", "gson_jar"):
        jar = tmp_path / f"{name}.jar"
        jar.write_text("")
        monkeypatch.setattr(paths, name, lambda j=jar: j)
    return tmp_path


class _FakeForge:
    """A Forge that reports results without playing anything.

    Enough of the real object's surface for `match.run` to drive it: it is
    launched, it emits result lines, it is waited on, and it can be stopped.
    """

    def __init__(self, games: int = 1, *, hang: bool = False) -> None:
        self.games = games
        self.hang = hang
        self.stopped = threading.Event()
        self.on_line = None

    def emit(self) -> None:
        for i in range(1, self.games + 1):
            self.on_line(
                json.dumps(
                    {
                        "kind": "game_result",
                        "game": i,
                        "winner": "A" if i % 2 else "B",
                        "draw": False,
                        "turns": 12 + i,
                        "ms": 1000,
                    }
                )
            )

    def wait(self, timeout=None):
        self.emit()
        if self.hang:
            self.stopped.wait(timeout=5)
        return 0

    def stop(self, grace: float = 5.0) -> None:
        self.stopped.set()

    def drain(self, timeout: float | None = None) -> bool:
        """The real one joins the log pump. Nothing to wait for here."""
        return True


@pytest.fixture
def fake_engine(monkeypatch):
    """Replace the JVM launch with something that returns instantly."""
    created = []

    def launch(cmd, log_path, *, on_line=None, trace=False):
        forge = created.pop(0) if created else _FakeForge()
        forge.on_line = on_line or (lambda line: None)
        forge.cmd = cmd
        return forge

    monkeypatch.setattr(matchmod.engine, "launch", launch)
    return created


# ------------------------------------------------------------------- planning


def test_plan_writes_a_dck_per_seat(_isolated, deck_file) -> None:
    planned = matchmod.plan(
        [
            matchmod.SeatSpec(seat="A", deck=str(deck_file)),
            matchmod.SeatSpec(seat="B", deck=str(deck_file)),
        ]
    )
    for seat in ("A", "B"):
        written = planned.deck_paths[seat]
        assert written.exists()
        assert "[Commander]" in written.read_text()


def test_plan_gives_every_match_a_distinct_id(_isolated, deck_file) -> None:
    """Two matches started in the same second must not share a directory, a
    socket, or a transcript row."""
    ids = {
        matchmod.plan([matchmod.SeatSpec(seat="A", deck=str(deck_file))]).match_id
        for _ in range(20)
    }
    assert len(ids) == 20


def test_has_interactive_detects_any_interactive_seat(_isolated, deck_file) -> None:
    def planned(*controllers):
        return matchmod.plan(
            [
                matchmod.SeatSpec(seat=chr(65 + i), deck=str(deck_file), controller=c)
                for i, c in enumerate(controllers)
            ]
        )

    assert not planned("forge", "forge").has_interactive
    assert planned("forge", "interactive").has_interactive
    assert planned("interactive", "interactive").has_interactive


def test_resolve_deck_prefers_a_real_path_over_a_collection_name(
    _isolated, deck_file, monkeypatch
) -> None:
    """A path that exists wins, because somebody who typed a path meant it.

    The previous version asserted an `or` of two things that were both true and
    never set up a collection deck to lose to, so it tested nothing.
    """
    from gauntlet import decks as deckmod

    def collection_would_answer(name, **kw):
        raise AssertionError(f"went to the collection for {name!r} despite a real file")

    monkeypatch.setattr(deckmod, "from_collection", collection_would_answer)
    resolved = matchmod.resolve_deck(str(deck_file))
    assert "testdeck" in resolved.name


def test_resolve_deck_reads_a_dck_without_reparsing_it(_isolated, tmp_path) -> None:
    dck = tmp_path / "ready.dck"
    dck.write_text(
        "[metadata]\nName=Ready\n[Commander]\n1 Jetmir, Nexus of Revels\n[Main]\n99 Plains\n"
    )
    resolved = matchmod.resolve_deck(str(dck))
    assert resolved.commanders == ("Jetmir, Nexus of Revels",)
    assert resolved.source.startswith("dck:")


# -------------------------------------------------------------------- running


def test_run_records_every_game_forge_reports(_isolated, deck_file, fake_engine) -> None:
    fake_engine.append(_FakeForge(games=4))
    planned = matchmod.plan(
        [
            matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="forge"),
            matchmod.SeatSpec(seat="B", deck=str(deck_file), controller="forge"),
        ],
        games=4,
    )
    result = matchmod.run(planned)
    assert len(result.games) == 4
    assert result.wins_by_seat() == {"A": 2, "B": 2}
    assert not result.crashed


def test_a_forge_only_run_opens_no_seats(_isolated, deck_file, fake_engine) -> None:
    """Forge plays inside the JVM. A python seat for it would never be asked and
    would only add a way to get the wiring wrong."""
    planned = matchmod.plan(
        [
            matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="forge"),
            matchmod.SeatSpec(seat="B", deck=str(deck_file), controller="forge"),
        ]
    )
    assert matchmod.build_seats(planned) == {}


def test_run_writes_the_match_and_its_seats_to_the_transcript(
    _isolated, deck_file, fake_engine
) -> None:
    import sqlite3

    from gauntlet import paths

    planned = matchmod.plan(
        [
            matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="forge"),
            matchmod.SeatSpec(seat="B", deck=str(deck_file), controller="forge"),
        ],
        seed=99,
    )
    matchmod.run(planned)

    conn = sqlite3.connect(paths.transcripts_db())
    row = conn.execute(
        "SELECT seed, status FROM matches WHERE id = ?", (planned.match_id,)
    ).fetchone()
    seats = conn.execute(
        "SELECT seat, controller FROM seats WHERE match_id = ? ORDER BY seat",
        (planned.match_id,),
    ).fetchall()
    conn.close()

    assert row == ("99", "finished")
    assert seats == [("A", "forge"), ("B", "forge")]


def test_a_crashed_engine_is_reported_rather_than_swallowed(
    _isolated, deck_file, fake_engine, monkeypatch
) -> None:
    class _Crashing(_FakeForge):
        def wait(self, timeout=None):
            return 1

    fake_engine.append(_Crashing())
    planned = matchmod.plan([matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="forge")])
    result = matchmod.run(planned)
    assert result.crashed
    assert "exited 1" in result.error


def test_the_plan_round_trips_through_disk(_isolated, deck_file) -> None:
    """A detached match is handed its plan as json. Anything lost in that trip
    is a difference between a foreground run and a background one."""
    planned = matchmod.plan(
        [
            matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="interactive"),
            matchmod.SeatSpec(seat="B", deck=str(deck_file), controller="forge"),
        ],
        seed=7,
        games=3,
        game_format="Commander",
    )
    path = matchmod._dump_plan(planned)
    loaded = matchmod.load_plan(path)

    assert loaded.match_id == planned.match_id
    assert loaded.seed == planned.seed
    assert loaded.games == planned.games
    assert [s.controller for s in loaded.specs] == ["interactive", "forge"]
    assert loaded.decklists["A"].commanders == planned.decklists["A"].commanders
    assert loaded.deck_paths == planned.deck_paths


# --------------------------------------------------------------------- sweeps


def test_a_pairing_counts_wins_losses_and_draws(_isolated) -> None:
    p = sweepmod.Pairing(deck="x", opponent="y", games=4)
    p.wins, p.losses, p.draws = 3, 1, 2
    assert p.played == 6
    # A draw is usually the clock running out, so counting it as half a win
    # would flatter a deck that stalls.
    assert p.win_rate == 0.75


def test_win_rate_of_a_pairing_that_never_finished_is_not_a_crash() -> None:
    assert sweepmod.Pairing(deck="x", opponent="y", games=0).win_rate == 0.0
    assert sweepmod.Pairing(deck="x", opponent="y", games=0).median_turns == 0


def test_worker_count_drops_when_seats_cost_money() -> None:
    """Forge workers are bounded by memory. Agent workers are bounded by how
    many live model sessions upstream will tolerate, which is fewer.

    Asserted as a strict drop rather than a range, because the previous version
    passed for a function returning any constant.
    """
    from unittest.mock import patch

    # Asserted across machine sizes rather than on this one. A four-core runner
    # clamps free and paid to the same floor, so a strict drop is only true
    # where there are cores to drop from, and asserting it unconditionally
    # failed in CI while passing on a laptop.
    for cores in (2, 4, 8, 16, 32):
        with patch("os.cpu_count", lambda c=cores: c):
            free = sweepmod.default_workers(0)
            one_paid = sweepmod.default_workers(1)
            two_paid = sweepmod.default_workers(2)

            assert free >= one_paid >= two_paid >= 1, (
                f"at {cores} cores: free={free} one={one_paid} two={two_paid}"
            )

    # Where there is room to differ, paid seats must actually get fewer.
    with patch("os.cpu_count", lambda: 32):
        assert sweepmod.default_workers(0) > sweepmod.default_workers(2)


def test_the_table_warns_when_a_seat_ran_out_mid_sweep() -> None:
    """The headline failure. A sweep that lost its seat reported a clean record
    for two hours, so the warning is part of the contract now."""
    good = sweepmod.Pairing(deck="d", opponent="ok", games=2)
    good.wins = 2
    spent = sweepmod.Pairing(deck="d", opponent="died", games=2)
    spent.wins, spent.exhausted = 1, True

    table = sweepmod.format_table("d", [good, spent], 1.0)
    assert "WARNING" in table
    assert "not" in table.lower()

    clean = sweepmod.format_table("d", [good], 1.0)
    assert "WARNING" not in clean


def test_one_budget_covers_the_whole_sweep(_isolated, deck_file, monkeypatch) -> None:
    """A budget per pairing would multiply the cap by the size of the field."""
    seen: list[Budget | None] = []

    def fake_pairing(p, **kw):
        seen.append(kw.get("budget"))
        p.wins = p.games
        return p

    monkeypatch.setattr(sweepmod, "run_pairing", fake_pairing)
    shared = Budget(max_usd=5.0)
    sweepmod.run_sweep("d", ["a", "b", "c"], games=1, budget=shared, workers=1)

    assert len(seen) == 3
    assert all(b is shared for b in seen)


def test_a_sweep_stops_at_the_first_exhausted_pairing(_isolated, monkeypatch) -> None:
    """Carrying on means every later pairing is Forge wearing an agent's name."""
    played: list[str] = []

    def fake_pairing(p, **kw):
        played.append(p.opponent)
        if p.opponent == "second":
            p.exhausted = True
        else:
            p.wins = p.games
        return p

    monkeypatch.setattr(sweepmod, "run_pairing", fake_pairing)
    results = sweepmod.run_sweep("d", ["first", "second", "third", "fourth"], games=1, workers=1)

    assert "second" in played
    # Whether a later pairing was cancelled before starting or refused on the
    # way in, what matters is that none of them played a game.
    actually_played = [p for p in results if not p.exhausted and p.wins]
    assert all(p.opponent in ("first",) for p in actually_played), actually_played
    assert any(r.exhausted for r in results)
