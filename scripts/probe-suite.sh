#!/bin/bash
# Borrowed Hands hostile probe suite.
#
#   scripts/probe-suite.sh [--profile NAME] [--keep]
#
# Builds tests/hostile-app (a Run Script phase and a project-controlled CC wrapper that both try
# every escape in tests/hostile-app/probes/probe.sh) through the real path: sudo bh-dispatch ->
# system-domain launchd job (bh-builder, SessionCreate) -> bh-runner -> sandbox-exec xcodebuild,
# under the named installed profile (default: the installed builder.sb; others come from
# /opt/borrowed-hands/libexec/profiles/). Then checks for persistence after the job (leftover
# processes, launchd jobs still firing, files, prefs). Prints a PASS/FAIL table: an escape that
# works is FAIL. Exit 0 only if the build succeeded and every row is PASS.
#
# Run ON the build Mac as the admin owner (needs `sudo -n` for bh-dispatch and to read job logs).
set -uo pipefail
. "$(dirname "$0")/bh-stage-lib.sh"

PROFILE=
KEEP=0
while [ $# -gt 0 ]; do
  case $1 in
    --profile) PROFILE=$2; shift 2 ;;
    --keep) KEEP=1; shift ;;
    *) echo "usage: $0 [--profile NAME] [--keep]" >&2; exit 2 ;;
  esac
done

NONCE=$(bh_uuid | tr -d '-' | cut -c1-16)
# Root-owned, world-readable canary outside the read allowlist, for the hard-link / clonefile rows.
# Never a real system file. Lives on the same (Data) volume as the job dirs.
CANARY_DIR=/opt/borrowed-hands-test; CANARY=$CANARY_DIR/canary.txt; MADE_CANARY_DIR=0
MARKER_SHARED=/Users/Shared/bh-bait-$NONCE
LISTENER_PID=
clean_outside() {
  # Files a successful escape (or a launchd job a probe submitted) would have left: exact names only.
  sudo -n /bin/sh -c "rm -f /tmp/bh-probe-*-$NONCE /private/var/tmp/bh-probe-*-$NONCE /Users/Shared/bh-probe-*-$NONCE \
    $BH_HOME/bh-probe-*-$NONCE $BH_HOME/Library/LaunchAgents/bh-probe-*-$NONCE.plist \
    /tmp/bh-submit-$NONCE-* /tmp/bh-boot-$NONCE-* $BH_HOME/Library/Preferences/com.bh.probe.plist ${OTHER_JOB:-/nonexistent}/bh-probe-*"
}
cleanup() {
  [ -n "$LISTENER_PID" ] && kill "$LISTENER_PID" 2>/dev/null
  rm -f "$MARKER_SHARED"
  clean_outside
  [ "$MADE_CANARY_DIR" = 1 ] && sudo -n rm -rf "$CANARY_DIR"
  [ "$KEEP" = 1 ] && { echo "kept: $STAGE ${BH_CLEAN_JOBS[*]}"; return; }
  bh_cleanup
}
trap cleanup EXIT

bh_stage_new || exit 1
[ -d "$CANARY_DIR" ] || { sudo -n install -d -o root -g wheel -m 755 "$CANARY_DIR" && MADE_CANARY_DIR=1; }
printf 'bh-canary %s\n' "$NONCE" | sudo -n tee "$CANARY" >/dev/null && sudo -n chown root:wheel "$CANARY" && sudo -n chmod 644 "$CANARY"
# World-readable bait the sandbox (not uid) must keep the build from reading.
echo "$NONCE" > "$MARKER_SHARED"; chmod 644 "$MARKER_SHARED"
echo "$NONCE" > "$STAGE/marker-tmp"; chmod 644 "$STAGE/marker-tmp"
# A loopback listener the build should not be able to reach.
PORT=$(( 20000 + RANDOM % 20000 ))
/usr/bin/python3 -I -c 'import socket,sys,time
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(16)
s.settimeout(1)
end=time.time()+900
while time.time()<end:
    try: c,_=s.accept(); c.close()
    except socket.timeout: pass' "$PORT" &
