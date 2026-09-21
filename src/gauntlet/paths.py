"""Where everything lives.

One module so that a machine with an unusual layout has exactly one file to
change, and so nothing else in the package has to know about XDG.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent


def _xdg(var: str, default: str) -> Path:
    raw = os.environ.get(var)
    return Path(raw) if raw else Path.home() / default


def data_dir() -> Path:
    d = _xdg("XDG_DATA_HOME", ".local/share") / "gauntlet"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_dir() -> Path:
    """Live match sockets and pid files. Disposable, unlike data."""
    d = _xdg("XDG_STATE_HOME", ".local/state") / "gauntlet"
    d.mkdir(parents=True, exist_ok=True)
    return d


def vendor_dir() -> Path:
    """The pinned Forge install.

    Overridable because a shared machine may want one Forge for several
    checkouts, and because 300 MB per clone is not reasonable.
    """
    raw = os.environ.get("GAUNTLET_FORGE_HOME")
    return Path(raw).expanduser() if raw else REPO_ROOT / "vendor"


def forge_jar() -> Path:
    jars = sorted(vendor_dir().glob("forge-gui-desktop-*-jar-with-dependencies.jar"))
    if not jars:
        raise FileNotFoundError(f"no Forge jar under {vendor_dir()} - run scripts/fetch-forge.sh")
    return jars[-1]


def gson_jar() -> Path:
    jars = sorted((vendor_dir() / "lib").glob("gson-*.jar"))
    if not jars:
        raise FileNotFoundError(
            f"no gson jar under {vendor_dir() / 'lib'} - run scripts/fetch-forge.sh"
        )
    return jars[-1]


def bridge_jar() -> Path:
    jar = REPO_ROOT / "java" / "build" / "gauntlet-bridge.jar"
    if not jar.exists():
        raise FileNotFoundError(f"bridge not built at {jar} - run java/build.sh")
    return jar


def forge_version() -> str:
    """Read off the jar name, which is the only place it is reliably recorded."""
    name = forge_jar().name
    parts = name.split("-")
    return parts[3] if len(parts) > 3 else "unknown"


def transcripts_db() -> Path:
    return data_dir() / "transcripts.db"


#: sockaddr_un.sun_path is 104 bytes on macOS and 108 on Linux. Going over it
#: fails as a bare OSError from bind(), several frames from anything that names
#: the real problem.
_SOCKET_PATH_LIMIT = 100


def match_socket(match_id: str) -> Path:
    """Where a match listens for `gauntlet act`.

    Normally beside the rest of the match's state. When that path would be too
    long for a unix socket, somewhere short instead, because a deep state
    directory should not be the reason a match cannot start.
    """
    natural = state_dir() / f"{match_id}.sock"
    if len(str(natural).encode()) <= _SOCKET_PATH_LIMIT:
        return natural

    import hashlib
    import os
    import tempfile

    # Salted with the state directory, so two runs with different state
    # directories cannot land on the same socket. A fixed name here meant two
    # test runs, or two installs, silently sharing one match's control plane.
    salt = hashlib.sha1(f"{state_dir()}:{os.getpid()}".encode(), usedforsecurity=False).hexdigest()[
        :8
    ]
    short = Path(tempfile.gettempdir()) / f"gauntlet-{salt}-{match_id}.sock"
    if len(str(short).encode()) > _SOCKET_PATH_LIMIT:
        raise OSError(
            f"no socket path short enough for {match_id}: "
            f"{natural} and {short} both exceed {_SOCKET_PATH_LIMIT} bytes"
        )
    return short


def match_meta(match_id: str) -> Path:
    return state_dir() / f"{match_id}.json"


def deck_cache() -> Path:
    """Exported .dck files. Forge loads decks from disk, so they have to land
    somewhere, and keeping them lets a transcript be re-run against the exact
    list that was played."""
    d = data_dir() / "decks"
    d.mkdir(parents=True, exist_ok=True)
    return d
