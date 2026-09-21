"""The assignments nobody executes.

A mutation battery found that this project's units are well tested and its
wiring is not. Every Tier 1 survivor was a single line in `match.run` or
`cli.run_match` — one assignment, one `if` — that no test ever ran with a real
consequence attached.

So these tests drive the real entry points against a fake engine, and assert on
what a caller would actually see. Deleting any one of those lines has to turn
one of these red.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gauntlet import match as matchmod
from gauntlet.cli import app
from gauntlet.protocol import Request, Response
from gauntlet.seats import Seat, SeatExhausted, SeatTimeout

runner = CliRunner()

DECK = "Commander: Jetmir, Nexus of Revels\n" + "\n".join(f"1 Card {i}" for i in range(99))


@pytest.fixture
def deck_file(tmp_path) -> Path:
    p = tmp_path / "d.txt"
    p.write_text(DECK)
    return p


@pytest.fixture
def fake_forge(monkeypatch, tmp_path):
    """A Forge that plays games without a JVM, and records being stopped."""
    from gauntlet import paths

    for name in ("bridge_jar", "forge_jar", "gson_jar"):
        jar = tmp_path / f"{name}.jar"
        jar.write_text("")
        monkeypatch.setattr(paths, name, lambda j=jar: j)
    monkeypatch.setattr(paths, "forge_version", lambda: "test")

    state: dict = {"stopped": False, "drained": True}

    class _Forge:
        def __init__(self) -> None:
            self.on_line = None
            self.feed = 0

        def wait(self, timeout=None):
            if self.feed and state.get("endpoint"):
                _feed(state["endpoint"], self.feed)
            for i in (1, 2):
                self.on_line(
                    json.dumps(
                        {
                            "kind": "game_result",
                            "game": i,
                            "winner": "A",
                            "draw": False,
                            "turns": 10 + i,
                            "ms": 100,
                        }
                    )
                )
            return 0

        def drain(self, timeout=None):
            return state["drained"]

        def stop(self, grace: float = 5.0) -> None:
            state["stopped"] = True

    def launch(cmd, log_path, *, on_line=None, trace=False):
        forge = _Forge()
        forge.on_line = on_line or (lambda line: None)
        state["cmd"] = cmd
        # The endpoint Forge was told to connect to, so a test can have the
        # fake ask questions on it the way the real bridge does.
        for i, part in enumerate(cmd):
            if part == "--bridge":
                state["endpoint"] = cmd[i + 1].split("=", 1)[1]
        forge.feed = state.get("feed", 0)
        return forge

    monkeypatch.setattr(matchmod.engine, "launch", launch)
    return state


class _NeverAnswers(Seat):
    controller = "sdk"
    costs_money = True

    def decide(self, request: Request, timeout: float) -> Response:
        raise SeatTimeout("the agent never answered")


class _DiesImmediately(Seat):
    controller = "sdk"
    costs_money = True

    def decide(self, request: Request, timeout: float) -> Response:
        raise SeatExhausted("session limit")


def _plan(deck_file: Path, controller: str = "sdk"):
    return matchmod.plan(
        [
            matchmod.SeatSpec(seat="A", deck=str(deck_file), controller=controller),
            matchmod.SeatSpec(seat="B", deck=str(deck_file), controller="forge"),
        ],
        games=2,
    )


def _feed(endpoint: str, count: int) -> None:
    """Act as the bridge: ask a bridged seat some questions."""
    host, port = endpoint.split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        stream = sock.makefile("rwb")
        for i in range(1, count + 1):
            stream.write(
                (
                    json.dumps(
                        {
                            "v": 1,
                            "id": i,
                            "seat": "A",
                            "kind": "cast_or_pass",
                            "prompt": "priority",
                            "options": [{"i": 0, "label": "Pass priority"}],
                            "state": {"turn": 1},
                        }
                    )
                    + "\n"
                ).encode()
            )
            stream.flush()
            if not stream.readline():
                return


# --------------------------------------------------------- expected seats


def test_a_run_whose_bridge_never_connected_is_not_trustworthy(
    _isolated, deck_file, fake_forge, monkeypatch
) -> None:
    """The wiring for the whole trust mechanism is one assignment in run().

    Deleting `server.result.expected_seats = ...` made a run where Forge played
    every decision come back valid with a clean win record, and the suite stayed
    green because every trust test built its MatchResult by hand.
    """
    monkeypatch.setattr(matchmod, "build_seats", lambda p: {"A": _NeverAnswers()})
    result = matchmod.run(_plan(deck_file))

    assert result.games, "the fake engine should still have reported games"
    assert result.decisions == 0
    assert not result.trustworthy
    assert "never asked" in result.untrustworthy_because


def test_a_run_is_trustworthy_when_the_seat_actually_played(
    _isolated, deck_file, fake_forge, monkeypatch
) -> None:
    """The other direction, so the guard cannot be satisfied by always failing."""

    class _Answers(Seat):
        controller = "sdk"
        costs_money = True
        last_usage = (100, 10)

        def decide(self, request: Request, timeout: float) -> Response:
            return Response(id=request.id, choice=0, why="fine")

    monkeypatch.setattr(matchmod, "build_seats", lambda p: {"A": _Answers()})
    fake_forge["feed"] = 10

    result = matchmod.run(_plan(deck_file))

    assert result.decisions == 10
    assert result.fallbacks == 0
    assert result.trustworthy, result.untrustworthy_because


# -------------------------------------------------------------- stop wiring


def test_an_exhausted_seat_stops_the_real_engine(
    _isolated, deck_file, fake_forge, monkeypatch
) -> None:
    """`server.stop_engine = forge.stop` is one line in run().

    Deleting it survived every existing test, because the exhaustion test
    supplies its own callback and so only proves that _halt calls something.
    """
    monkeypatch.setattr(matchmod, "build_seats", lambda p: {"A": _DiesImmediately()})
    fake_forge["feed"] = 1

    matchmod.run(_plan(deck_file))

    for _ in range(50):
        if fake_forge["stopped"]:
            break
        time.sleep(0.02)
    assert fake_forge["stopped"], "an exhausted seat left the engine running"


def test_output_forge_never_finished_writing_is_reported(
    _isolated, deck_file, fake_forge, monkeypatch
) -> None:
    """A run whose log pump did not finish has an incomplete record."""
    monkeypatch.setattr(matchmod, "build_seats", lambda p: {})
    fake_forge["drained"] = False

    result = matchmod.run(
        matchmod.plan(
            [matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="forge")],
            games=2,
        )
    )
    assert "not fully read" in result.error


# --------------------------------------------------------- the json contract


def test_run_json_reports_an_untrustworthy_result_and_exits_nonzero(
    _isolated, deck_file, monkeypatch
) -> None:
    """The machine-readable contract, which no test invoked.

    A scripted caller reads `valid` and the exit code. Both were hardcodable to
    success with the suite green.
    """
    from gauntlet.server import MatchResult

    bad = MatchResult(match_id="m")
    bad.expected_seats = {"A"}
    bad.per_seat = {"A": (20, 20)}
    bad.games.append({"game": 1, "winner": "A", "draw": False})
    monkeypatch.setattr(matchmod, "run", lambda p, **kw: bad)

    result = runner.invoke(
        app,
        [
            "run",
            "--a",
            str(deck_file),
            "--b",
            str(deck_file),
            "--seat-a",
            "forge",
            "--seat-b",
            "forge",
            "--json",
        ],
    )
    payload = json.loads(result.output)

    assert payload["valid"] is False
    assert payload["fallbacks"] == 20
    assert payload["untrustworthy_because"]
    assert result.exit_code == 1, "a void run exited 0, so a script would take it"


def test_run_json_reports_a_good_result_and_exits_zero(_isolated, deck_file, monkeypatch) -> None:
    from gauntlet.server import MatchResult

    good = MatchResult(match_id="m")
    good.expected_seats = {"A"}
    good.per_seat = {"A": (20, 0)}
    good.games.append({"game": 1, "winner": "A", "draw": False})
    monkeypatch.setattr(matchmod, "run", lambda p, **kw: good)

    result = runner.invoke(
        app,
        [
            "run",
            "--a",
            str(deck_file),
            "--b",
            str(deck_file),
            "--seat-a",
            "forge",
            "--seat-b",
            "forge",
            "--json",
        ],
    )
    payload = json.loads(result.output)
    assert payload["valid"] is True
    assert result.exit_code == 0


def test_the_fallback_threshold_is_where_it_says_it_is() -> None:
    """The constant can drift to 0.99 and every relative assertion still holds."""
    from gauntlet.server import MAX_TOLERABLE_FALLBACK_RATE, MatchResult

    assert MAX_TOLERABLE_FALLBACK_RATE == 0.25

    middling = MatchResult(match_id="m")
    middling.expected_seats = {"A"}
    middling.per_seat = {"A": (100, 30)}
    assert not middling.trustworthy, "30% unanswered read as a real result"


# ----------------------------------------------------------- budget wiring


def test_a_detached_run_carries_its_budget_through_the_plan_file(_isolated, deck_file) -> None:
    """A detached run is a different process, so the cap travels as JSON.

    `budget_for` returning None survived every test, and detaching is what the
    documented two-agent mode does, so this was the configuration where an
    uncapped run costs real money.
    """
    from gauntlet.budget import Budget

    planned = matchmod.plan(
        [
            matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="interactive"),
            matchmod.SeatSpec(seat="B", deck=str(deck_file), controller="sdk"),
        ],
        budget=Budget(max_usd=3.5, max_decisions=777, model="claude-opus-5"),
    )
    reloaded = matchmod.load_plan(matchmod._dump_plan(planned))
    budget = matchmod.budget_for(reloaded)

    assert budget is not None, "a detached run would have been uncapped"
    assert budget.max_usd == 3.5
    assert budget.max_decisions == 777
    assert budget.model == "claude-opus-5"


def test_a_plan_with_no_budget_stays_uncapped(_isolated, deck_file) -> None:
    """A Forge-only run spends nothing and must not carry a dollar cap."""
    planned = matchmod.plan([matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="forge")])
    assert matchmod.budget_for(matchmod.load_plan(matchmod._dump_plan(planned))) is None


# ---------------------------------------------------------- routing wiring


def test_a_bare_routed_list_applies_to_every_seat() -> None:
    """--routed is the documented cost dial, and a bare list was ignored.

    cast_or_pass is most of a game's decisions, so a user trimming it paid full
    price with no signal.
    """
    from gauntlet.cli import _parse_routed

    parsed = _parse_routed("attack,block")
    assert parsed["*"] == ("attack", "block")


def test_a_lowercase_seat_letter_still_names_that_seat() -> None:
    from gauntlet.cli import _parse_routed

    assert _parse_routed("a=attack")["A"] == ("attack",)


def test_a_forge_seat_is_never_given_a_bridge(_isolated, deck_file, fake_forge) -> None:
    """A bridge for a seat Forge plays opens a connection nothing answers, and
    every decision on it times out."""
    matchmod.run(
        matchmod.plan(
            [
                matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="forge"),
                matchmod.SeatSpec(seat="B", deck=str(deck_file), controller="forge"),
            ]
        )
    )
    cmd = fake_forge["cmd"]
    assert "--bridge" not in cmd


# --------------------------------------------- the budget reaching a real run


def test_the_cap_the_user_confirmed_reaches_the_running_match(
    _isolated, deck_file, monkeypatch
) -> None:
    """Every budget test built a Budget by hand, and every cost-gate test
    stopped at a refusal. Nothing checked the object reaches a match, so
    dropping `budget=budget` from the run call survived.
    """
    seen: dict = {}

    def capture(plan_, *, budget=None, **kw):
        from gauntlet.server import MatchResult

        seen["budget"] = budget
        seen["plan"] = plan_
        r = MatchResult(match_id=plan_.match_id)
        r.expected_seats = set()
        return r

    monkeypatch.setattr(matchmod, "run", capture)
    result = runner.invoke(
        app,
        [
            "run",
            "--a",
            str(deck_file),
            "--b",
            str(deck_file),
            "--seat-a",
            "api",
            "--seat-b",
            "forge",
            "--games",
            "1",
            "--max-cost",
            "4.25",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen["budget"] is not None, "the match ran with no cap at all"
    assert seen["budget"].max_usd == 4.25
    # And the plan carries it too, for the detached path.
    assert seen["plan"].max_usd == 4.25


def test_the_cap_reaches_a_sweep(_isolated, monkeypatch) -> None:
    seen: dict = {}

    def capture(deck, opponents, **kw):
        seen.update(kw)
        return [], 0.1

    monkeypatch.setattr(app, "info", app.info)
    from gauntlet import sweep as sweepmod

    monkeypatch.setattr(sweepmod, "timed_sweep", capture)
    from gauntlet import cli as climod

    monkeypatch.setattr(climod.sweep, "timed_sweep", capture)

    runner.invoke(
        app,
        [
            "sweep",
            "--deck",
            "x",
            "--against",
            "a,b",
            "--games",
            "1",
            "--seat-a",
            "api",
            "--seat-b",
            "forge",
            "--max-cost",
            "3.5",
            "--yes",
        ],
    )
    assert seen.get("budget") is not None, "the sweep ran with no cap"
    assert seen["budget"].max_usd == 3.5


def test_a_sweep_nobody_played_exits_nonzero(_isolated, monkeypatch) -> None:
    """`run` had this contract and `sweep` did not, so a script checking the
    exit status took a wholly fictional sweep as clean."""
    from gauntlet import cli as climod
    from gauntlet.sweep import Pairing

    void = Pairing(deck="mine", opponent="theirs", games=2)
    void.exhausted = True
    void.error = "seat ran out of capacity"

    monkeypatch.setattr(climod.sweep, "timed_sweep", lambda *a, **kw: ([void], 1.0))
    result = runner.invoke(
        app,
        [
            "sweep",
            "--deck",
            "mine",
            "--against",
            "theirs",
            "--games",
            "2",
            "--seat-a",
            "forge",
            "--seat-b",
            "forge",
            "--json",
        ],
    )
    payload = json.loads(result.output)
    assert payload["valid"] is False
    assert result.exit_code == 1


def test_the_headline_rate_excludes_pairings_the_seat_did_not_play() -> None:
    """The documented regression: a deck that went 1-3 reported at 79% with the
    warning printed underneath the number."""
    from gauntlet.sweep import Pairing, format_table

    played = Pairing(deck="d", opponent="real", games=4)
    played.wins, played.losses = 1, 3
    void = Pairing(deck="d", opponent="void", games=20)
    void.wins, void.losses, void.exhausted = 18, 2, True
    void.error = "seat ran out of capacity"

    table = format_table("d", [played, void], 1.0)
    assert "overall 1-3-0" in table, table
    assert "excludes 1 pairing" in table
    assert "WARNING" in table


def test_a_seat_that_died_late_is_still_void(_isolated) -> None:
    """Existing exhaustion tests all halt on decision one, where the fallback
    rate covers for a missing exhausted flag. A seat that died on turn 18 of 20
    leaves the rate under the threshold, so the flag is the only signal."""
    from gauntlet.server import MatchResult

    late = MatchResult(match_id="late")
    late.expected_seats = {"A"}
    late.per_seat = {"A": (100, 5)}
    late.exhausted = {"A": "session limit"}

    assert not late.trustworthy
    assert "ran out of capacity" in late.untrustworthy_because


# ------------------------------------------- what run() actually hands Forge


def test_the_seed_reaches_forge(_isolated, deck_file, fake_forge) -> None:
    """Reproducibility rests on this one argument.

    build_command is tested in isolation and the stored seed is tested, and
    nothing connected them, so hardcoding seed=None survived. Every
    before-and-after deck comparison would have become noise with no signal.
    """
    matchmod.run(
        matchmod.plan(
            [matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="forge")],
            seed=4242,
        )
    )
    cmd = fake_forge["cmd"]
    assert "--seed" in cmd
    assert cmd[cmd.index("--seed") + 1] == "4242"


def test_each_seat_gets_its_own_deck_file(_isolated, tmp_path, fake_forge) -> None:
    """Writing both seats to one path survived, and every matchup would have
    been a mirror match with two deck names in the transcript."""
    mine = tmp_path / "mine.txt"
    mine.write_text("Commander: Jetmir, Nexus of Revels\n1 Sol Ring\n")
    theirs = tmp_path / "theirs.txt"
    theirs.write_text("Commander: Anowon, the Ruin Thief\n1 Swamp\n")

    planned = matchmod.plan(
        [
            matchmod.SeatSpec(seat="A", deck=str(mine), controller="forge"),
            matchmod.SeatSpec(seat="B", deck=str(theirs), controller="forge"),
        ]
    )

    assert planned.deck_paths["A"] != planned.deck_paths["B"]
    assert planned.deck_paths["A"].read_text() != planned.deck_paths["B"].read_text()
    assert "Jetmir" in planned.deck_paths["A"].read_text()
    assert "Anowon" in planned.deck_paths["B"].read_text()


def test_an_empty_routing_list_is_sent_rather_than_omitted(_isolated, deck_file) -> None:
    """Omitting the flag made GauntletMain fall back to its own default, so a
    seat asked to route nothing routed everything, at full price."""
    from gauntlet.engine import SeatConfig, build_command

    cmd = build_command(
        [SeatConfig(seat="A", deck_path=deck_file, bridge_endpoint="h:1", routed_kinds=())]
    )
    assert "--routed" in cmd
    assert "A=" in cmd


def test_provenance_is_recorded_so_a_result_can_be_reproduced(
    _isolated, deck_file, fake_forge
) -> None:
    """The decklist, the Forge version, the bridge revision and the routing
    policy are what make a transcript reproducible, and blanking any of them
    changed nothing."""
    import json as jsonlib
    import sqlite3

    from gauntlet import paths

    planned = matchmod.plan(
        [matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="forge")],
        seed=11,
    )
    matchmod.run(planned)

    conn = sqlite3.connect(paths.transcripts_db())
    row = conn.execute(
        "SELECT forge_version, bridge_revision, policy FROM matches WHERE id = ?",
        (planned.match_id,),
    ).fetchone()
    decklist = conn.execute(
        "SELECT decklist FROM seats WHERE match_id = ? AND seat = 'A'", (planned.match_id,)
    ).fetchone()[0]
    conn.close()

    forge_version, bridge_revision, policy = row
    assert forge_version, "no Forge version recorded"
    assert bridge_revision, "no bridge revision recorded"
    assert jsonlib.loads(policy)["routed"], "the routing policy was not recorded"

    cards = jsonlib.loads(decklist)
    assert cards["commanders"], "the deck played was not recorded"
    assert cards["main"]


def test_a_seat_exhausted_late_in_a_real_run_voids_it(
    _isolated, deck_file, fake_forge, monkeypatch
) -> None:
    """The one line copying the server's exhaustion into the result.

    Deleting it survived, and a seat that hit its session limit on decision 95
    of 100 left the fallback rate under the threshold, so the run read as valid
    with a clean win record. That is the incident this project exists for.
    """

    class _DiesLate(Seat):
        controller = "sdk"
        costs_money = True

        def __init__(self) -> None:
            self.n = 0

        def decide(self, request: Request, timeout: float) -> Response:
            self.n += 1
            if self.n > 18:
                raise SeatExhausted("session limit")
            return Response(id=request.id, choice=0, why="fine")

    monkeypatch.setattr(matchmod, "build_seats", lambda p: {"A": _DiesLate()})
    fake_forge["feed"] = 20

    result = matchmod.run(_plan(deck_file))

    assert result.exhausted, "the seat's exhaustion never reached the result"
    assert not result.trustworthy
    assert result.fallback_rate < 0.25, (
        "this test only means something while the rate stays under the threshold"
    )


def test_sweeping_against_all_never_includes_the_deck_itself(_isolated, monkeypatch) -> None:
    """--against all is the default and was untested.

    --deck takes a slug or a name, so excluding by slug alone made a deck named
    rather than slugged play itself, and the mirror's 50% went into the
    headline. The comment on that line says it already shipped once.
    """
    from dataclasses import dataclass

    from gauntlet import cli as climod

    @dataclass
    class _Row:
        slug: str
        name: str
        owner: str = "tester"
        commander: str = "X"
        card_count: int = 100

    rows = [
        _Row(slug="hawk-swarm", name="Feather Storm"),
        _Row(slug="other-deck", name="Something Else"),
    ]
    monkeypatch.setattr(climod.deckmod, "list_collection_decks", lambda **kw: rows)

    seen: dict = {}

    def capture(deck, opponents, **kw):
        seen["opponents"] = opponents
        return [], 0.1

    monkeypatch.setattr(climod.sweep, "timed_sweep", capture)

    # Named, not slugged, which is the case that used to slip through.
    runner.invoke(
        app,
        [
            "sweep",
            "--deck",
            "Feather Storm",
            "--against",
            "all",
            "--seat-a",
            "forge",
            "--seat-b",
            "forge",
        ],
    )
    assert "hawk-swarm" not in seen["opponents"], "the deck was swept against itself"
    assert seen["opponents"] == ["other-deck"]


def test_a_confirmed_run_is_capped_like_a_yes_flagged_one(
    _isolated, deck_file, monkeypatch
) -> None:
    """The --yes path was tested and the interactive-confirm path was not, so a
    user who types y could have got an uncapped run."""
    import types

    import typer

    from gauntlet import cli as climod

    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)
    # CliRunner replaces sys.stdin after any patch of the real one, so patch
    # what the module under test actually reads.
    monkeypatch.setattr(
        climod,
        "sys",
        types.SimpleNamespace(stdin=types.SimpleNamespace(isatty=lambda: True)),
    )

    seen: dict = {}

    def capture(plan_, *, budget=None, **kw):
        from gauntlet.server import MatchResult

        seen["budget"] = budget
        r = MatchResult(match_id=plan_.match_id)
        r.expected_seats = set()
        return r

    monkeypatch.setattr(matchmod, "run", capture)
    runner.invoke(
        app,
        [
            "run",
            "--a",
            str(deck_file),
            "--b",
            str(deck_file),
            "--seat-a",
            "api",
            "--seat-b",
            "forge",
            "--games",
            "1",
            "--max-cost",
            "2.75",
        ],
    )
    assert seen["budget"] is not None, "a confirmed run was uncapped"
    assert seen["budget"].max_usd == 2.75


def test_a_result_with_no_game_number_is_not_collapsed_into_one(_isolated) -> None:
    """Six numberless results used to dedupe into one, so a twenty-game run
    reported a single game."""
    from gauntlet.server import MatchServer
    from gauntlet.transcript import Transcript

    server = MatchServer(
        match_id="nonum",
        seats={},
        transcript=Transcript(_isolated / "t.db"),
    )
    for winner in ("A", "A", "B", "A", "B", "A"):
        server.record_game_result({"winner": winner, "draw": False, "turns": 10})

    assert len(server.result.games) == 6
    assert server.result.wins_by_seat() == {"A": 4, "B": 2}


def test_a_crashed_engine_is_not_recorded_as_finished(
    _isolated, deck_file, fake_forge, monkeypatch
) -> None:
    """An exception out of wait() propagates through the finally that writes
    the status, and the status used to be set optimistically up front."""
    import sqlite3

    from gauntlet import paths

    class _Exploding:
        on_line = staticmethod(lambda line: None)

        def wait(self, timeout=None):
            raise RuntimeError("the JVM vanished")

        def drain(self, timeout=None):
            return True

        def stop(self, grace: float = 5.0) -> None:
            pass

    monkeypatch.setattr(matchmod.engine, "launch", lambda *a, **kw: _Exploding())
    planned = matchmod.plan([matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="forge")])
    with pytest.raises(RuntimeError):
        matchmod.run(planned)

    conn = sqlite3.connect(paths.transcripts_db())
    status = conn.execute(
        "SELECT status FROM matches WHERE id = ?", (planned.match_id,)
    ).fetchone()[0]
    conn.close()
    assert status != "finished", "a match that died mid-run claims it completed"


@pytest.mark.parametrize(
    ("line", "expected_game"),
    [
        ('{"kind":"game_result","game":1,"winner":"A"}', 1),
        ('[main] INFO forge: {"kind":"game_result","game":2,"winner":"A"} ok', 2),
        ('noise {"unrelated":1} {"kind":"game_result","game":3,"winner":"B"}', 3),
        ('   {"kind":"game_result","game":4,"draw":true}   ', 4),
    ],
)
def test_a_result_is_found_whatever_shares_its_line(line: str, expected_game: int) -> None:
    """The only result channel a Forge-versus-Forge sweep has.

    Every fake engine emits a bare unprefixed line, which is the one input this
    function does not need to exist for. A regression drops every game and the
    sweep reports 0-0-0.
    """
    payload = matchmod._extract_result(line)
    assert payload is not None
    assert payload["game"] == expected_game


@pytest.mark.parametrize(
    "line",
    ["no json at all", '{"kind":"other","game":1}', "{ broken json", ""],
)
def test_a_line_that_is_not_a_result_is_not_mistaken_for_one(line: str) -> None:
    assert matchmod._extract_result(line) is None


def test_a_detached_seat_keeps_the_model_it_was_given(_isolated, deck_file) -> None:
    """The plan's `model` prices the budget and `options` picks the player.

    Only the first round-tripped under test, so a detached `--model opus` run
    could price opus and play haiku, or price haiku for an opus run and
    overshoot the cap fivefold. The foreground path is guarded and the detached
    one, which the docs tell you to use for agent play, was not.
    """
    from gauntlet.budget import Budget

    planned = matchmod.plan(
        [
            matchmod.SeatSpec(
                seat="A",
                deck=str(deck_file),
                controller="sdk",
                options={"model": "claude-opus-5"},
            ),
            matchmod.SeatSpec(seat="B", deck=str(deck_file), controller="forge"),
        ],
        budget=Budget(max_usd=5.0, model="claude-opus-5"),
    )
    back = matchmod.load_plan(matchmod._dump_plan(planned))

    assert back.specs[0].options == {"model": "claude-opus-5"}
    # And the seat built from it really uses that model, rather than its own
    # default.
    seats = matchmod.build_seats(back)
    assert seats["A"].model == "claude-opus-5"
    assert "B" not in seats, "a forge seat was given a python seat"


def test_a_seat_is_told_which_deck_it_is_piloting(_isolated, deck_file) -> None:
    """deck_note is how a paid seat knows its own plan. Dropping it survived."""
    planned = matchmod.plan([matchmod.SeatSpec(seat="A", deck=str(deck_file), controller="sdk")])
    seats = matchmod.build_seats(planned)
    assert seats["A"].deck_note, "the seat was never told what deck it holds"


def test_deck_warnings_go_to_stderr_so_json_stays_parseable(_isolated, tmp_path, capsys) -> None:
    """A warning on stdout lands in the middle of `--json` output.

    That shipped once and is named in the comment on the line. Nothing looked
    at which stream it used.
    """
    short = tmp_path / "short.txt"
    short.write_text("Commander: Jetmir, Nexus of Revels\n1 Sol Ring\n")

    matchmod.plan([matchmod.SeatSpec(seat="A", deck=str(short), controller="forge")])

    captured = capsys.readouterr()
    assert "warning" in captured.err.lower(), "the deck warning was not printed at all"
    assert "warning" not in captured.out.lower(), (
        "a deck warning went to stdout, which corrupts --json"
    )
