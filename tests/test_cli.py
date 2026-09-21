"""The command line, driven through typer's runner.

Nothing here starts a JVM or spends anything. The commands that would are
stopped at the cost gate or have their orchestration replaced, which is itself
worth asserting: a test that accidentally played a game would be a bug in the
safeguards.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from gauntlet.cli import app

runner = CliRunner()


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    from gauntlet import paths

    monkeypatch.setattr(paths, "transcripts_db", lambda: tmp_path / "t.db")
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "deck_cache", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def deck_file(tmp_path):
    p = tmp_path / "d.txt"
    p.write_text(
        "Commander: Jetmir, Nexus of Revels\n"
        + "\n".join(f"1 Card {i}" for i in range(98))
        + "\n1 Sol Ring\n"
    )
    return p


# ----------------------------------------------------------------- basic shape


def test_bare_invocation_shows_help_rather_than_doing_something() -> None:
    """Click exits 2 when it shows help for a missing command, which is the
    convention. What matters is that it prints usage rather than acting."""
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "Usage" in result.output


@pytest.mark.parametrize(
    "command",
    [
        "decks",
        "export",
        "run",
        "act",
        "status",
        "stop",
        "sweep",
        "matches",
        "replay",
        "summary",
        "doctor",
    ],
)
def test_every_command_has_help(command: str) -> None:
    """Every command documents what it does.

    The previous version accepted "Usage" alone, which typer prints for
    everything, so it asserted only that --help did not crash.
    """
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output
    # Typer's options box alone is hundreds of characters, so a length check
    # passes for a command with no docstring. Look for the docstring itself.
    from gauntlet.cli import app as _app

    documented = {
        c.name or c.callback.__name__: (c.callback.__doc__ or "").strip()
        for c in _app.registered_commands
    }
    summary = documented.get(command, "")
    assert summary, f"{command} has no docstring"
    first_words = " ".join(summary.split()[:4])
    assert first_words.split()[0] in result.output, (
        f"{command}'s help does not show its own summary"
    )


# -------------------------------------------------------------- the cost gate


def test_a_paid_run_without_a_terminal_refuses(_isolated, deck_file) -> None:
    """CliRunner's stdin is not a tty, which is the scripted case. It must
    refuse rather than hang, and it must not start anything."""
    result = runner.invoke(
        app,
        [
            "run",
            "--a",
            str(deck_file),
            "--b",
            str(deck_file),
            "--seat-a",
            "sdk",
            "--seat-b",
            "sdk",
            "--games",
            "10",
        ],
    )
    assert result.exit_code != 0
    assert "no terminal" in result.output


def test_a_run_over_the_cap_refuses_even_with_yes(_isolated, deck_file) -> None:
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
            "api",
            "--games",
            "500",
            "--yes",
        ],
    )
    assert result.exit_code != 0
    assert "over the" in result.output
    assert "$5.00" in result.output


def test_the_projection_is_shown_before_anything_runs(_isolated, deck_file) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--a",
            str(deck_file),
            "--b",
            str(deck_file),
            "--seat-a",
            "sdk",
            "--seat-b",
            "sdk",
            "--games",
            "500",
            "--yes",
        ],
    )
    assert "decisions" in result.output
    assert "HARD CAP" in result.output


def test_a_sweep_over_the_cap_refuses(_isolated) -> None:
    result = runner.invoke(
        app,
        [
            "sweep",
            "--deck",
            "x",
            "--against",
            "a,b,c",
            "--games",
            "50",
            "--seat-a",
            "api",
            "--seat-b",
            "api",
            "--yes",
        ],
    )
    assert result.exit_code != 0
    assert "over the" in result.output


# ------------------------------------------------------------------ validation


def test_an_unknown_decision_kind_is_refused_with_the_known_ones(_isolated, deck_file) -> None:
    """A typo here routes nothing and looks like a slow agent hours later."""
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
            "--routed",
            "cast_or_pass,typo",
        ],
    )
    assert result.exit_code != 0
    assert "typo" in result.output
    assert "cast_or_pass" in result.output


def test_a_missing_deck_fails_before_starting_anything(_isolated) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--a",
            "no-such-deck-anywhere",
            "--b",
            "also-missing",
            "--seat-a",
            "forge",
            "--seat-b",
            "forge",
        ],
    )
    assert result.exit_code != 0


# --------------------------------------------------------------------- export


def test_export_writes_a_loadable_dck(_isolated, deck_file, tmp_path) -> None:
    out = tmp_path / "out.dck"
    result = runner.invoke(app, ["export", str(deck_file), "-o", str(out)])
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert "[metadata]" in text
    assert "[Commander]" in text
    assert "[Main]" in text


def test_export_warns_about_an_illegal_deck_without_refusing(_isolated, tmp_path) -> None:
    """A deck that is one card short is still worth testing, so a problem is a
    warning rather than a refusal."""
    short = tmp_path / "short.txt"
    short.write_text("Commander: Jetmir, Nexus of Revels\n1 Sol Ring\n")
    out = tmp_path / "short.dck"
    result = runner.invoke(app, ["export", str(short), "-o", str(out)])
    assert result.exit_code == 0
    assert out.exists()
    # The "warns" half of the name, which nothing checked.
    assert "warning" in result.output.lower()
    # Names the actual problem. Every successful export prints the word
    # "cards", so matching that asserted nothing.
    assert "Commander wants 100" in result.output


# ---------------------------------------------------------------- transcripts


def test_matches_lists_what_is_there_and_nothing_when_there_is_not(_isolated) -> None:
    """Asserting only the exit code would pass on garbage output."""
    from gauntlet.transcript import Transcript

    empty = runner.invoke(app, ["matches"])
    assert empty.exit_code == 0
    assert not empty.output.strip()

    t = Transcript()
    t.start_match(
        match_id="listed-match",
        format="Commander",
        seed=5,
        seats=[{"seat": "A", "controller": "forge"}],
    )
    t.finish_match(match_id="listed-match")
    t.close()

    listed = runner.invoke(app, ["matches"])
    assert listed.exit_code == 0
    assert "listed-match" in listed.output
    assert "finished" in listed.output


def test_replay_of_an_unknown_match_does_not_crash(_isolated) -> None:
    result = runner.invoke(app, ["replay", "no-such-match"])
    assert result.exit_code == 0
    assert result.output.strip()


def test_summary_of_an_unknown_match_returns_json(_isolated) -> None:
    result = runner.invoke(app, ["summary", "no-such-match"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload.get("exists") is False


# ------------------------------------------------------------- live match ops


def test_acting_on_a_match_that_is_not_running_fails_clearly(_isolated) -> None:
    result = runner.invoke(app, ["act", "--match", "ghost", "--seat", "A"])
    assert result.exit_code != 0
    assert "ghost" in result.output


def test_status_of_a_dead_match_fails_clearly(_isolated) -> None:
    """Clearly, which the previous version did not check at all. It would have
    passed on a bare traceback."""
    result = runner.invoke(app, ["status", "--match", "ghost"])
    assert result.exit_code != 0
    assert "ghost" in result.output
    assert "Traceback" not in result.output


def test_stopping_a_dead_match_fails_clearly(_isolated) -> None:
    result = runner.invoke(app, ["stop", "--match", "ghost"])
    assert result.exit_code != 0
    assert "ghost" in result.output
    assert "Traceback" not in result.output


# -------------------------------------------------------------------- doctor


def test_doctor_reports_seat_readiness(_isolated, monkeypatch) -> None:
    """A missing key used to surface as a fallback on every decision, hours
    into a run. Doctor is where that should be visible instead.

    Asserted on the verdicts rather than the labels. The labels are static
    strings in doctor's own output, so the previous version passed on an
    install where every single check reported FAIL.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(app, ["doctor"])

    # forge is always available, and the api seat has no key here.
    assert "ok    forge" in result.output
    assert "FAIL  api" in result.output
    assert result.exit_code == 1, "doctor reported a failure and exited 0"


def test_doctor_passes_when_the_api_seat_has_a_key(_isolated, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    result = runner.invoke(app, ["doctor"])
    assert "ok    api" in result.output


def test_doctor_exits_nonzero_when_something_is_broken(_isolated, monkeypatch) -> None:
    from gauntlet import paths

    def missing():
        raise FileNotFoundError("no forge here")

    monkeypatch.setattr(paths, "forge_jar", missing)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL" in result.output
