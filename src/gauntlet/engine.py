"""Starting and supervising the Forge process.

Forge is treated as a black box that plays Magic. This module knows how to
launch it, where it insists on being launched from, and how to notice when it
has stopped. It knows nothing about decisions.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

#: Forge loads localisation and card data through paths relative to the process
#: working directory, so it has to run from its own install root. Getting this
#: wrong fails as a missing resource bundle several frames deep.
_MUST_RUN_FROM_FORGE_HOME = True

#: Lines Forge prints on every start that say nothing. Filtered out of the log
#: so a real error is visible without scrolling past two hundred of these.
_NOISE = (
    "was not assigned to any set",
    "Upcoming set ",
    "Read cards:",
    "Language '",
    "(ThreadUtil first call)",
    "markAppIsDaemon",
)


@dataclass(slots=True)
class SeatConfig:
    """How one player is set up for a run."""

    seat: str
    deck_path: Path
    #: None means Forge's own AI plays it, and no bridge is opened.
    bridge_endpoint: str | None = None
    routed_kinds: tuple[str, ...] = ()


@dataclass(slots=True)
class ForgeRun:
    """A running Forge process."""

    process: subprocess.Popen
    log_path: Path
    _reader: threading.Thread | None = field(default=None, repr=False)

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait for Forge to exit *and* for its output to be fully read.

        The second half matters. The process exiting does not mean the pump has
        finished with the pipe, and a slow on_line callback dropped results that
        Forge had already printed, leaving a six-game run reporting one.
        """
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        self.drain(timeout=timeout)
        return code

    def drain(self, timeout: float | None = None) -> bool:
        """Block until every line Forge printed has been handled.

        Returns False if the reader is still going, which means some output was
        never processed and the run's record is incomplete.
        """
        if self._reader is None:
            return True
        self._reader.join(timeout if timeout is not None else 30)
        return not self._reader.is_alive()

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def stop(self, grace: float = 5.0) -> None:
        """Ask Forge to stop, then insist.

        A hung JVM is the normal reason a match will not end, so the escalation
        is not optional.
        """
        if not self.running:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=grace)


def build_command(
    seats: Iterable[SeatConfig],
    *,
    game_format: str = "Commander",
    seed: int | None = None,
    games: int = 1,
    decision_timeout: int = 300,
    game_timeout: int = 900,
    heap: str = "6g",
) -> list[str]:
    """The java command line for one run.

    Kept as a pure function so a test can assert on it and a person can copy it
    out of a transcript and run it by hand. Reproducing a match by hand is the
    first thing anyone does when a result looks wrong.
    """
    classpath = os.pathsep.join(
        str(p) for p in (paths.bridge_jar(), paths.forge_jar(), paths.gson_jar())
    )
    cmd = [
        "java",
        f"-Xmx{heap}",
        "-cp",
        classpath,
        "forge.gauntlet.GauntletMain",
        "--format",
        game_format,
        "--games",
        str(games),
        "--decision-timeout",
        str(decision_timeout),
        "--game-timeout",
        str(game_timeout),
    ]
    if seed is not None:
        cmd += ["--seed", str(seed)]

    for s in sorted(seats, key=lambda c: c.seat):
        cmd += ["--deck", f"{s.seat}={s.deck_path}"]
        if s.bridge_endpoint:
            cmd += ["--bridge", f"{s.seat}={s.bridge_endpoint}"]
            # Always sent, even when empty. Omitting it let GauntletMain fall
            # back to its own default, so a seat asked to route nothing routed
            # everything, which is the opposite of what the caller said.
            cmd += ["--routed", f"{s.seat}={','.join(s.routed_kinds)}"]
    return cmd


def launch(
    cmd: list[str],
    log_path: Path,
    *,
    on_line: Callable[[str], None] | None = None,
    trace: bool = False,
) -> ForgeRun:
    """Start Forge and pump its output into a log file.

    Output is drained on a thread rather than left in the pipe. Forge is chatty,
    and a full pipe buffer deadlocks the game with no indication of why.
    """
    env = dict(os.environ)
    if trace:
        env["GAUNTLET_TRACE"] = "1"

    workdir = paths.vendor_dir() if _MUST_RUN_FROM_FORGE_HOME else None
    if workdir is not None and not workdir.is_dir():
        raise FileNotFoundError(
            f"Forge's install directory is missing: {workdir}. "
            "Run scripts/fetch-forge.sh, or set GAUNTLET_FORGE_HOME."
        )

    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")

    process = subprocess.Popen(
        cmd,
        cwd=str(workdir) if workdir is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        bufsize=1,
    )

    def pump() -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip("\n")
                if any(n in line for n in _NOISE):
                    continue
                handle.write(line + "\n")
                handle.flush()
                if on_line is not None:
                    # Guarded. This thread's real job is draining the pipe, and
                    # if it dies Forge blocks on a full buffer and the match
                    # hangs forever. A callback that raises must cost its own
                    # line, not the run.
                    try:
                        on_line(line)
                    except Exception as exc:
                        handle.write(f"[gauntlet] on_line failed: {exc!r}\n")
                        handle.flush()
        finally:
            handle.close()

    reader = threading.Thread(target=pump, name="forge-log", daemon=True)
    reader.start()
    return ForgeRun(process=process, log_path=log_path, _reader=reader)
