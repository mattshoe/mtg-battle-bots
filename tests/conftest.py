"""Suite-wide guards.

Two rules the unit suite must obey, enforced rather than trusted.

Nothing reaches the network. A test that opens a socket to anywhere but
loopback, or imports a model SDK for real, could spend money, and a suite that
can spend money by accident is one nobody can run freely.

Nothing depends on this machine. The collection database, the Forge install and
the user's XDG directories all differ between a laptop and a runner, and tests
that quietly skip in CI are tests that do not exist. A committed fixture stands
in for the collection so those paths run everywhere.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch, request):
    """Loopback only.

    The harness talks to itself over 127.0.0.1 constantly, so that stays open.
    Anything else is a model call or a download, and neither belongs in a unit
    test. Mark a test `network` to opt out.
    """
    if "network" in request.keywords:
        return

    real = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        # A unix socket cannot leave the machine, and the control plane is
        # built on them.
        if self.family == socket.AF_UNIX:
            return real(self, address, *args, **kwargs)

        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(
                f"a test tried to reach {host!r}. Unit tests must not use the "
                f"network, and a call that leaves this machine may cost money."
            )
        return real(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def _hermetic_paths(tmp_path_factory, monkeypatch):
    """Point every machine-specific location at a temporary one.

    Without this the suite reads the developer's real collection and writes to
    their real transcript database, so it behaves differently on a laptop than
    on a runner, and the difference hides bugs in whichever direction is less
    often run.
    """
    root = tmp_path_factory.mktemp("gauntlet-home")
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    # The committed fixture, so the collection paths run in CI rather than
    # skipping. A test that wants the real thing asks for it explicitly.
    monkeypatch.setenv("GAUNTLET_COLLECTION", str(FIXTURES))


@pytest.fixture
def real_collection(monkeypatch):
    """Opt back in to the developer's actual collection, when one exists."""
    from gauntlet import decks

    monkeypatch.delenv("GAUNTLET_COLLECTION", raising=False)
    found = decks._default_db_dir()
    if found is None:
        pytest.skip("no real collection on this machine")
    return found


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    """Keep every path this writes to inside tmp_path.

    Shared, because three test files drive the real entry points and all of
    them must write their decks, sockets and transcripts somewhere disposable.
    """
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
