#!/bin/bash
# Borrowed Hands sandbox: end-to-end tests. Run ON a dedicated build Mac as the admin owner,
# after `sudo bash sandbox/setup-bh-builder.sh --with-test-profiles`:
#     bash tests/run-tests.sh            # everything (~10-15 min)
#     bash tests/run-tests.sh quick      # validation + guard tests only
# Every build goes through `sudo bh-dispatch` (system-domain launchd job), never `sudo -u bh-builder`.
set -uo pipefail
. "$(dirname "$0")/../scripts/bh-stage-lib.sh"
MODE=${1:-all}
PASS=0; FAIL=0
ok() { PASS=$((PASS+1)); echo "ok   - $*"; }
no() { FAIL=$((FAIL+1)); echo "FAIL - $*"; }
check() { local d=$1; shift; if "$@"; then ok "$d"; else no "$d"; fi; }

# Sentinel: a process of the owner's uid. Every runner/dispatcher cleanup must leave it alive (d).
/bin/sleep 3600 & SENTINEL=$!
trap 'kill "$SENTINEL" 2>/dev/null; bh_cleanup' EXIT
sentinel_alive() { kill -0 "$SENTINEL" 2>/dev/null; }
# (launchd may be mid-spawn of an Apple agent via xpcproxy; give it a few seconds to settle)
no_builder_procs() {
  local i; for i in 1 2 3 4 5 6 7 8 9 10; do [ -z "$(bh_builder_procs)" ] && return 0; sleep 0.5; done
  echo "  leftover uid-470 processes:"; bh_builder_procs_all | sed 's/^/    /'; return 1
}
label_gone() { ! sudo -n launchctl print system/com.borrowedhands.runner >/dev/null 2>&1; }

