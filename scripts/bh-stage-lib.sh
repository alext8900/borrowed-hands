# shellcheck shell=bash
# Shared helpers for scripts/probe-suite.sh and tests/. Run ON the build Mac as the admin owner
# (passwordless sudo). Builds only ever start through `sudo bh-dispatch` (a system-domain launchd
# job), never as `sudo -u bh-builder` from this login session. Source this file.
BH_UID=470
BH_HOME=/Users/Shared/.bh-builder-home
BH_JOBS=$BH_HOME/jobs
BH_PREFIX=/opt/borrowed-hands
BH_DISPATCH=$BH_PREFIX/bin/bh-dispatch
BH_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BH_CLEAN_JOBS=()

bh_uuid() { uuidgen | tr 'A-Z' 'a-z'; }

# bh_stage_new: world-readable staging dir /private/tmp/bh-stage-XXXXXXXX (file:// remotes). Sets STAGE.
bh_stage_new() {
  STAGE=$(mktemp -d /private/tmp/bh-stage-XXXXXXXX) || return 1
  STAGE=$(cd "$STAGE" && pwd -P); chmod 755 "$STAGE"
}

bh_git() { git -c user.name=bh-test -c user.email=bh-test@example.invalid -c commit.gpgsign=false "$@"; }
# bh_stage_repo <src-dir> <name>: copy a project into $STAGE/<name>-work, commit, publish. Prints sha.
bh_stage_repo() {
  local w="$STAGE/$2-work"
  mkdir -p "$w" && (cd "$1" && tar cf - --exclude .DS_Store .) | (cd "$w" && tar xf -)
  bh_git -C "$w" init -q -b main && bh_git_commit "$2" initial && bh_publish "$2"
}
bh_git_commit() { bh_git -C "$STAGE/$1-work" add -A && bh_git -C "$STAGE/$1-work" commit -q -m "$2"; }
# bh_publish <name>: (re)publish $STAGE/<name>-work as the bare repo $STAGE/<name>.git; print HEAD.
# HEAD is also pushed to refs/heads/bh/test (the runner only builds commits reachable from bh/*),
# unless BH_NO_BH_UPDATE=1 (to publish a commit that is NOT on a bh/ branch).
bh_publish() {
  local w="$STAGE/$1-work" b="$STAGE/$1.git"
  [ "${BH_NO_BH_UPDATE:-0}" = 1 ] || bh_git -C "$w" branch -f bh/test HEAD >/dev/null
  rm -rf "$b"; bh_git clone -q --bare "$w" "$b"
  bh_git -C "$b" config uploadpack.allowAnySHA1InWant true
  bh_git -C "$b" config uploadpack.allowFilter true
  chmod -R a+rX "$STAGE"
  bh_git -C "$w" rev-parse HEAD
}

# bh_new_job: fresh job id. Sets JOB_ID/JOB_DIR (call in the current shell, not in $(...)).
bh_new_job() { JOB_ID=$(bh_uuid); JOB_DIR=$BH_JOBS/$JOB_ID; BH_CLEAN_JOBS+=("$JOB_DIR"); }

# bh_job_json <repo-name> <sha> <project> <scheme>
bh_job_json() {
  printf '{"v":1,"job_id":"%s","remote":"file://%s/%s.git","commit":"%s","project":"%s","scheme":"%s","configuration":"Debug","destination":"platform=macOS,arch=arm64","allowed_refs":"refs/heads/bh/*"}\n' \
    "$JOB_ID" "$STAGE" "$1" "$2" "$3" "$4"
}
# bh_dispatch <repo-name> <sha> <project> <scheme> [dispatch-args...]: prints the JSON result line.
bh_dispatch() {
  local n=$1 s=$2 p=$3 sc=$4; shift 4
  bh_job_json "$n" "$s" "$p" "$sc" | (cd / && sudo -n "$BH_DISPATCH" --allow-file-remote "$@")
}
bh_json_get() { /usr/bin/python3 -I -c 'import json,sys
try: o=json.loads(sys.stdin.read())
except ValueError: o={}
for k in sys.argv[1].split("."): o=o.get(k) if isinstance(o,dict) else None
print("" if o is None else (json.dumps(o) if isinstance(o,(dict,list)) else o))' "$1"; }
bh_job_log() { sudo -n cat "$JOB_DIR/logs/xcodebuild.log" 2>/dev/null; }

# uid-470 processes, minus zombies and the Apple per-user agents launchd itself respawns on demand
# (ppid 1, exact binaries listed in bh-runner LAUNCHD_AGENTS). bh_builder_procs_all shows everything.
bh_builder_procs_all() { ps -U "$BH_UID" -o pid=,ppid=,stat=,command= 2>/dev/null; }
bh_builder_procs() {
  bh_builder_procs_all | awk '$3 ~ /^Z/ {next}
    $2 == 1 && ($4 == "/usr/sbin/distnoted" || $4 == "/usr/sbin/cfprefsd" || $4 == "/usr/libexec/lsd" ||
      $4 == "/usr/libexec/trustd" || $4 == "/usr/libexec/secd" || $4 == "/usr/libexec/secinitd" ||
      $4 == "/usr/libexec/containermanagerd") {next} {print}'
}

# bh_cleanup: remove job dirs we created and the staging dir. Kills nothing: bh-dispatch already
# swept uid 470 (as uid 470) on every path, and the tests assert that.
bh_cleanup() {
  local j
  for j in ${BH_CLEAN_JOBS[@]+"${BH_CLEAN_JOBS[@]}"}; do
    [[ $j =~ ^$BH_JOBS/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] && sudo -n rm -rf "$j"
  done
  [ -n "${STAGE:-}" ] && [[ $STAGE == /private/tmp/bh-stage-* ]] && rm -rf "$STAGE"
  return 0
}
