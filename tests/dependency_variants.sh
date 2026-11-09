#!/bin/bash
# Remote-dependency dispatcher-path regressions. Dedicated build Mac only, test install required; local file:// fixtures.
set -uo pipefail
. "$(dirname "$0")/../scripts/bh-stage-lib.sh"
[ -f "$BH_PREFIX/TEST_MODE" ] || { echo 'test install required'; exit 2; }
bh_stage_new
trap bh_cleanup EXIT
mkdir -p "$STAGE/base"
(cd "$BH_ROOT/helper/tests/fixtures/probe-proj" && tar cf - .) | (cd "$STAGE/base" && tar xf -)
bh_stage_repo "$STAGE/base" variants >/dev/null
pass=0; fail=0
for variant in case-resolved case-pbx case-swift link-resolved link-pbx link-swift nested-name registry computed-local big-resolved big-pbx big-swift inconsistent-pin; do
  # Restore only this task-owned fixture to its original commit, serially.
  bh_git -C "$STAGE/variants-work" reset --hard -q HEAD~0
  bh_git -C "$STAGE/variants-work" clean -fdq
  /usr/bin/python3 -I - "$STAGE/variants-work" "$variant" <<'PY'
import json, os, pathlib, sys
root, variant = pathlib.Path(sys.argv[1]), sys.argv[2]
pin = json.dumps({"pins": [{"kind": "remoteSourceControl", "location": "https://example.invalid/a.git"}]})
remote = '.package(url: "https://example.invalid/a.git", from: "1.0.0")'
kind = variant.split('-')[-1]
name, content = {"resolved": ("Package.resolved", pin), "pbx": ("project.pbxproj", "XCRemoteSwiftPackageReference"),
                 "swift": ("Package.swift", remote)}.get(kind, ("Package.swift", remote))
# Extra manifests under a normal directory avoid modifying the buildable control project.
d = root / 'Deps'; d.mkdir(exist_ok=True)
if variant.startswith('case-'):
    name = {"resolved": "package.resolved", "pbx": "project.PBXPROJ", "swift": "package.swift"}[kind]
if variant.startswith('link-'):
    (d / 'payload.txt').write_text(content)
    (d / name).symlink_to('payload.txt')
else:
    if variant == 'nested-name':
        content = '.package(name: "Foo(bar(baz))", url: "https://example.invalid/a.git", from: "1.0.0")'
    elif variant == 'registry':
        content = '.package(id: "owner.pkg", from: "1.0.0")'
    elif variant == 'computed-local':
        content = '.package(path: computedPath)'
    elif variant == 'inconsistent-pin':
        name = 'Package.resolved'
        content = json.dumps({"pins": [{"kind": "localSourceControl", "location": "https://example.invalid/a.git"}]})
    if variant.startswith('big-'):
        content = ' ' * (8 * 1024 * 1024 + 1) + content
    (d / name).write_text(content)
print('  variant=' + variant + ' stored=Deps/' + name, flush=True)
PY
  bh_git_commit variants "$variant" >/dev/null
  sha=$(bh_publish variants)
  bh_new_job
  result=$(bh_dispatch variants "$sha" Probe.xcodeproj Probe --timeout 300)
  status=$(bh_json_get status <<<"$result")
  files=$(bh_json_get files <<<"$result")
  if [ "$status" = unsupported_dependencies ] && grep -q 'Deps/' <<<"$files" && ! sudo -n test -s "$JOB_DIR/logs/xcodebuild.log"; then
    echo "ok   - $variant refused before xcodebuild: $files"; pass=$((pass+1))
  else
    echo "FAIL - $variant: status=$status files=$files xcodebuild_log=$(sudo -n test -s "$JOB_DIR/logs/xcodebuild.log" && echo present || echo absent)"; fail=$((fail+1))
  fi
  # Remove the committed variant before creating the next one.
  bh_git -C "$STAGE/variants-work" reset --hard -q HEAD~1
 done
echo "# dependency variants pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