# ---------------------------------------------------------------- ancestry, tree safety, remote dependencies, cancel, retention
l3_tests() {
  local R st SHA_OK SHA_MAIN sha rc
  mkdir -p "$STAGE/l3-src" && (cd "$BH_ROOT/helper/tests/fixtures/probe-proj" && tar cf - .) | (cd "$STAGE/l3-src" && tar xf -)
  SHA_OK=$(bh_stage_repo "$STAGE/l3-src" l3)
  bh_new_job; R=$(bh_dispatch l3 "$SHA_OK" Probe.xcodeproj Probe --timeout 600)
  check "ancestry: commit on bh/test builds ($(bh_json_get status <<<"$R"))" [ "$(bh_json_get status <<<"$R")" = succeeded ]
  local shape; shape="$(bh_json_get errors <<<"$R")|$(bh_json_get warnings <<<"$R")|$(bh_json_get error_count <<<"$R")|$(bh_json_get warning_count <<<"$R")"
  check "succeeded result carries errors [], warnings [], error_count, warning_count ($shape)" [ "$shape" = "[]|[]|0|0" ]
  # a commit only on main (bh/test not moved): not reachable from refs/heads/bh/*
  echo "// main only" >> "$STAGE/l3-work/Sources/Greeting.swift"; bh_git_commit l3 "main only" >/dev/null
  SHA_MAIN=$(BH_NO_BH_UPDATE=1 bh_publish l3)
  bh_new_job; R=$(bh_dispatch l3 "$SHA_MAIN" Probe.xcodeproj Probe --timeout 600)
  check "ancestry: commit only on main -> not_reachable ($(bh_json_get status <<<"$R"))" [ "$(bh_json_get status <<<"$R")" = not_reachable ]
  check "not_reachable: no build ran (no xcodebuild.log)" bash -c '! sudo -n test -s "$1/logs/xcodebuild.log"' _ "$JOB_DIR"
  bh_new_job; R=$(bh_dispatch l3 "0123456789abcdef0123456789abcdef01234567" Probe.xcodeproj Probe --timeout 600)
  check "nonexistent sha -> fetch_failed ($(bh_json_get status <<<"$R"))" [ "$(bh_json_get status <<<"$R")" = fetch_failed ]
  # tree safety (ported from gitmat): an escaping symlink and a .git lookalike, both on bh/test
  ln -s ../../outside "$STAGE/l3-work/escape"; bh_git_commit l3 "escaping symlink" >/dev/null; sha=$(bh_publish l3)
  bh_new_job; R=$(bh_dispatch l3 "$sha" Probe.xcodeproj Probe --timeout 600)
  check "escaping symlink -> unsafe_tree ($(bh_json_get status <<<"$R"))" [ "$(bh_json_get status <<<"$R")" = unsafe_tree ]
  rm "$STAGE/l3-work/escape"; ln -s Sources "$STAGE/l3-work/.git$(printf '\342\200\213')"
  bh_git_commit l3 "dotgit lookalike" >/dev/null; sha=$(bh_publish l3)
  bh_new_job; R=$(bh_dispatch l3 "$sha" Probe.xcodeproj Probe --timeout 600)
  check "'.git<ZWSP>' lookalike -> unsafe_tree ($(bh_json_get status <<<"$R"))" [ "$(bh_json_get status <<<"$R")" = unsafe_tree ]
  rm "$STAGE/l3-work/.git$(printf '\342\200\213')"; bh_git_commit l3 "clean" >/dev/null

  # remote Swift package dependencies are refused before any build; local packages build
  mkdir -p "$STAGE/l3-work/Probe.xcodeproj/project.xcworkspace/xcshareddata/swiftpm"
  cp "$BH_ROOT/tests/deps-apps/Package.resolved.remote" "$STAGE/l3-work/Probe.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved"
  bh_git_commit l3 "remote pin" >/dev/null; sha=$(bh_publish l3)
  bh_new_job; R=$(bh_dispatch l3 "$sha" Probe.xcodeproj Probe --timeout 600)
  echo "  remote deps resolved: files=$(bh_json_get files <<<"$R")"
  check "Package.resolved with a remote pin -> unsupported_dependencies ($(bh_json_get status <<<"$R"))" [ "$(bh_json_get status <<<"$R")" = unsupported_dependencies ]
  check "unsupported_dependencies lists the checkout-relative file" grep -q 'Probe.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved' <<<"$(bh_json_get files <<<"$R")"
  check "unsupported_dependencies message says to vendor as a local path package" grep -q 'local path package' <<<"$(bh_json_get message <<<"$R")"
  check "unsupported_dependencies: no build ran" bash -c '! sudo -n test -s "$1/logs/xcodebuild.log"' _ "$JOB_DIR"
  local d
  for d in remote-pbx local-pkg; do
    mkdir -p "$STAGE/$d-src" && (cd "$BH_ROOT/tests/deps-apps/$d" && tar cf - .) | (cd "$STAGE/$d-src" && tar xf -)
  done
  sha=$(bh_stage_repo "$STAGE/remote-pbx-src" rpbx)
  bh_new_job; R=$(bh_dispatch rpbx "$sha" RemoteDep.xcodeproj RemoteDep --timeout 600)
  echo "  remote deps pbxproj: files=$(bh_json_get files <<<"$R")"
  check "XCRemoteSwiftPackageReference -> unsupported_dependencies ($(bh_json_get status <<<"$R"))" \
    bash -c '[ "$1" = unsupported_dependencies ] && grep -q "RemoteDep.xcodeproj/project.pbxproj" <<<"$2"' _ "$(bh_json_get status <<<"$R")" "$(bh_json_get files <<<"$R")"
  sha=$(bh_stage_repo "$STAGE/local-pkg-src" lpkg)
  bh_new_job; R=$(bh_dispatch lpkg "$sha" LocalDep.xcodeproj LocalDep --timeout 600)
  check "local-path package still builds ($(bh_json_get status <<<"$R"): $(bh_json_get message <<<"$R" | cut -c1-80))" [ "$(bh_json_get status <<<"$R")" = succeeded ]

  # cancel from the owner's uid: SIGTERM to the helper's own `sudo bh-dispatch` child, both with the
  # child in the caller's process group and in its own session (PROTOCOL 4.6)
  mkdir -p "$STAGE/cancel-src" && (cd "$BH_ROOT/tests/hang-app" && tar cf - .) | (cd "$STAGE/cancel-src" && tar xf -)
  sha=$(bh_stage_repo "$STAGE/cancel-src" cancelr)
  local mode out
  for mode in sigterm-sudo sigterm-sudo-sess cancel-cmd; do
    bh_new_job; bh_job_json cancelr "$sha" Hang.xcodeproj Hang > "$STAGE/cancel-job.json"
    out=$(/usr/bin/python3 -I "$BH_ROOT/tests/cancel_owner.py" "$STAGE/cancel-job.json" "$mode" --allow-file-remote --timeout 600); rc=$?
    check "cancel ($mode): owner probe exit 0" [ "$rc" -eq 0 ]
    echo "  cancel[$mode]: $(bh_json_get status <<<"$out") in $(bh_json_get elapsed_s <<<"$out")s"
    check "cancel from uid $(id -u) ($mode) -> cancelled, signalled mid-build" \
      bash -c '[ "$1" = cancelled ] && [ "$2" = True ]' _ "$(bh_json_get status <<<"$out")" "$(bh_json_get cancelled_mid_build <<<"$out")"
    check "cancel ($mode): no bh-builder processes after" no_builder_procs
    check "cancel ($mode): label gone, sentinel alive" bash -c '! sudo -n launchctl print system/com.borrowedhands.runner >/dev/null 2>&1 && kill -0 "$1"' _ "$SENTINEL"
  done

  bh_new_job; bh_job_json cancelr "$sha" Hang.xcodeproj Hang > "$STAGE/cancel-job.json"
  out=$(/usr/bin/python3 -I "$BH_ROOT/tests/cancel_owner.py" "$STAGE/cancel-job.json" cancel-wrong-id --allow-file-remote --timeout 600); rc=$?
  check "wrong-id cancel: exit 0, live build preserved then cancelled" [ "$rc" -eq 0 ]
  echo "  cancel[wrong id]: $(bh_json_get cancel_reply <<<"$out" | cut -c1-160)"
  check "--cancel with another job's id answers not_running and leaves the build running" bash -c \
    'grep -q "not_running" <<<"$1" && grep -q "\"build_still_running\": true" <<<"$1"' _ "$out"
  check "after the wrong-id test: no bh-builder processes" no_builder_procs
  out=$(bh_uuid | (cd / && sudo -n "$BH_DISPATCH" --cancel)); rc=$?
  check "idle cancel: helper requires exit 0" [ "$rc" -eq 0 ]
  check "--cancel with nothing running -> not_running ($(bh_json_get status <<<"$out"))" [ "$(bh_json_get status <<<"$out")" = not_running ]
  out=$(echo "not-a-uuid" | (cd / && sudo -n "$BH_DISPATCH" --cancel))
  check "--cancel with a malformed id -> usage" [ "$(bh_json_get status <<<"$out")" = usage ]

  # retention: at most 20 job dirs after a job (the new one + the 19 newest older ones)
  local i u fakes=()
  for i in $(seq -w 1 25); do
    u=$(bh_uuid); fakes+=("$u")
    # future mtimes, one minute apart: the fakes are the newest dirs, in order fakes[0] (oldest) .. fakes[24]
    sudo -n /bin/sh -c "install -d -o 470 -g 470 -m 700 '$BH_JOBS/$u' '$BH_JOBS/$u/logs' && touch -h -t 2030123100$i '$BH_JOBS/$u'"
  done
  bh_new_job; R=$(bh_dispatch l3 "$SHA_OK" Probe.xcodeproj Probe --timeout 600)
  local left; left=$(sudo -n ls "$BH_JOBS" | grep -cE '^[0-9a-f]{8}-')
  echo "  retention: removed=$(bh_json_get dispatch.retention_removed <<<"$R") job_dirs_now=$left"
  check "retention: 20 job dirs remain after a job (got $left)" [ "$left" -eq 20 ]
  check "retention: the in-flight job's dir is kept" sudo -n test -d "$JOB_DIR"
  check "retention: the 19 newest older dirs are kept, the 6 oldest removed" bash -c '
    for u in "${@:2:6}"; do sudo -n test ! -e "$1/$u" || exit 1; done
    for u in "${@:8}"; do sudo -n test -d "$1/$u" || exit 1; done' _ "$BH_JOBS" "${fakes[@]}"
  check "retention: job still succeeded ($(bh_json_get status <<<"$R"))" [ "$(bh_json_get status <<<"$R")" = succeeded ]
  # remove the future-dated fakes now: left behind, they would make the next jobs' retention prune
  # newer dirs (e.g. the probe suite's "other job" bait) instead
  for u in "${fakes[@]}"; do sudo -n rm -rf "$BH_JOBS/$u"; done
}