LISTENER_PID=$!; disown "$LISTENER_PID"
# A previous job's dir (same uid): the build must not read or write it.
OTHER_JOB=$BH_JOBS/$(bh_uuid); BH_CLEAN_JOBS+=("$OTHER_JOB")
sudo -n /bin/sh -c "install -d -o $BH_UID -g $BH_UID -m 700 $OTHER_JOB && echo $NONCE > $OTHER_JOB/marker && chown $BH_UID:$BH_UID $OTHER_JOB/marker"

OWNER_TMPDIR=$(cd "$(getconf DARWIN_USER_TEMP_DIR)" && pwd -P)
mkdir -p "$STAGE/hostile-src"
(cd "$BH_ROOT/tests/hostile-app" && tar cf - --exclude .DS_Store .) | (cd "$STAGE/hostile-src" && tar xf -)
cat > "$STAGE/hostile-src/probes/targets.env" <<EOF
OWNER_HOME='$HOME'
OWNER_UID=$(id -u)
OWNER_TMPDIR='$OWNER_TMPDIR'
OWNER_VAR_FOLDER='$(dirname "$OWNER_TMPDIR")'
NONCE=$NONCE
MARKER_SHARED=$MARKER_SHARED
MARKER_TMP=$STAGE/marker-tmp
LOOPBACK_PORT=$PORT
BH_REAL_HOME=$BH_HOME
OTHER_JOB_DIR=$OTHER_JOB
OTHER_JOB_MARKER=$OTHER_JOB/marker
CANARY=$CANARY
EOF
SHA=$(bh_stage_repo "$STAGE/hostile-src" hostile) || { echo "staging failed" >&2; exit 1; }

extract() { grep -oE 'PROBE (script|cc)\.[a-z_0-9]+=[A-Za-z0-9@._,-]*( \([^)]*\))?' | sort -u; }
echo "probe-suite: profile=${PROFILE:-builder.sb (installed)} nonce=$NONCE host=$(hostname -s)"
# Positive control: the same hostile build with no sandbox (allow-all.sb). A row counts as PASS only
# if the control shows the escape working; otherwise it is INCONCLUSIVE (blocked by uid/session or
# the target doesn't exist here, so the row says nothing about the profile).
if [ "$PROFILE" != allow-all.sb ]; then
  bh_new_job
  CRESULT=$(bh_dispatch hostile "$SHA" Hostile.xcodeproj Hostile --profile allow-all.sb --timeout 600)
  echo "control: status=$(printf '%s' "$CRESULT" | bh_json_get status)"
  CONTROL=$(bh_job_log | extract)
  clean_outside
fi
bh_new_job
RESULT=$(bh_dispatch hostile "$SHA" Hostile.xcodeproj Hostile ${PROFILE:+--profile "$PROFILE"} --timeout 600)
echo "dispatch: $RESULT"
STATUS=$(printf '%s' "$RESULT" | bh_json_get status)
LOG=$(bh_job_log)
PROBES=$(printf '%s\n' "$LOG" | extract)
[ "$PROFILE" = allow-all.sb ] && CONTROL=$PROBES

