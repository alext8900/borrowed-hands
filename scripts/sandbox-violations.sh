#!/bin/bash
# Build an app through bh-dispatch under an installed profile and list the sandbox denials it hit.
#   scripts/sandbox-violations.sh <profile-name|builder.sb> [sample|hostile]
# Run on the build Mac. Denials come from the kernel's Sandbox log lines for this time window,
# limited to processes that were bh-builder's (matched by the pids seen in the job's log lines
# is not possible after the fact, so we filter by the known build tool names plus anything whose
# denial mentions the job dir).
set -uo pipefail
. "$(dirname "$0")/bh-stage-lib.sh"
PROF=${1:-builder.sb}; APP=${2:-sample}
trap bh_cleanup EXIT
bh_stage_new
if [ "$APP" = sample ]; then
  SHA=$(bh_stage_repo "$BH_ROOT/helper/tests/fixtures/probe-proj" sample); P=Probe.xcodeproj; S=Probe
else
  SHA=$(bh_stage_repo "$BH_ROOT/tests/hostile-app" sample); P=Hostile.xcodeproj; S=Hostile
fi
START=$(date '+%Y-%m-%d %H:%M:%S')
bh_new_job
arg=(); [ "$PROF" != builder.sb ] && arg=(--profile "$PROF")
R=$(bh_dispatch sample "$SHA" "$P" "$S" ${arg[@]+"${arg[@]}"} --timeout 600)
echo "status=$(bh_json_get status <<<"$R") duration=$(bh_json_get duration_s <<<"$R")"
[ "$(bh_json_get status <<<"$R")" = succeeded ] || bh_job_log | grep -E 'error|fail|denied' | head -20
sleep 2
# Lines look like: Sandbox: xcodebuild(123) deny(1) mach-lookup com.apple.foo
/usr/bin/log show --start "$START" --style compact --predicate 'sender == "Sandbox"' 2>/dev/null |
  grep -oE 'Sandbox: [^(]+\([0-9]+\) deny\([0-9]+\) [a-z*-]+ .*' |
  sed -E 's/^Sandbox: ([^(]+)\([0-9]+\) deny\([0-9]+\) ([a-z*-]+) (.*)$/\2 \3	[\1]/' |
  sed -E "s#$JOB_DIR#<JOB>#g" | sort | uniq -c | sort -rn | head -${MAXLINES:-80}