# ---------------------------------------------------------------- budgets (MEDIUM 5, round-2 HIGH)
hog_mode() {  # hog_mode <mode>: commit that mode into the hog repo, print its sha
  echo "$1" > "$STAGE/hog-work/hog-mode"; bh_git_commit hog "mode $1" >/dev/null; bh_publish hog
}
budget_tests() {
  mkdir -p "$STAGE/hog-src" && (cd "$BH_ROOT/tests/hog-app" && tar cf - .) | (cd "$STAGE/hog-src" && tar xf -)
  bh_stage_repo "$STAGE/hog-src" hog >/dev/null
  local R st
  bh_new_job; R=$(bh_dispatch hog "$(hog_mode disk)" Hog.xcodeproj Hog --timeout 300 --disk-budget-mb 500)
  check "Run Script writing 3 GB with a 500 MB budget -> disk_budget ($(bh_json_get message <<<"$R"))" [ "$(bh_json_get status <<<"$R")" = disk_budget ]
  check "no bh-builder processes after disk_budget" no_builder_procs
  # an unlinked-but-open 3 GB file: invisible to du, visible to the dispatcher's statvfs
  bh_new_job; R=$(bh_dispatch hog "$(hog_mode unlinked)" Hog.xcodeproj Hog --timeout 300 --disk-budget-mb 1000)
  st=$(bh_json_get status <<<"$R")
  check "unlinked-but-open 3 GB file, 1000 MB budget -> disk_budget from the dispatcher ($st: $(bh_json_get message <<<"$R"))" \
    bash -c '[ "$1" = disk_budget ] && grep -q "dispatcher (statvfs)" <<<"$2"' _ "$st" "$R"
  check "no bh-builder processes after the unlinked-file hog" no_builder_procs
  check "volume still writable after the unlinked-file hog" bash -c "echo ok > '$STAGE/writable' && [ -s '$STAGE/writable' ]"
  # a Run Script that tries to SIGSTOP/SIGKILL the runner first, then hogs
  # (src/ is deleted after the job, so dispatch_watch copies stopper.out while it runs)
  dispatch_watch stopper.out "$(hog_mode stopper)" --timeout 300 --disk-budget-mb 1000
  local log=$WATCH_OUT
  echo "$log" | grep -E '^BH-STOPPER ' | sed 's/^/  /'
  check "stopper knew the runner's pid (not a vacuous DENIED)" grep -qE 'BH-STOPPER runner_pid=[0-9]+$' <<<"$log"
  check "build cannot kill -STOP the runner" grep -q 'BH-STOPPER kill_stop_runner=DENIED' <<<"$log"
  check "build cannot pkill -STOP the runner" grep -q 'BH-STOPPER pkill_stop_runner=DENIED' <<<"$log"
  check "build cannot kill -KILL the runner" grep -q 'BH-STOPPER kill_kill_runner=DENIED' <<<"$log"
  check "budget still enforced after the stop attempts ($(bh_json_get status <<<"$R"))" [ "$(bh_json_get status <<<"$R")" = disk_budget ]
  check "no bh-builder processes after the stopper" no_builder_procs
  # positive control: without builder.sb the same Run Script CAN stop and kill the runner, so the
  # DENIED results above are the signal rules, not a broken probe. Only the root dispatcher is left.
  dispatch_watch stopper.out "$(hog_mode stopper)" --timeout 120 --disk-budget-mb 1000 --profile allow-all.sb
  log=$WATCH_OUT
  check "control (allow-all.sb): the build can SIGSTOP and SIGKILL the runner" \
    bash -c 'grep -q "kill_stop_runner=ok" <<<"$1" && grep -q "kill_kill_runner=ok" <<<"$1"' _ "$log"
  check "control: dispatcher still ends the job and sweeps ($(bh_json_get status <<<"$R"))" no_builder_procs
  bh_new_job; R=$(bh_dispatch hog "$(hog_mode mem)" Hog.xcodeproj Hog --timeout 300 --mem-budget-mb 700)
  check "Run Script allocating 1.5 GB with a 700 MB budget -> memory_budget ($(bh_json_get message <<<"$R"))" [ "$(bh_json_get status <<<"$R")" = memory_budget ]
  check "memory budget enforced by the root dispatcher (message starts 'dispatcher:')" bash -c '[[ "$1" == dispatcher:* ]]' _ "$(bh_json_get message <<<"$R")"
  check "no bh-builder processes after memory_budget" no_builder_procs
  limit_tests
}