# Persistence checks after the job, i.e. after bh-runner's teardown.
post() { printf 'PROBE post.%s=%s\n' "$1" "$2"; }
POST=$(
  left=$(bh_builder_procs || true)
  [ -n "$left" ] && post leftover_processes "ok ($(echo "$left" | wc -l | tr -d ' '))" || post leftover_processes DENIED
  printf '%s\n' "$left" | grep -q "bh-probe-daemon-$NONCE" && post daemon_survives ok || post daemon_survives DENIED
  sudo -n launchctl print system/com.borrowedhands.runner >/dev/null 2>&1 && post runner_label_still_loaded ok || post runner_label_still_loaded DENIED
  # Jobs the probes submitted append the uid they ran as to these files.
  uids=$(sudo -n /bin/sh -c "cat /tmp/bh-submit-$NONCE-* /tmp/bh-boot-$NONCE-* 2>/dev/null" | sort -u | tr '\n' ' ')
  echo "$uids" | tr ' ' '\n' | grep -v -e '^$' -e "^$BH_UID\$" | grep -q . && post launchd_ran_as_other_uid "ok (uids: $uids)" || post launchd_ran_as_other_uid "DENIED (uids: ${uids:-none})"
  c1=$(sudo -n /bin/sh -c "cat /tmp/bh-submit-$NONCE-* /tmp/bh-boot-$NONCE-* 2>/dev/null | wc -l"); sleep 12
  c2=$(sudo -n /bin/sh -c "cat /tmp/bh-submit-$NONCE-* /tmp/bh-boot-$NONCE-* 2>/dev/null | wc -l")
  [ "$c2" -gt "$c1" ] && post launchd_job_fires_after_job "ok ($c1 -> $c2)" || post launchd_job_fires_after_job DENIED
  sudo -n test -e "$BH_HOME/Library/Preferences/com.bh.probe.plist" && post defaults_persist ok || post defaults_persist DENIED
  ipcs -m -s -q 2>/dev/null | awk '$5=="bh-builder"' | grep -q . && post sysv_ipc_left_behind ok || post sysv_ipc_left_behind DENIED
  n=$(sudo -n /bin/sh -c "ls -d /tmp/bh-probe-*-$NONCE /private/var/tmp/bh-probe-*-$NONCE /Users/Shared/bh-probe-*-$NONCE $BH_HOME/bh-probe-*-$NONCE $BH_HOME/Library/LaunchAgents/bh-probe-* $OTHER_JOB/bh-probe-* 2>/dev/null | wc -l" | tr -d ' ')
  [ "$n" -gt 0 ] && post outside_files_left "ok ($n)" || post outside_files_left DENIED
)

# Keys that are informational or allowed by design (inside the job's own checkout).
INFO_KEYS="whoami write_srcroot"   # plus every info_* key
KEYS=$(printf '%s\n' "$PROBES" | sed -E 's/^PROBE (script|cc)\.([a-z_0-9]+)=.*/\2/' | sort -u)
fails=0; passes=0; incs=0
printf '\n%-32s %-13s %-13s\n' "probe" "script" "cc"
printf '%-32s %-13s %-13s\n' "-----" "------" "--"
for k in $KEYS; do
  case " $INFO_KEYS " in *" $k "*) continue ;; esac
  case $k in info_*) continue ;; esac
  row=()
  for ctx in script cc; do
    v=$(printf '%s\n' "$PROBES" | grep -m1 "^PROBE $ctx\.$k=" | sed -E 's/^[^=]*=([A-Za-z]+).*/\1/')
    c=$(printf '%s\n' "$CONTROL" | grep -m1 "^PROBE $ctx\.$k=" | sed -E 's/^[^=]*=([A-Za-z]+).*/\1/')
    case $v in
      ok) row+=(FAIL); fails=$((fails+1)) ;;
      DENIED) if [ "$c" = ok ]; then row+=(PASS); passes=$((passes+1)); else row+=(INCONCLUSIVE); incs=$((incs+1)); fi ;;
      ABSENT) row+=(INCONCLUSIVE); incs=$((incs+1)) ;;
      *) row+=(MISSING); fails=$((fails+1)) ;;
    esac
  done
  printf '%-32s %-13s %-13s\n' "$k" "${row[0]}" "${row[1]}"
done
printf '%s\n' "$POST" | while IFS= read -r l; do
  k=$(echo "$l" | sed -E 's/^PROBE post\.([a-z_]+)=.*/\1/'); v=$(echo "$l" | sed -E 's/^[^=]*=([A-Za-z]+).*/\1/')
  d=$(echo "$l" | sed -nE 's/^[^=]*=[A-Za-z]+ (.*)$/\1/p')
  printf '%-32s %-13s %s\n' "post:$k" "$([ "$v" = DENIED ] && echo PASS || echo FAIL)" "$d"
done
pf=$(printf '%s\n' "$POST" | grep -c '=ok' || true)
fails=$((fails + pf)); passes=$((passes + $(printf '%s\n' "$POST" | grep -c '=DENIED')))
nkeys=$(echo "$KEYS" | grep -c . || true)
echo
printf '%s\n' "$LOG" | grep -oE 'PROBE (script|cc)\.(whoami|info_[a-z_]+)=.*' | sort -u | sed 's/^/info: /' 
echo "SUITE build=$STATUS probes=$nkeys pass=$passes fail=$fails inconclusive=$incs (control: allow-all.sb)"
[ "$STATUS" = succeeded ] && [ "$fails" -eq 0 ] && [ "$nkeys" -ge 40 ]
