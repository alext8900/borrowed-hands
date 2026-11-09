#!/bin/bash
# Prove the argv test rejects a runner that drops, moves, re-spells or contradicts SWIFTC_DISABLE_SANDBOX=YES.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
W=$(mktemp -d "${TMPDIR:-/tmp}/bh-macro-mutant.XXXXXXXX")
trap 'rm -rf "$W"' EXIT
OLD='"SWIFTC_DISABLE_SANDBOX=YES", "build"]'
mutant() {
  python3 - "$ROOT/sandbox/bh-runner" "$W/bh-runner-$1" "$OLD" "$2" <<'PY'
import sys
src, dst, old, new = sys.argv[1:]
s = open(src).read()
assert s.count(old) == 1
open(dst, 'w').write(s.replace(old, new, 1))
PY
}
mutant removed '"build"]'
mutant after-build '"build", "SWIFTC_DISABLE_SANDBOX=YES"]'
mutant other-swift-flags '"OTHER_SWIFT_FLAGS=$(inherited) -disable-sandbox", "build"]'
mutant contradicted '"SWIFTC_DISABLE_SANDBOX=YES", "SWIFTC_DISABLE_SANDBOX=NO", "build"]'
uv run --project "$ROOT/helper" pytest -q "$ROOT/helper/tests/test_runner_macros.py"
rc=0
for m in removed after-build other-swift-flags contradicted; do
  if BH_TEST_RUNNER="$W/bh-runner-$m" uv run --project "$ROOT/helper" pytest -q "$ROOT/helper/tests/test_runner_macros.py" > "$W/result" 2>&1; then
    echo "SURVIVED: $m"; rc=1
  elif grep -q 'AssertionError' "$W/result"; then
    echo "KILLED: $m"
  else
    cat "$W/result"; echo "Mutant $m failed for an unrelated reason"; rc=1
  fi
done
exit $rc