# dispatch_watch <out-name> <sha> [dispatch args]: run the hog job in the background; while it runs,
# copy <job>/src/<out-name> (job dirs are deleted after the job) and record the peak number of
# uid-470 processes. Sets R, WATCH_OUT, PEAK, ELAPSED. Gives up (SIGTERM to the dispatcher) at 240 s.
dispatch_watch() {
  local name=$1 sha=$2; shift 2
  bh_new_job; local out="$STAGE/watch.$JOB_ID" t0=$SECONDS
  ( bh_dispatch hog "$sha" Hog.xcodeproj Hog "$@" > "$out" ) & local bg=$!
  PEAK=0; WATCH_OUT=
  while kill -0 "$bg" 2>/dev/null; do
    local n; n=$(ps -U "$BH_UID" -o pid= 2>/dev/null | wc -l | tr -d ' '); [ "$n" -gt "$PEAK" ] && PEAK=$n
    local c; c=$(sudo -n cat "$JOB_DIR/src/$name" 2>/dev/null) && [ -n "$c" ] && WATCH_OUT=$c
    if [ $((SECONDS - t0)) -gt 240 ]; then
      local d; d=$(pgrep -f "[Pp]ython3? -I $BH_DISPATCH" | head -1); [ -n "$d" ] && sudo -n kill -TERM "$d"
    fi
    sleep 0.5
  done
  wait "$bg"; R=$(cat "$out"); ELAPSED=$((SECONDS - t0))
}

limit_tests() {
  # MEDIUM 1: fork bomb and fd exhaustion are capped by the plist rlimits; teardown still works
  dispatch_watch forkbomb.out "$(hog_mode forkbomb)" --timeout 60
  echo "  forkbomb: $WATCH_OUT peak_uid470_procs=$PEAK status=$(bh_json_get status <<<"$R")"
  check "forkbomb: one JSON status line ($(bh_json_get status <<<"$R"))" [ -n "$(bh_json_get status <<<"$R")" ]
  check "forkbomb: capped by NumberOfProcesses 256 (peak $PEAK uid-470 processes)" [ "$PEAK" -le 270 ]
  check "forkbomb: no bh-builder processes after" no_builder_procs
  check "forkbomb: runner label booted out" label_gone
  check "forkbomb: sentinel (uid $(id -u)) alive" sentinel_alive
  check "forkbomb: owner can still fork" /usr/bin/true
  dispatch_watch fds.out "$(hog_mode fds)" --timeout 60
  echo "  fds: $(echo $WATCH_OUT) status=$(bh_json_get status <<<"$R")"
  check "fds: each process capped by NumberOfFiles 1024" bash -c 'n=$(grep -c "opened=" <<<"$1"); [ "$n" -ge 1 ] && ! grep -oE "opened=[0-9]+" <<<"$1" | cut -d= -f2 | awk "\$1>1024{bad=1} END{exit !bad}"' _ "$WATCH_OUT"
  check "fds: one JSON status line, no leftovers, label gone, sentinel alive" \
    bash -c '[ -n "$1" ]' _ "$(bh_json_get status <<<"$R")"
  check "fds: no bh-builder processes after" no_builder_procs
  check "fds: label gone and sentinel alive" bash -c '! sudo -n launchctl print system/com.borrowedhands.runner >/dev/null 2>&1 && kill -0 "$1"' _ "$SENTINEL"

  # MEDIUM 2: chflags uchg denied; chmod 000 trees still deleted; disk space comes back
  local free0 free1; free0=$(df -m /Users/Shared | awk 'NR==2{print $4}')
  dispatch_watch immutable.out "$(hog_mode immutable)" --timeout 300
  echo "  immutable: $(echo $WATCH_OUT) status=$(bh_json_get status <<<"$R") job_dirs_removed=$(bh_json_get dispatch.job_dirs_removed <<<"$R")"
  check "immutable: chflags uchg denied by builder.sb" bash -c 'grep -q "chflags_uchg=DENIED" <<<"$1" && grep -q "chflags_uchg_dir=DENIED" <<<"$1"' _ "$WATCH_OUT"
  check "immutable: dispatcher removed the build dirs (only logs/job.json/result.json/runner.err left)" \
    bash -c '[ "$(sudo -n ls -A "$1" | sort | tr "\n" " ")" = "job.json logs result.json runner.err " ]' _ "$JOB_DIR"
  check "immutable: rm -rf of the job dir succeeds" sudo -n rm -rf "$JOB_DIR"
  free1=$(df -m /Users/Shared | awk 'NR==2{print $4}')
  check "immutable: free space back within 200 MB of the start ($free0 -> $free1 MB)" [ $((free0 - free1)) -le 200 ]

  # LOW 4: dispatcher deadline with the runner frozen (allow-all control: builder.sb would deny the stop)
  dispatch_watch freezer.out "$(hog_mode freezer)" --timeout 30 --profile allow-all.sb
  echo "  freezer: $WATCH_OUT status=$(bh_json_get status <<<"$R") elapsed=${ELAPSED}s msg=$(bh_json_get message <<<"$R")"
  check "freezer: the build really SIGSTOPped the runner" grep -q "freezer_stopped=ok" <<<"$WATCH_OUT"
  check "freezer: dispatcher deadline -> timeout within timeout+30s(+15 slack) (${ELAPSED}s)" \
    bash -c '[ "$1" = timeout ] && [ "$2" -le 75 ] && grep -q "runner did not finish" <<<"$3"' _ "$(bh_json_get status <<<"$R")" "$ELAPSED" "$(bh_json_get message <<<"$R")"
  check "freezer: no bh-builder processes after" no_builder_procs
}
echo "# host $(hostname -s), macOS $(sw_vers -productVersion), $(xcodebuild -version | head -1)"
[ -f "$BH_PREFIX/TEST_MODE" ] || { echo "install in test mode first: sudo bash sandbox/setup-bh-builder.sh --with-test-profiles"; exit 2; }
bh_stage_new
# root-owned, world-readable canary for the hard-link / clonefile probe rows (never a system file)
CANARY_DIR=/opt/borrowed-hands-test
sudo -n install -d -o root -g wheel -m 755 "$CANARY_DIR" && echo "bh-canary" | sudo -n tee "$CANARY_DIR/canary.txt" >/dev/null \
  && sudo -n chmod 644 "$CANARY_DIR/canary.txt"
