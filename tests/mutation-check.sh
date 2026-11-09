#!/bin/bash
# Mutation check for the HIGH 1 (test-mode gate) and MEDIUM 4 (sweep/result hardening) fixes:
# break each rule on purpose and confirm a test fails. Run on the build Mac after
#   sudo bash sandbox/setup-bh-builder.sh --with-test-profiles
# Every mutant is a copy; the installed files are restored by re-running setup at the end.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PREFIX=/opt/borrowed-hands
W=$(mktemp -d /private/tmp/bh-mut-XXXXXXXX)
restore() { sudo -n bash "$ROOT/sandbox/setup-bh-builder.sh" --with-test-profiles >/dev/null; rm -rf "$W"; }
trap restore EXIT
killed=0; survived=0
verdict() {  # verdict <name> <test exit code>
  if [ "$2" -ne 0 ]; then killed=$((killed+1)); echo "KILLED   - $1"; else survived=$((survived+1)); echo "SURVIVED - $1"; fi
}
mutant() {  # mutant <name> <file> <python replace: old> <new>  -> path of mutated copy on stdout
  local out="$W/$1"
  /usr/bin/python3 -I - "$2" "$out" "$3" "$4" <<'PY' || return 1
import sys
src, dst, old, new = sys.argv[1:5]
s = open(src).read()
assert s.count(old) == 1, "mutation anchor must match exactly once: " + old
open(dst, "w").write(s.replace(old, new, 1))
PY
  echo "$out"
}

# ---- MEDIUM 4: unit tests against mutated dispatcher copies
unit() {
  local rc
  cp "$D" "$W/unit-original"
  sudo -n /usr/bin/python3 -I "$ROOT/tests/unit_dispatch.py" "$1" >"$W/unit.out" 2>&1; rc=$?
  grep '^FAIL' "$W/unit.out" | head -4 | sed 's/^/           /'
  cmp -s "$W/unit-original" "$D" || exit 2
  echo "RESTORED - dispatcher unit copy ($(shasum -a 256 "$D" | cut -c1-12))"
  return "$rc"
}
D=$ROOT/sandbox/bh-dispatch
m=$(mutant cancel-idle-exit "$D" 'sys.exit(0 if status == "not_running" else 1)' 'sys.exit(1)') && { unit "$m"; verdict "not_running cleanup returning nonzero" $?; }
m=$(mutant no-nlink "$D" 'and st.st_nlink == 1' '') && { unit "$m"; verdict "read_result without the st_nlink == 1 check" $?; }
m=$(mutant no-mode "$D" 'and stat.S_IMODE(st.st_mode) == mode' '') && { unit "$m"; verdict "read_result without the mode 0600 check" $?; }
m=$(mutant no-uid "$D" 'and st.st_uid == R.BUILDER_UID and st.st_nlink' 'and st.st_nlink') && { unit "$m"; verdict "read_result without the uid 470 check" $?; }
m=$(mutant follow "$D" 'os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd' 'os.O_RDONLY | os.O_NONBLOCK, dir_fd=dfd') && { unit "$m"; verdict "read_result following symlinks" $?; }
m=$(mutant no-pre-abort "$D" '    if not dispatch["pre_sweep_ok"]:' '    if False:') && { unit "$m"; verdict "pre-sweep failure not aborting" $?; }
m=$(mutant no-post-abort "$D" '    if not dispatch["post_sweep_ok"]:' '    if False:') && { unit "$m"; verdict "post-sweep failure not discarding result.json" $?; }

# ---- HIGH 1: install a dispatcher without the test-mode gate / a setup that keeps profiles,
# then run the production-mode checks from tests/run-tests.sh (quick mode).
quick() { bash "$ROOT/tests/run-tests.sh" quick >"$W/quick.out" 2>&1; local rc=$?; grep -E '^FAIL' "$W/quick.out" | sed 's/^/           /'; return $rc; }
# (setup reinstalls bin/bh-dispatch from the checkout, so the mutant must replace the checkout copy)
m=$(mutant no-gate "$D" 'and not test_mode():' 'and False:') && {
  cp "$D" "$W/bh-dispatch.orig"; cp "$m" "$D"
  quick; rc=$?
  cp "$W/bh-dispatch.orig" "$D"
  cmp -s "$W/bh-dispatch.orig" "$D" || exit 2
  echo "RESTORED - no-gate ($(shasum -a 256 "$D" | cut -c1-12))"
  verdict "dispatch honouring --profile/--allow-file-remote without TEST_MODE" $rc; }
