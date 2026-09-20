# Install

From a fresh clone to a game you can run. Four steps, roughly ten minutes, most
of it downloading Forge.

## Prerequisites

| What | Version | Why |
|---|---|---|
| Java | 17 or newer, JDK not JRE | `java/build.sh` compiles with `-source 17 -target 17`, so a JRE is not enough |
| Python | 3.11 or newer | `requires-python = ">=3.11"` in `pyproject.toml` |
| `uv` | any recent | runs the CLI and manages the virtualenv |
| `curl`, `tar`, `bash` | any | `scripts/fetch-forge.sh` uses all three |
| Disk | about 1.5 GB peak, 1 GB after | 753 MB of Forge, a 300 MB tarball deleted after extraction, 250 MB of virtualenv |

Check Java first. A JRE passes `java -version` and fails `javac`.

```bash
java -version
javac -version
```

## 1. Fetch Forge

```bash
scripts/fetch-forge.sh
```

Downloads the pinned Forge release from GitHub, extracts it into `vendor/`, and
fetches gson from Maven Central. Two to five minutes on a fast connection, the
download is about 300 MB.

The version is pinned in the script.

```bash
FORGE_VERSION="${FORGE_VERSION:-2.0.14}"
GSON_VERSION="${GSON_VERSION:-2.11.0}"
```

Both are overridable from the environment. Changing `FORGE_VERSION` is a
deliberate act, see `docs/EXTENDING.md`.

The script is idempotent. If `vendor/` already holds the jar and a `res/`
directory it prints a line and exits. Nothing in `vendor/` is committed to git.

When it finishes it prints where Forge landed and a card script count.

```
forge 2.0.14 in /path/to/mtg-gauntlet/vendor
0 card scripts
```

`0 card scripts` is expected and not a failure. The script counts loose `.txt`
files under `res/cardsfolder`, and the release ships them as
`res/cardsfolder/cardsfolder.zip`. Forge reads the zip. Use `gauntlet doctor`
to confirm the install rather than that line.

## 2. Build the bridge

```bash
java/build.sh
```

Compiles the five Java files under `java/src/` against the Forge fat jar and
gson, then packs them into `java/build/gauntlet-bridge.jar`. Under a minute.
Forge itself is never rebuilt, the bridge just loads ahead of it on the
classpath.

## 3. Install the Python package

```bash
uv sync
```

Creates `.venv` and installs `typer` and `claude-agent-sdk`. One to two minutes
the first time. After this, run everything through `uv run`.

```bash
uv run gauntlet --help
```

## 4. Check the install

```bash
uv run gauntlet doctor
```

Every line must say `ok`. A failure exits 1.

```
forge
  ok    install: /path/to/mtg-gauntlet/vendor
  ok    jar: forge-gui-desktop-2.0.14-jar-with-dependencies.jar
  ok    gson: gson-2.11.0.jar
  ok    card data: 27 resource dirs
bridge
  ok    jar: gauntlet-bridge.jar
java
  ok    runtime: openjdk version "21.0.6" 2025-01-21
collection
  ok    decks: 32 decks
storage
  ok    transcripts: /home/you/.local/share/gauntlet/transcripts.db
  ok    state: /home/you/.local/state/gauntlet
```

The `collection` check needs the collection database. Without it that line
fails and everything else passes, which means you can play decks from text
files but not by slug.

## Sharing one Forge install

`GAUNTLET_FORGE_HOME` points at a Forge install outside the repo. Two checkouts
then share one 753 MB directory.

```bash
export GAUNTLET_FORGE_HOME=~/forge-2.0.14
scripts/fetch-forge.sh
```

`scripts/fetch-forge.sh` and the Python side both honour it. `java/build.sh`
does not, it reads `$repo/vendor` directly, so a build with
`GAUNTLET_FORGE_HOME` set and no `vendor/` in the repo fails with `no Forge jar
in .../vendor`. Symlink `vendor` at the shared install as a workaround.

```bash
ln -s ~/forge-2.0.14 vendor
```

## Where things land

| Path | Holds | Override with |
|---|---|---|
| `vendor/` | the pinned Forge install | `GAUNTLET_FORGE_HOME` |
| `java/build/gauntlet-bridge.jar` | the compiled bridge | none |
| `~/.local/share/gauntlet/transcripts.db` | every match ever played | `XDG_DATA_HOME` |
| `~/.local/share/gauntlet/decks/<match-id>/` | the `.dck` files a match was played from | `XDG_DATA_HOME` |
| `~/.local/state/gauntlet/<match-id>.sock` | a live match's control socket | `XDG_STATE_HOME` |
| `~/.local/state/gauntlet/<match-id>.forge.log` | Forge's own stdout for that match | `XDG_STATE_HOME` |
| `~/.local/state/gauntlet/<match-id>.json` | endpoint, pid and the exact java command | `XDG_STATE_HOME` |

`vendor/` and `java/build/` are gitignored. The transcript database is not in
the repo at all.

## First game

Forge on both sides, no agent, no API cost. About a minute, most of it Forge
loading its card database.

```bash
uv run gauntlet run --a feather-storm --b temur-roar-precon \
  --seat-a forge --seat-b forge
```

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| `no Forge jar under .../vendor - run scripts/fetch-forge.sh` | step 1 not run, or `GAUNTLET_FORGE_HOME` points somewhere empty | run `scripts/fetch-forge.sh` |
| `no gson jar under .../vendor/lib` | the jar downloaded but gson did not | rerun `scripts/fetch-forge.sh`, it skips the big download |
| `no res/ directory in ..., the install is incomplete` | the tarball extracted partially | delete `vendor/` and rerun |
| `bridge not built at .../gauntlet-bridge.jar - run java/build.sh` | step 2 not run, or it failed | run `java/build.sh` and read the javac output |
| `javac: invalid target release: 17` | Java older than 17 | install a JDK 17 or newer |
| `collection database not found at ...` | the collection database is not on this machine | play from text decklists, pass a file path to `--a` and `--b` |
| `MissingResourceException: en-US` | Forge reads `res/` relative to the working directory | the harness launches the JVM with `cwd` set to the Forge install root. Seeing this means you ran the java command by hand from somewhere else |
| `HeadlessException` from `GuiDesktop` | `java.awt.headless` was set before the class loaded, it needs a screen device to compute UI scale | the bridge sets headless after `GuiBase.setInterface`. Seeing this means that ordering was changed |
| a match starts and nothing happens | both seats default to `interactive` and both need their own `gauntlet act` loop | pass `--seat-b forge`, or start the second loop |
| `no running match '...'` | the match ended, or it never came up | check `~/.local/state/gauntlet/<id>.forge.log` |

Forge prints a lot on startup. The harness filters the known noise out of the
log, so what is left in `<match-id>.forge.log` is worth reading.

## Development

```bash
uv run --with pytest python -m pytest -q
uv run --with ruff ruff check src/ tests/
```
