#!/bin/bash
# Probe a sandbox profile's file/network/exec rules directly (no build), with a positive control.
#   scripts/profile-probe.sh <profile.sb>
# Runs each probe twice as the CURRENT user (never as bh-builder, never through launchd): once
# without a sandbox (control) and once under sandbox-exec with the profile and bh-runner's params
# pointed at a scratch job dir. A row is PASS only if the control succeeded and the sandboxed run
# was denied; if the control also fails the row is INCONCLUSIVE (it proves nothing about the
# profile). "expect=ok" rows are functional checks (TCP 443 must still work for git).
#
# Exec rules: rows tagged [x] widen process-exec to the probe tool (cat/ls/head/nc/sh) by appending
# one allow rule to a copy of the profile, so the file and network rules are what's being tested.
# The exec_* rows use the profile unmodified.
set -uo pipefail
PROFILE=${1:?usage: profile-probe.sh <profile.sb> [git|build]}
MODE=${2:-git}   # git: TCP 443 must work and only git may exec; build: no network at all
DEV=/Applications/Xcode.app/Contents/Developer
W=$(mktemp -d /private/tmp/bh-stage-XXXXXXXX); W=$(cd "$W" && pwd -P)
trap 'rm -rf "$W" /tmp/bh-pp-write-$$' EXIT
mkdir -p "$W/job/tmp" "$W/T" "$W/C"
NONCE=$RANDOM$RANDOM
echo "$NONCE" > "/Users/Shared/bh-bait-pp-$NONCE"; chmod 644 "/Users/Shared/bh-bait-pp-$NONCE"
trap 'chflags -R nouchg "$W" 2>/dev/null; rm -rf "$W" /tmp/bh-pp-write-$$ "/Users/Shared/bh-bait-pp-$NONCE"' EXIT
cp "$PROFILE" "$W/plain.sb"
{ cat "$PROFILE"; echo '(allow process-exec (literal "/bin/cat") (literal "/bin/ls") (literal "/usr/bin/head") (literal "/usr/bin/nc") (literal "/bin/sh") (literal "/usr/bin/touch") (literal "/usr/bin/chflags"))'; } > "$W/widened.sb"
D=(-D "JOB=$W/job" -D "USER_TMP=$W/T" -D "USER_CACHE=$W/C" -D "BUILDER_HOME=/Users/Shared/.bh-builder-home" -D "DEVELOPER_DIR=$DEV")

run() { /usr/bin/perl -e 'alarm shift; exec @ARGV or exit 127' 8 "$@" >/dev/null 2>&1; }
pass=0; fail=0; inc=0
row() {  # row <key> <expect: denied|ok> <x|plain> <cmd...>
  local k=$1 want=$2 mode=$3; shift 3
  local c s v
  run "$@" && c=ok || c=DENIED
  run /usr/bin/sandbox-exec -f "$W/$([ "$mode" = x ] && echo widened || echo plain).sb" "${D[@]}" "$@" && s=ok || s=DENIED
  if [ "$want" = ok ]; then [ "$s" = ok ] && v=PASS || v=FAIL
  elif [ "$c" != ok ]; then v=INCONCLUSIVE
  elif [ "$s" = DENIED ]; then v=PASS
  else v=FAIL; fi
  case $v in PASS) pass=$((pass+1));; FAIL) fail=$((fail+1));; *) inc=$((inc+1));; esac
  printf '%-30s %-6s control=%-6s sandboxed=%-6s %s\n' "$k" "$v" "$c" "$s" "$([ "$want" = ok ] && echo '(must work)')"
}
echo "profile-probe: $PROFILE (as uid $(id -u), $(hostname -s))"
row read_etc_hosts            denied x /bin/cat /etc/hosts
row read_etc_passwd           denied x /bin/cat /etc/passwd
row read_wifi_prefs           denied x /bin/cat /Library/Preferences/SystemConfiguration/com.apple.airport.preferences.plist
row read_network_prefs        denied x /bin/cat /Library/Preferences/SystemConfiguration/NetworkInterfaces.plist
row read_system_keychain_file denied x /usr/bin/head -c 4 /Library/Keychains/System.keychain
row read_shared_marker        denied x /bin/cat "/Users/Shared/bh-bait-pp-$NONCE"
row list_users                denied x /bin/ls /Users
row list_users_shared         denied x /bin/ls /Users/Shared
# inside the job's own tmp (writable): only the flag change itself must be refused
# (each run uses its own file: the inner shell's pid; the trap clears the flags before rm)
row chflags_uchg              denied x /bin/sh -c "/usr/bin/touch '$W/job/tmp/uchg-'\$\$ && /usr/bin/chflags uchg '$W/job/tmp/uchg-'\$\$"
row write_tmp                 denied x /bin/sh -c "echo x > /tmp/bh-pp-write-$$"
row net_tcp_80                denied x /usr/bin/nc -z -G 3 example.com 80
row net_tcp_22                denied x /usr/bin/nc -z -G 3 github.com 22
row net_udp_53                denied x /usr/bin/nc -u -z -w 2 1.1.1.1 53
if [ "$MODE" = git ]; then
  row net_tcp_443             ok     x /usr/bin/nc -z -G 3 example.com 443
  row exec_perl               denied plain /usr/bin/perl -e 1
else
  row net_tcp_443             denied x /usr/bin/nc -z -G 3 example.com 443
fi
row exec_osascript            denied plain /usr/bin/osascript -e 'return 1'
echo "PROFILE-PROBE pass=$pass fail=$fail inconclusive=$inc"
[ "$fail" -eq 0 ]