S=$ROOT/sandbox/setup-bh-builder.sh
m=$(mutant keep-profiles "$S" 'rm -f $PREFIX/libexec/profiles/*.sb $PREFIX/TEST_MODE' ':') && {
  cp "$ROOT/tests/run-tests.sh" "$W/run-tests.orig"
  cp "$m" "$ROOT/sandbox/.setup-mutant.sh"
  /usr/bin/python3 -I - "$ROOT/tests/run-tests.sh" <<'PYM'
import sys
p = sys.argv[1]
s = open(p).read()
old = 'sandbox/setup-bh-builder.sh" >/dev/null'
assert s.count(old) == 1
open(p, "w").write(s.replace(old, 'sandbox/.setup-mutant.sh" >/dev/null', 1))
PYM
  quick; rc=$?
  cp "$W/run-tests.orig" "$ROOT/tests/run-tests.sh"
  cmp -s "$W/run-tests.orig" "$ROOT/tests/run-tests.sh" || exit 2
  echo "RESTORED - keep-profiles ($(shasum -a 256 "$ROOT/tests/run-tests.sh" | cut -c1-12))"
  rm -f "$ROOT/sandbox/.setup-mutant.sh"
  verdict "setup leaving test profiles + TEST_MODE in a production install" $rc; }

# ---- fix round 2: mutate the checkout copy (setup installs from it), reinstall, run one test mode
checkout_mutant() {  # checkout_mutant <name> <file> <old> <new> <run-tests mode>
  if [ -n "${MUT_ONLY:-}" ] && ! [[ $1 =~ $MUT_ONLY ]]; then return; fi   # MUT_ONLY=regex: run a subset
  local m rc; m=$(mutant "$1" "$2" "$3" "$4") || { echo "SKIPPED  - $1 (anchor missing)"; survived=$((survived+1)); return; }
  cp "$2" "$W/orig.$1"; cp "$m" "$2"
  sudo -n bash "$ROOT/sandbox/setup-bh-builder.sh" --with-test-profiles >/dev/null 2>&1
  bash "$ROOT/tests/run-tests.sh" "$5" >"$W/$1.out" 2>&1; rc=$?
  grep -E '^ *FAIL' "$W/$1.out" | head -4 | sed 's/^ */           /'
  cp "$W/orig.$1" "$2"
  cmp -s "$W/orig.$1" "$2" || exit 2
  echo "RESTORED - $1 ($(shasum -a 256 "$2" | cut -c1-12))"
  sudo -n bash "$ROOT/sandbox/setup-bh-builder.sh" --with-test-profiles >/dev/null 2>&1
  verdict "$6" $rc
}
B=$ROOT/sandbox/builder.sb
RN=$ROOT/sandbox/bh-runner
checkout_mutant no-statvfs "$D" '                if used > disk_budget or free_now < FREE_FLOOR_MB:' '                if False:' budget \
  "dispatcher without the statvfs disk budget"
checkout_mutant no-signal-deny "$B" '(deny signal)' ';; (deny signal) removed' budget \
  "builder.sb without (deny signal)"
checkout_mutant runner-fail-open "$RN" '            raise JobError("budget_check_failed", "could not measure job resources: %s" % str(e)[:200])' '            used = mem = 0' quick \
  "runner budget checks failing open"
checkout_mutant no-pin "$RN" '        if self.pin:' '        if False:' quick \
  "runner without the DNS pin"
checkout_mutant setup-adopts "$S" '[ "$own" = $ID ] && [ "$mode" = 700 ] ||' 'true ||' quick \
  "setup adopting a home it doesn't own"

# ---- fix round 3
checkout_mutant no-retry "$D" '    end = time.monotonic() + deadline_s
    while True:' '    return fn()
    while True:' quick \
  "dispatcher teardown without the EAGAIN retry guard"
checkout_mutant no-flags-deny "$B" '(deny file-write-flags file-write-acl)' ';; flags/acl deny removed' quick \
  "builder.sb without (deny file-write-flags file-write-acl)"
checkout_mutant no-job-cleanup "$D" 'dispatch["job_dirs_removed"] = retry(lambda: as_builder(clean_job) == 0)' 'dispatch["job_dirs_removed"] = False' budget \
  "dispatcher not deleting the job's build dirs"
