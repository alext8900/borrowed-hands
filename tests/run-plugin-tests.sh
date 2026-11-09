#!/bin/bash
# Borrowed Hands plugin + protocol tests. Runs with the Hermes venv so tests see the same Python,
# cryptography and aiohttp as the gateway.
#     HERMES_SRC=<hermes checkout at v0.22.7> tests/run-plugin-tests.sh [pytest args]
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
: "${HERMES_SRC:?set HERMES_SRC to a Hermes checkout at v0.22.7 (b200f5bc6) with a .venv}"
PY="$HERMES_SRC/.venv/bin/python"
[ -x "$PY" ] || { echo "no venv python at $PY" >&2; exit 2; }
TMP=$(mktemp -d "${TMPDIR:-/tmp}/bh-plugin-tests.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
export HERMES_HOME="$TMP/hermes-home"
mkdir -p "$HERMES_HOME"
export HERMES_SRC BH_TEST_TMP="$TMP"
cd "$ROOT"
exec "$PY" -I -B -m pytest -p no:cacheprovider tests/plugin tests/protocol -q "$@"