trap 'kill "$SENTINEL" 2>/dev/null; sudo -n rm -rf "$CANARY_DIR"; bh_cleanup' EXIT
if [ "$MODE" = budget ]; then budget_tests; echo "# pass=$PASS fail=$FAIL"; [ "$FAIL" -eq 0 ]; exit; fi
if [ "$MODE" = l3 ]; then l3_tests; echo "# pass=$PASS fail=$FAIL"; [ "$FAIL" -eq 0 ]; exit; fi
if [ "$MODE" = deps ]; then bash "$BH_ROOT/tests/dependency_variants.sh"; exit $?; fi

# ---------------------------------------------------------------- validation (no build)
inval() {  # inval <description> <json> [dispatch-args]: expect invalid_job/usage and no job dir
  local d=$1 j=$2; shift 2
  local out; out=$(printf '%s' "$j" | (cd / && sudo -n "$BH_DISPATCH" "$@"))
  local st; st=$(printf '%s' "$out" | bh_json_get status)
  if [[ $st == invalid_job || $st == usage ]]; then ok "rejects: $d ($st)"; else no "rejects: $d -> $out"; fi
}
U=$(bh_uuid); S=0123456789abcdef0123456789abcdef01234567
J() { printf '{"v":1,"job_id":"%s","remote":"%s","commit":"%s","project":"%s","scheme":"App","configuration":"Debug","destination":"%s","allowed_refs":"%s"%s}' \
  "${1:-$U}" "${2:-https://github.com/example/app.git}" "${3:-$S}" "${4:-App.xcodeproj}" "${5:-platform=macOS,arch=arm64}" "${7:-refs/heads/bh/*}" "${6:-}"; }
