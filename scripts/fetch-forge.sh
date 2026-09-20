#!/usr/bin/env bash
# Downloads and pins the Forge release the bridge is built against.
#
# Forge is not vendored into git. It is 300 MB of someone else's GPL code plus
# card data, and pinning it here rather than committing it keeps the upgrade an
# explicit act: change FORGE_VERSION, run this, rebuild the bridge, run the
# tests. Anything that breaks, breaks visibly at that point.
set -euo pipefail

FORGE_VERSION="${FORGE_VERSION:-2.0.14}"
GSON_VERSION="${GSON_VERSION:-2.11.0}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")"
vendor="${GAUNTLET_FORGE_HOME:-$root/vendor}"

mkdir -p "$vendor/lib"
cd "$vendor"

jar="forge-gui-desktop-${FORGE_VERSION}-jar-with-dependencies.jar"
tarball="forge-installer-${FORGE_VERSION}.tar.bz2"
url="https://github.com/Card-Forge/forge/releases/download/forge-${FORGE_VERSION}/${tarball}"

if [[ -f "$jar" && -d res ]]; then
  echo "forge ${FORGE_VERSION} already in $vendor"
else
  echo "fetching forge ${FORGE_VERSION} (~300 MB)"
  curl -fL --progress-bar -o "$tarball" "$url"
  echo "extracting"
  tar -xjf "$tarball"
  rm -f "$tarball"
  if [[ ! -f "$jar" ]]; then
    echo "expected $jar in the archive, got:" >&2
    ls >&2
    exit 1
  fi
fi

gson="lib/gson-${GSON_VERSION}.jar"
if [[ -f "$gson" ]]; then
  echo "gson ${GSON_VERSION} already present"
else
  echo "fetching gson ${GSON_VERSION}"
  curl -fL --progress-bar -o "$gson" \
    "https://repo1.maven.org/maven2/com/google/code/gson/gson/${GSON_VERSION}/gson-${GSON_VERSION}.jar"
fi

# Forge reads card data through paths relative to its own install root, so the
# res directory has to sit next to the jar. A missing one fails later as an
# unhelpful missing resource bundle.
if [[ ! -d res ]]; then
  echo "no res/ directory in $vendor, the install is incomplete" >&2
  exit 1
fi

# Forge ships card scripts as a zip, with loose .txt only in older builds.
if [[ -f res/cardsfolder/cardsfolder.zip ]]; then
  cards=$(unzip -l res/cardsfolder/cardsfolder.zip '*.txt' 2>/dev/null | tail -1 | awk '{print $2}')
else
  cards=$(find res/cardsfolder -name '*.txt' 2>/dev/null | wc -l | tr -d ' ')
fi
echo
echo "forge ${FORGE_VERSION} in $vendor"
echo "${cards:-unknown} card scripts"
echo
echo "next: $root/java/build.sh"