checkout_mutant no-dispatch-mem "$D" '                if rss > mem_budget:' '                if False:' budget \
  "dispatcher without its memory budget"
checkout_mutant no-dispatch-deadline "$D" '                if time.monotonic() > deadline:' '                if False:' budget \
  "dispatcher without its wall-clock deadline"

# ---- ancestry, tree safety, remote dependencies, retention
checkout_mutant l3-refs "$RN" '    if job["allowed_refs"] != ALLOWED_REFS:' '    if False:' quick \
  "validator accepting any allowed_refs"
checkout_mutant l3-ancestry "$RN" '        if not reachable:' '        if False:' l3 \
  "runner building commits not reachable from bh/*"
checkout_mutant l3-symlink "$RN" '                    if not inside or in_git:' '                    if False:' quick \
  "check_tree accepting escaping symlinks"
checkout_mutant l3-tree-call "$RN" '        check_tree(self.src)
' '' l3 \
  "runner not checking the materialized tree"
checkout_mutant l3-deps "$RN" '        if deps:' '        if False:' l3 \
  "runner ignoring remote Swift package dependencies"
checkout_mutant l3-retention "$D" 'victims = retention_victims(job_dir_entries(), current)' 'victims = []' l3 \
  "dispatcher without job dir retention"
checkout_mutant l3-inflight "$D" '(m, n) for n, m in entries if n != current' '(m, n) for n, m in entries' quick \
  "retention allowed to select the in-flight job"

checkout_mutant l3-argv-tier "$D" 'and int(args[1]) in TIMEOUT_TIERS' '' quick \
  "production argv accepting any --timeout value"
checkout_mutant l3-argv-strict "$D" '    if not test_mode() and not production_argv_ok(args):' '    if False:' quick \
  "dispatcher without the exact production argv check"
checkout_mutant l3-cancel-id "$D" '    if running != job_id:' '    if False:' l3 \
  "--cancel signalling a dispatcher that runs a different job"


# Independent scanner guards: mutate a task-owned copy, never the installed runner or checkout.
# The unit harness takes that copy explicitly. Require the intended assertion, not an import crash.
runner_unit_mutant() {
  local m rc
  m=$(mutant "$1" "$RN" "$2" "$3") || { survived=$((survived+1)); return; }
  cp "$RN" "$W/scanner-original"
  sudo -n /usr/bin/python3 -I "$ROOT/tests/unit_dispatch.py" "$D" "$m" >"$W/$1.out" 2>&1; rc=$?
  grep '^FAIL' "$W/$1.out" | head -4 | sed 's/^/           /'
  if [ "$rc" -eq 0 ] || ! grep -F "$4" "$W/$1.out" | grep -q '^FAIL'; then
    verdict "$1 (intended assertion missing)" 0
  else
    verdict "$1" "$rc"
  fi
  rm "$m"
  cmp -s "$W/scanner-original" "$RN" || exit 2
  echo "RESTORED - $1 ($(shasum -a 256 "$RN" | cut -c1-12))"
}
runner_unit_mutant deps-casefold 'name = e.name.casefold()' 'name = e.name' 'case-folded dependency name'
runner_unit_mutant deps-symlink '                    if candidate:
                        found.append(rel)' '                    if False:
                        found.append(rel)' 'in-tree dependency symlink refused'
runner_unit_mutant deps-detection '    return any(LOCAL_PACKAGE_RE.match(text, m.start()) is None for m in PACKAGE_CALL_RE.finditer(text))' '    return bool(re.search(r"\.package\s*\((?:[^()]|\([^()]*\))*?\burl\s*:", text, re.S))' 'nonliteral-local package declaration refused'
runner_unit_mutant deps-cap '                    if len(raw) > MAX_SCAN_FILE:' '                    if False:' 'oversize dependency file refused: project.pbxproj'
runner_unit_mutant deps-pin-location '        if isinstance(loc, str) and re.match' '        if kind is None and isinstance(loc, str) and re.match' 'remote location refused regardless of pin kind'
runner_unit_mutant deps-special '                    if not e.is_file(follow_symlinks=False):' '                    if False:' 'special dependency file refused without open'

echo "# mutants killed=$killed survived=$survived"
[ "$survived" -eq 0 ] && { [ -n "${MUT_ONLY:-}" ] || [ "$killed" -eq 35 ]; }
