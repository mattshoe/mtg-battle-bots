"""The java command line, and the process around it.

`build_command` is the seam between this harness and Forge. Everything about a
run is expressed there, and a mistake in it is a run that does not do what the
caller asked while looking like it did. It is a pure function, so there is no
excuse for it being untested.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gauntlet.engine import SeatConfig, build_command

pytestmark = pytest.mark.usefixtures("_fake_jars")


@pytest.fixture
def _fake_jars(tmp_path, monkeypatch):
    """Stand in for a Forge install so these tests need no 300 MB download."""
    from gauntlet import engine, paths

    vendor = tmp_path / "vendor"
    (vendor / "lib").mkdir(parents=True)
    forge = vendor / "forge-gui-desktop-2.0.14-jar-with-dependencies.jar"
    gson = vendor / "lib" / "gson-2.11.0.jar"
    bridge = tmp_path / "bridge.jar"
    for f in (forge, gson, bridge):
        f.write_text("")

    monkeypatch.setattr(paths, "forge_jar", lambda: forge)
    monkeypatch.setattr(paths, "gson_jar", lambda: gson)
    monkeypatch.setattr(paths, "bridge_jar", lambda: bridge)
    monkeypatch.setattr(engine.paths, "forge_jar", lambda: forge)
    monkeypatch.setattr(engine.paths, "gson_jar", lambda: gson)
    monkeypatch.setattr(engine.paths, "bridge_jar", lambda: bridge)
    return vendor


def _seats(**kw):
    a = SeatConfig(seat="A", deck_path=Path("/decks/a.dck"), **kw)
    b = SeatConfig(seat="B", deck_path=Path("/decks/b.dck"))
    return [a, b]


def test_every_seat_gets_its_deck() -> None:
    cmd = build_command(_seats())
    assert "--deck" in cmd
    assert "A=/decks/a.dck" in cmd
    assert "B=/decks/b.dck" in cmd


def test_a_seat_without_a_bridge_gets_no_bridge_flag() -> None:
    """A forge seat is played inside the JVM. Handing it a bridge would open a
    connection nothing answers, and every decision would time out."""
    cmd = build_command(_seats())
    assert "--bridge" not in cmd
    assert "--routed" not in cmd


def test_a_bridged_seat_gets_its_endpoint_and_kinds() -> None:
    cmd = build_command(_seats(bridge_endpoint="127.0.0.1:9999", routed_kinds=("attack",)))
    assert "A=127.0.0.1:9999" in cmd
    assert "A=attack" in cmd
    # Seat B was given neither, and must not inherit A's.
    assert not any(c.startswith("B=127.0.0.1") for c in cmd)


def test_seat_order_does_not_depend_on_dict_iteration() -> None:
    """Seats are sorted, so the same run produces the same command twice.

    Forge assigns turn order from the order players are registered, so an
    unstable ordering would make a seeded run unreproducible.
    """
    forward = build_command(_seats())
    backward = build_command(list(reversed(_seats())))
    assert forward == backward
    assert forward.index("A=/decks/a.dck") < forward.index("B=/decks/b.dck")


def test_seed_is_passed_through_and_omitted_when_absent() -> None:
    assert "--seed" not in build_command(_seats())
    cmd = build_command(_seats(), seed=4242)
    assert cmd[cmd.index("--seed") + 1] == "4242"


@pytest.mark.parametrize(
    ("kwarg", "flag", "value"),
    [
        ("games", "--games", 7),
        ("decision_timeout", "--decision-timeout", 45),
        ("game_timeout", "--game-timeout", 600),
        ("game_format", "--format", "Commander"),
    ],
)
def test_run_settings_reach_the_command_line(kwarg, flag, value) -> None:
    cmd = build_command(_seats(), **{kwarg: value})
    assert cmd[cmd.index(flag) + 1] == str(value)


def test_bridge_jar_precedes_forge_on_the_classpath() -> None:
    """The bridge overrides Forge classes, so it has to be found first."""
    cmd = build_command(_seats())
    classpath = cmd[cmd.index("-cp") + 1].split(os.pathsep)
    assert "bridge.jar" in classpath[0]
    assert any("forge-gui-desktop" in p for p in classpath)
    assert any("gson" in p for p in classpath)


def test_heap_is_set_because_forge_needs_it() -> None:
    cmd = build_command(_seats(), heap="8g")
    assert "-Xmx8g" in cmd
    assert cmd[0] == "java"
    assert "forge.gauntlet.GauntletMain" in cmd


def test_the_command_is_a_list_of_strings_subprocess_can_take() -> None:
    """No Path objects, no ints. subprocess wants strings and a stray Path here
    fails at launch rather than at build time."""
    cmd = build_command(_seats(), seed=99, games=3)
    assert all(isinstance(part, str) for part in cmd), cmd


@pytest.fixture(autouse=True)
def _forge_home(tmp_path, monkeypatch):
    """Somewhere to launch from, since Forge is run from its own install root."""
    from gauntlet import engine

    home = tmp_path / "forge-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(engine.paths, "vendor_dir", lambda: home)
    return home


# ------------------------------------------- the process, not just the command

# Everything below build_command was untested, and every mutation survived:
# losing the Forge working directory, never draining the log pump, deleting the
# kill escalation, dropping the noise filter. Each is a documented past
# incident. These use short-lived shell commands, no JVM.


def test_output_reaches_the_callback_and_the_log(tmp_path) -> None:
    from gauntlet.engine import launch

    seen: list[str] = []
    log = tmp_path / "forge.log"
    run = launch(["/bin/sh", "-c", "echo one; echo two"], log, on_line=seen.append)

    assert run.wait(timeout=10) == 0
    assert seen == ["one", "two"]
    assert "one" in log.read_text()


def test_wait_does_not_return_until_the_output_is_read(tmp_path) -> None:
    """The process exiting is not the output being handled.

    A slow callback, which an ordinary contended sqlite write is, dropped
    results Forge had already printed and left a six-game run reporting one.
    """
    import time

    from gauntlet.engine import launch

    seen: list[str] = []

    def slow(line: str) -> None:
        time.sleep(0.15)
        seen.append(line)

    run = launch(["/bin/sh", "-c", "echo a; echo b; echo c"], tmp_path / "l.log", on_line=slow)
    run.wait(timeout=10)
    assert seen == ["a", "b", "c"], f"output was dropped: {seen}"


def test_a_callback_that_raises_does_not_kill_the_pump(tmp_path) -> None:
    """If the pump dies, Forge blocks on a full pipe buffer and the match hangs
    forever, which is the deadlock the thread exists to prevent."""
    from gauntlet.engine import launch

    seen: list[str] = []

    def explode(line: str) -> None:
        if line == "a":
            raise RuntimeError("database is locked")
        seen.append(line)

    run = launch(["/bin/sh", "-c", "echo a; echo b; echo c"], tmp_path / "l.log", on_line=explode)
    run.wait(timeout=10)
    assert seen == ["b", "c"], "one bad line killed the reader"
    assert run.drain(timeout=5)


def test_noise_forge_prints_on_every_start_is_filtered(tmp_path) -> None:
    from gauntlet.engine import launch

    seen: list[str] = []
    run = launch(
        ["/bin/sh", "-c", "echo 'Read cards: 33617 files'; echo 'real line'"],
        tmp_path / "l.log",
        on_line=seen.append,
    )
    run.wait(timeout=10)
    assert seen == ["real line"]


def test_stop_escalates_to_a_kill(tmp_path) -> None:
    """A hung JVM is the normal reason a match will not end, so the escalation
    is not optional. Deleting kill() survived."""
    from gauntlet.engine import launch

    # Ignores SIGTERM, so only kill() ends it.
    run = launch(["/bin/sh", "-c", "trap '' TERM; sleep 30"], tmp_path / "l.log")
    assert run.running
    run.stop(grace=0.5)
    assert not run.running, "a process that ignored terminate was never killed"


def test_forge_runs_from_its_own_install_directory(tmp_path, _forge_home) -> None:
    """Forge reads card data relative to the working directory. Getting this
    wrong fails several frames deep as a missing resource bundle."""
    from gauntlet.engine import launch

    seen: list[str] = []
    run = launch(["/bin/sh", "-c", "pwd"], tmp_path / "l.log", on_line=seen.append)
    run.wait(timeout=10)
    assert seen and seen[0].endswith("forge-home")


def test_trace_is_passed_to_the_child_only_when_asked(tmp_path) -> None:
    from gauntlet.engine import launch

    for trace, expected in ((True, "1"), (False, "")):
        seen: list[str] = []
        run = launch(
            ["/bin/sh", "-c", "echo ${GAUNTLET_TRACE:-}"],
            tmp_path / f"l{trace}.log",
            on_line=seen.append,
            trace=trace,
        )
        run.wait(timeout=10)
        assert seen == [expected] if expected else seen in ([], [""])


def test_a_missing_forge_install_says_so(tmp_path, monkeypatch) -> None:
    """It used to fail as a FileNotFoundError naming the java binary, which
    sends you looking in the wrong place entirely."""
    from gauntlet import engine
    from gauntlet.engine import launch

    monkeypatch.setattr(engine.paths, "vendor_dir", lambda: tmp_path / "not-here")
    with pytest.raises(FileNotFoundError, match="fetch-forge"):
        launch(["/bin/sh", "-c", "true"], tmp_path / "l.log")
