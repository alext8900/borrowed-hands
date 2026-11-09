#!/bin/bash
# Prove the exact-destination test rejects a runner that accepts any Simulator destination.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
W=$(mktemp -d "${TMPDIR:-/tmp}/bh-simulator-mutant.XXXXXXXX")
trap 'rm -rf "$W"' EXIT
python3 - "$ROOT/sandbox/bh-runner" "$W/bh-runner" <<'PY'
import sys
src, dst = sys.argv[1:]
s = open(src).read()
old = 'if job["destination"] not in (DESTINATION, "generic/platform=iOS Simulator"):'
new = 'if job["destination"] != DESTINATION and "Simulator" not in job["destination"]:'
assert s.count(old) == 1
open(dst, 'w').write(s.replace(old, new, 1))
PY
uv run --project "$ROOT/helper" pytest -q "$ROOT/helper/tests/test_runner_destination.py"
if BH_TEST_RUNNER="$W/bh-runner" uv run --project "$ROOT/helper" pytest -q "$ROOT/helper/tests/test_runner_destination.py" > "$W/result" 2>&1; then
  echo 'SURVIVED: any Simulator destination mutant'
  exit 1
fi
cat "$W/result"
grep -q 'Failed: DID NOT RAISE' "$W/result" || { echo 'Mutant failed for an unrelated reason'; exit 1; }
echo 'KILLED: any Simulator destination mutant'
