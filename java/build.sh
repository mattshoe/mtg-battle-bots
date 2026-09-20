#!/usr/bin/env bash
# Builds the Gauntlet bridge against a pinned Forge release.
#
# Forge is not rebuilt. The bridge compiles against the shipped fat jar and is
# loaded ahead of it on the classpath, which keeps the upgrade story to "drop in
# a new Forge, recompile this, fix whatever the compiler complains about".
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")"
vendor="${GAUNTLET_FORGE_HOME:-$root/vendor}"

forge_jar="$(ls "$vendor"/forge-gui-desktop-*-jar-with-dependencies.jar 2>/dev/null | head -1)"
gson_jar="$(ls "$vendor"/lib/gson-*.jar 2>/dev/null | head -1)"

if [[ -z "$forge_jar" ]]; then
  echo "no Forge jar in $vendor - run scripts/fetch-forge.sh first" >&2
  exit 1
fi
if [[ -z "$gson_jar" ]]; then
  echo "no gson jar in $vendor/lib - run scripts/fetch-forge.sh first" >&2
  exit 1
fi

out="$here/build/classes"
rm -rf "$out"
mkdir -p "$out"

echo "forge: $(basename "$forge_jar")"
javac -nowarn -Xlint:-options -source 17 -target 17 \
  -cp "$forge_jar:$gson_jar" \
  -d "$out" \
  $(find "$here/src" -name '*.java')

jar --create --file "$here/build/gauntlet-bridge.jar" -C "$out" .
echo "built $here/build/gauntlet-bridge.jar"
