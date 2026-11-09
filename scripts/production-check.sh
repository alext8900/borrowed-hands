#!/bin/bash
# Reinstall the sandbox in PRODUCTION mode on the build Mac and print evidence (SPEC 9.4):
#   bash scripts/production-check.sh            # run on the build Mac as the admin owner
# Runs `sudo bash sandbox/setup-bh-builder.sh` (no flags), then checks: no TEST_MODE, empty
# libexec/profiles, installed files identical to this checkout, and the dispatcher refusing every
# test-only flag before reading a job. Starts no build. Exit 0 only if every check passes.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
P=/opt/borrowed-hands
fail=0
ok() { echo "ok   - $*"; }
no() { echo "FAIL - $*"; fail=1; }
check() { local d=$1; shift; if "$@"; then ok "$d"; else no "$d"; fi; }

echo "# production check: $(hostname -s), $(date -u '+%Y-%m-%dT%H:%M:%SZ'), macOS $(sw_vers -productVersion), $(xcodebuild -version | head -1)"
echo "# checkout: $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo 'not a git checkout')"
echo "\$ sudo bash sandbox/setup-bh-builder.sh"
(cd / && sudo -n bash "$ROOT/sandbox/setup-bh-builder.sh") 2>&1 | sed 's/^/  /'
echo "\$ ls -la $P $P/libexec $P/libexec/profiles"
ls -la "$P" "$P/libexec" "$P/libexec/profiles" 2>&1 | sed 's/^/  /'
check "no $P/TEST_MODE" [ ! -e "$P/TEST_MODE" ]
check "libexec/profiles/ is empty" [ -z "$(ls -A "$P/libexec/profiles")" ]
for f in bin/bh-dispatch:bh-dispatch libexec/bh-runner:bh-runner libexec/builder.sb:builder.sb libexec/git.sb:git.sb; do
  check "installed ${f%%:*} matches sandbox/${f#*:} ($(shasum -a 256 "$P/${f%%:*}" | cut -c1-12))" cmp -s "$P/${f%%:*}" "$ROOT/sandbox/${f#*:}"
done
check "installed code is root-owned and not group/other-writable" bash -c \
  '[ -z "$(find '"$P"' \( ! -user root -o -perm -g+w -o -perm -o+w \) -print)" ]'
U=$(uuidgen | tr 'A-Z' 'a-z')
JOB=$(printf '{"v":1,"job_id":"%s","remote":"https://github.com/example/app.git","commit":"%s","project":"App.xcodeproj","scheme":"App","configuration":"Debug","destination":"platform=macOS,arch=arm64","allowed_refs":"refs/heads/bh/*"}' "$U" 0123456789abcdef0123456789abcdef01234567)
for a in "--profile allow-all.sb" "--profile builder.sb" "--allow-file-remote" "--keep-build-dirs" "--timeout 5²" \
         "--timeout 300" "--timeout 301 --disk-budget-mb 20480 --mem-budget-mb 6144" \
         "--timeout 300 --disk-budget-mb 20480 --mem-budget-mb 6144 --profile allow-all.sb"; do
  out=$(printf '%s' "$JOB" | (cd / && sudo -n "$P/bin/bh-dispatch" $a))
  check "production dispatcher refuses '$a': $out" grep -q '"status": "usage"' <<<"$out"
done
out=$(printf '%s' "$JOB" | sed 's#refs/heads/bh/\*#refs/heads/*#' | (cd / && sudo -n "$P/bin/bh-dispatch" --timeout 300 --disk-budget-mb 20480 --mem-budget-mb 6144))
check "production dispatcher refuses allowed_refs other than refs/heads/bh/*: $(cut -c1-120 <<<"$out")" grep -q '"status": "invalid_job"' <<<"$out"
check "no job dir was created by the refused jobs" sudo -n test ! -e "/Users/Shared/.bh-builder-home/jobs/$U"
check "runner label not loaded" bash -c '! sudo -n launchctl print system/com.borrowedhands.runner >/dev/null 2>&1'
echo "# production check: $([ $fail = 0 ] && echo PASS || echo FAIL)"
exit $fail
