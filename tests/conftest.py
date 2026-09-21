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