inval "file:// remote without flag" "$(J "" "file:///private/tmp/x.git")"
# allowed_refs must be exactly refs/heads/bh/*
inval "allowed_refs refs/heads/*" "$(J "" "" "" "" "" "" "refs/heads/*")"
inval "allowed_refs refs/heads/bh/x" "$(J "" "" "" "" "" "" "refs/heads/bh/x")"
inval "allowed_refs missing" "$(J | sed 's/,"allowed_refs":"refs\/heads\/bh\/\*"//')"
inval "ssh remote" "$(J "" "ssh://git@github.com/x/y.git")"
inval "https with userinfo" "$(J "" "https://tok@github.com/x/y.git")"
inval "https with port" "$(J "" "https://github.com:8443/x/y.git")"
inval "short sha" "$(J "" "" "0123456")"
inval "project path traversal" "$(J "" "" "" "../evil.xcodeproj")"
inval "absolute project" "$(J "" "" "" "/etc/App.xcodeproj")"
inval "other destination" "$(J "" "" "" "" "platform=iOS Simulator,name=iPhone 17")"
inval "extra key" "$(J "" "" "" "" "" ',"extra":1')"
inval "job_id not uuid4" "$(J "../../etc")"
inval "not json" "nope"
inval "profile path traversal" "$(J)" --profile ../../../etc/x.sb
# MEDIUM 3: remote hosts that are addresses, or resolve to non-public addresses
for h in 127.1 0x7f.0.0.1 10.1 169.254.169.254 100.64.0.1 192.168.1.1 1.1.1.1 "[::1]" "[fd00::1]" localtest.me 127.0.0.1.nip.io; do
  inval "remote host $h" "$(J "" "https://$h/x/y.git")"
done
# LOW 6: bad port, 2000-deep JSON
inval "remote port 99999" "$(J "" "https://github.com:99999/x/y.git")"
out=$(J "" "https://github.com/x/y.git" | (cd / && sudo -n "$BH_DISPATCH" --timeout 5²))
check "dispatch --timeout 5² -> usage ($(bh_json_get status <<<"$out"))" grep -q '"status": "usage"' <<<"$out"
out=$(J "" "https://github.com/x/y.git" | (cd / && sudo -n "$BH_DISPATCH" --disk-budget-mb 99999999))
check "dispatch --disk-budget-mb 99999999 -> usage" grep -q '"status": "usage"' <<<"$out"
inval "2000-deep JSON" "$(/usr/bin/python3 -c 'print("["*2000 + "]"*2000)')"
check "no job dir created by rejected jobs" sudo -n test ! -e "$BH_JOBS/$U"

# HIGH 1: production install (no --with-test-profiles) -> no alternative profiles, no TEST_MODE,
# and --profile / --allow-file-remote are refused before any job is read or any plist written.
sudo -n bash "$BH_ROOT/sandbox/setup-bh-builder.sh" >/dev/null
check "prod setup: profiles/ is empty" [ -z "$(ls "$BH_PREFIX/libexec/profiles")" ]
check "prod setup: no TEST_MODE flag" [ ! -e "$BH_PREFIX/TEST_MODE" ]
F=$(bh_uuid); BH_CLEAN_JOBS+=("$BH_JOBS/$F")   # (only created if the gate is broken)
for a in "--profile allow-all.sb" "--profile ablate-0.sb" "--profile builder.sb" "--allow-file-remote" "--keep-build-dirs"; do
  out=$(J "$F" "file://$STAGE/x.git" | (cd / && sudo -n "$BH_DISPATCH" $a))
  check "prod mode refuses '$a' ($(bh_json_get status <<<"$out"))" grep -q '"status": "usage"' <<<"$out"
done
# exact production argv (sudoers lines list exact argv; see SANDBOX.md gap 8)
for a in "--timeout 300" "--timeout 301 --disk-budget-mb 20480 --mem-budget-mb 6144" \
         "--timeout 300 --mem-budget-mb 6144 --disk-budget-mb 20480" "--timeout 0300 --disk-budget-mb 20480 --mem-budget-mb 6144" \
         "--timeout 300 --disk-budget-mb 20480 --mem-budget-mb 6144 --timeout 300" "--timeout 300 --disk-budget-mb 999999 --mem-budget-mb 6144" \
         "--sweep-only --cancel" "--cancel x" ""; do
  out=$(J "$F" "https://127.1/x/y.git" | (cd / && sudo -n "$BH_DISPATCH" $a))
  check "prod mode refuses argv '$a' ($(bh_json_get status <<<"$out"))" grep -q '"status": "usage"' <<<"$out"
done
for t in 300 900 1800 3600; do
  out=$(J "$F" "https://127.1/x/y.git" | (cd / && sudo -n "$BH_DISPATCH" --timeout $t --disk-budget-mb 20480 --mem-budget-mb 6144))
  check "prod mode accepts the pinned argv for tier $t (then rejects the job itself: $(bh_json_get status <<<"$out"))" \
    grep -q '"status": "invalid_job"' <<<"$out"
done
out=$(cd / && sudo -n "$BH_DISPATCH" --sweep-only)
check "prod mode accepts --sweep-only ($(bh_json_get status <<<"$out"))" grep -q '"status": "swept"' <<<"$out"
check "prod mode: no job dir and no plist written" bash -c "sudo -n test ! -e '$BH_JOBS/$F' && test ! -e '$BH_PREFIX/run/com.borrowedhands.runner.plist'"
sudo -n bash "$BH_ROOT/sandbox/setup-bh-builder.sh" --with-test-profiles >/dev/null
check "test setup restored (TEST_MODE + profiles)" [ -f "$BH_PREFIX/TEST_MODE" -a -f "$BH_PREFIX/libexec/profiles/allow-all.sb" ]

# round-2 MEDIUM: setup never adopts a home someone else pre-created (tested on scratch paths
# through `setup --check-home`, so the installed builder is untouched)
H=/Users/Shared/bh-test-home-$$
mkdir "$H" && echo planted > "$H/file"
out=$(sudo -n bash "$BH_ROOT/sandbox/setup-bh-builder.sh" --check-home "$H" 2>&1); rc=$?
check "setup refuses a pre-created dir owned by uid $(id -u) with a file in it ($out)" \
  bash -c '[ "$1" -ne 0 ] && [ "$(stat -f %u "$2")" = "$(id -u)" ] && [ -f "$2/file" ]' _ "$rc" "$H"
rm -rf "$H"; mkdir "$H"
sudo -n bash "$BH_ROOT/sandbox/setup-bh-builder.sh" --check-home "$H" >/dev/null 2>&1; rc=$?
check "setup refuses a pre-created empty dir owned by uid $(id -u)" [ "$rc" -ne 0 ]
rm -rf "$H"; ln -s /tmp "$H"
sudo -n bash "$BH_ROOT/sandbox/setup-bh-builder.sh" --check-home "$H" >/dev/null 2>&1; rc=$?
check "setup refuses a symlink at the home path" [ "$rc" -ne 0 ]
rm -f "$H"
sudo -n bash "$BH_ROOT/sandbox/setup-bh-builder.sh" --check-home "$H" >/dev/null 2>&1; rc=$?
check "setup creates a missing home exclusively as 470:700" bash -c '[ "$1" -eq 0 ] && [ "$(stat -f %u:%Lp "$2")" = "470:700" ]' _ "$rc" "$H"
sudo -n rm -rf "$H"

# MEDIUM 4 / LOW 6 / LOW 7: dispatcher + runner internals
out=$(sudo -n /usr/bin/python3 -I "$BH_ROOT/tests/unit_dispatch.py" "$BH_DISPATCH" 2>&1); rc=$?
echo "$out" | sed 's/^/  /'
check "unit_dispatch.py (read_result, sweep failures, JSON/URL, user_dir)" [ "$rc" -eq 0 ]

# MEDIUM 2: git.sb denies reads/network/exec outside its job (positive control included)
out=$(bash "$BH_ROOT/scripts/profile-probe.sh" "$BH_PREFIX/libexec/git.sb" 2>&1); rc=$?
echo "$out" | sed 's/^/  /'
check "git.sb profile probe" [ "$rc" -eq 0 ]
out=$(bash "$BH_ROOT/scripts/profile-probe.sh" "$BH_PREFIX/libexec/builder.sb" build 2>&1); rc=$?
echo "$out" | sed 's/^/  /'
check "builder.sb profile probe" [ "$rc" -eq 0 ]
out=$(/usr/bin/python3 -I "$BH_PREFIX/libexec/bh-runner" "$BH_JOBS/$U" </dev/null)
check "bh-runner refuses to run as uid $(id -u)" grep -q 'must run as bh-builder' <<<"$out"
out=$(/usr/bin/python3 -I -c 'import importlib.machinery,types,sys
l=importlib.machinery.SourceFileLoader("r","/opt/borrowed-hands/libexec/bh-runner"); m=types.ModuleType("r"); m.__file__=l.path; l.exec_module(m)
print("guard_returned", m.kill_all_builder_processes())')
check "kill_all_builder_processes() is a no-op as uid $(id -u)" grep -q 'guard_returned False' <<<"$out"
check "sentinel (uid $(id -u)) alive after guard test" sentinel_alive
out=$(/usr/bin/python3 -I "$BH_DISPATCH" </dev/null)
check "bh-dispatch refuses to run without root" grep -q 'must run as root' <<<"$out"
[ "$MODE" = quick ] && { echo "# pass=$PASS fail=$FAIL"; [ "$FAIL" -eq 0 ]; exit; }

# ---------------------------------------------------------------- builds
SAMPLE=$BH_ROOT/helper/tests/fixtures/probe-proj
SHA=$(bh_stage_repo "$SAMPLE" sample)
bh_new_job; R=$(bh_dispatch sample "$SHA" Probe.xcodeproj Probe --timeout 600)
check "sample app (Probe) builds: $(bh_json_get status <<<"$R") in $(bh_json_get duration_s <<<"$R")s" [ "$(bh_json_get status <<<"$R")" = succeeded ]
check "no bh-builder processes after a build" no_builder_procs
check "runner label booted out after a build" label_gone
check "result.json/dispatch fields present" [ "$(bh_json_get dispatch.domain <<<"$R")" = system ]

# git materialization over real https under git.sb (public repo; no project there, so the job
# must get through the filtered bh/* fetch and the by-sha fetch, and stop at not_reachable: that repo has no
# bh/ branches, so the commit (on master) is not reachable from refs/heads/bh/*)
bh_new_job
R=$(printf '{"v":1,"job_id":"%s","remote":"https://github.com/octocat/Hello-World.git","commit":"7fd1a60b01f91b314f59955a4e4d4e80d8edf11d","project":"App.xcodeproj","scheme":"App","configuration":"Debug","destination":"platform=macOS,arch=arm64","allowed_refs":"refs/heads/bh/*"}' "$JOB_ID" | (cd / && sudo -n "$BH_DISPATCH" --timeout 120))
check "https fetch works under git.sb, then not_reachable ($(bh_json_get status <<<"$R"): $(bh_json_get message <<<"$R" | cut -c1-80))" [ "$(bh_json_get status <<<"$R")" = not_reachable ]

# git honours http.curloptResolve (the DNS pin): pinned to TEST-NET it must not reach GitHub
check "git honours the DNS pin (pinned to 192.0.2.1 -> no answer)" bash -c '! /usr/bin/perl -e "alarm 20; exec @ARGV" /Applications/Xcode.app/Contents/Developer/usr/bin/git -c http.curloptResolve=github.com:443:192.0.2.1 ls-remote https://github.com/octocat/Hello-World.git HEAD >/dev/null 2>&1'

# hostile git fixture: filter/diff attributes naming an undefined driver, a submodule gitlink,
# and a symlink out of the tree. Must materialize exactly (no submodule fetch) and build.
mkdir -p "$STAGE/fix-src" && (cd "$SAMPLE" && tar cf - .) | (cd "$STAGE/fix-src" && tar xf -)
printf '* filter=bhevil diff=bhevil\n' > "$STAGE/fix-src/.gitattributes"
printf '[submodule "sub"]\n\tpath = sub\n\turl = file:///private/tmp/bh-no-such-repo\n' > "$STAGE/fix-src/.gitmodules"
ln -s Sources "$STAGE/fix-src/leak"      # an internal symlink is fine (escaping ones: see the tree-safety tests)
SHAF=$(bh_stage_repo "$STAGE/fix-src" fixture)
bh_git -C "$STAGE/fixture-work" update-index --add --cacheinfo "160000,$SHA,sub" && bh_git -C "$STAGE/fixture-work" commit -q -m gitlink && SHAF=$(bh_publish fixture)
check "fixture commit really contains a gitlink" bash -c 'git -C "$1" ls-tree HEAD sub | grep -q "^160000 commit"' _ "$STAGE/fixture-work"
bh_new_job; R=$(bh_dispatch fixture "$SHAF" Probe.xcodeproj Probe --timeout 600 --keep-build-dirs)
check "fixture: checkout kept for inspection (--keep-build-dirs, test mode)" sudo -n test -d "$JOB_DIR/src"
check "hostile git fixture materializes and builds ($(bh_json_get status <<<"$R"))" [ "$(bh_json_get status <<<"$R")" = succeeded ]
check "fixture: submodule not fetched (src/sub empty)" bash -c 'test -z "$(sudo -n ls -A "$1/src/sub" 2>/dev/null)"' _ "$JOB_DIR"
check "fixture: symlink kept as a link, not followed" sudo -n test -L "$JOB_DIR/src/leak"

budget_tests
l3_tests
check "remote-dependency dispatcher variants" bash "$BH_ROOT/tests/dependency_variants.sh"

# compile error -> failed with 1-based line numbers
W=$STAGE/sample-work
/usr/bin/python3 -I - "$W/Sources/Greeting.swift" <<'PY'
import sys; p=sys.argv[1]; lines=open(p).read().split("\n")
lines.insert(1, '    let broken: Int = "not an int"  // line 2 on purpose')
open(p,"w").write("\n".join(lines))
PY
bh_git_commit sample "introduce error at line 2"; SHA2=$(bh_publish sample)
bh_new_job; R=$(bh_dispatch sample "$SHA2" Probe.xcodeproj Probe --timeout 600)
st=$(bh_json_get status <<<"$R"); e0=$(bh_json_get errors <<<"$R")
echo "  errors: $e0"
check "error build reports failed" [ "$st" = failed ]
check "error reported at Sources/Greeting.swift line 2 (1-based)" /usr/bin/python3 -I -c 'import json,sys
e=json.loads(sys.argv[1]); sys.exit(0 if any(x.get("file")=="Sources/Greeting.swift" and x.get("line")==2 for x in e) else 1)' "$e0"

# timeout: a Run Script that hangs and leaves detached children; runner must time out and sweep
mkdir -p "$STAGE/hang-src" && (cd "$BH_ROOT/tests/hang-app" && tar cf - .) | (cd "$STAGE/hang-src" && tar xf -)
SHA3=$(bh_stage_repo "$STAGE/hang-src" hang)
bh_new_job; R=$(bh_dispatch hang "$SHA3" Hang.xcodeproj Hang --timeout 45)
check "hanging build -> status timeout ($(bh_json_get status <<<"$R"), $(bh_json_get dispatch_duration_s <<<"$R")s)" [ "$(bh_json_get status <<<"$R")" = timeout ]
check "no bh-builder processes after timeout (detached daemons included)" no_builder_procs
check "sentinel (uid $(id -u)) alive after timeout sweep" sentinel_alive

# cancel: SIGTERM to the dispatcher mid-build (root signals the exact dispatcher pid it found)
bh_new_job
( bh_dispatch hang "$SHA3" Hang.xcodeproj Hang --timeout 600 > "$STAGE/cancel.out" ) &
BG=$!
for _ in $(seq 1 60); do bh_builder_procs | grep -q 'bh-hang-marker' && break; sleep 1; done
DPID=$(pgrep -f "[Pp]ython3? -I $BH_DISPATCH" | head -1)
[ -n "$DPID" ] && sudo -n kill -TERM "$DPID"
wait "$BG"; R=$(cat "$STAGE/cancel.out")
check "SIGTERM to dispatcher -> status cancelled ($(bh_json_get status <<<"$R"))" [ "$(bh_json_get status <<<"$R")" = cancelled ]
check "no bh-builder processes after cancel" no_builder_procs
check "runner label booted out after cancel" label_gone
check "sentinel (uid $(id -u)) alive after cancel" sentinel_alive

# ---------------------------------------------------------------- hostile probe suite
for prof in builder.sb allow-all.sb; do
  arg=(); [ "$prof" != builder.sb ] && arg=(--profile "$prof")
  out=$(bash "$BH_ROOT/scripts/probe-suite.sh" ${arg[@]+"${arg[@]}"} 2>&1); rc=$?
  echo "$out" | sed -n '/^probe /,/^SUITE/p' > "$STAGE/suite-$prof.txt"
  row() { grep -E "^$1 " "$STAGE/suite-$prof.txt"; }
  check "[$prof] build inside launchd is not in the owner's Aqua session (launchd_owner_session not FAIL)" \
    bash -c "grep -E '^launchd_owner_session ' '$STAGE/suite-$prof.txt' | grep -vq FAIL"
  [ "$prof" = allow-all.sb ] && check "[allow-all.sb] unsandboxed build reports managername System, manageruid 0" \
    grep -qE 'PROBE (script|cc)\.info_launchd_session=System \(manageruid=0\)' <<<"$out"
  check "[$prof] launchd jobs submitted by the build never ran as another uid" \
    bash -c "grep -E '^post:launchd_ran_as_other_uid +PASS' '$STAGE/suite-$prof.txt' >/dev/null"
  check "[$prof] no persistence after the job (processes, launchd firing, label)" \
    bash -c "! grep -E '^post:(leftover_processes|daemon_survives|launchd_job_fires_after_job|runner_label_still_loaded) +FAIL' '$STAGE/suite-$prof.txt' >/dev/null"
  [ "$prof" = builder.sb ] && check "[builder.sb] full probe suite passes, no FAIL/MISSING (rc=$rc)" [ "$rc" -eq 0 ]
  # Kernel groups can't be 470-only: macOS adds everyone(12)/localaccounts(61) and groups nesting
  # them (_lpoperator 100, com.apple.sharepoint.group.70x) for every local user. Assert no others.
  [ "$prof" = builder.sb ] && check "[builder.sb] build's kernel groups: 470 + implicit only (no staff/admin)" \
    bash -c 'g=$(grep -oE "PROBE cc\.info_kernel_groups=[0-9,]+" <<<"$1" | head -1 | cut -d= -f2); [ -n "$g" ] && ! tr , "\n" <<<"$g" | grep -vxE "470|12|61|100|70[1-9]" | grep -q .' _ "$out"
  [ "$prof" = builder.sb ] && grep -E "INCONCLUSIVE|FAIL|MISSING" "$STAGE/suite-$prof.txt" | sed 's/^/    /'
  echo "  $(tail -1 "$STAGE/suite-$prof.txt")"
done
check "sentinel (uid $(id -u)) alive at the end" sentinel_alive
check "no bh-builder processes at the end" no_builder_procs
echo "# pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
