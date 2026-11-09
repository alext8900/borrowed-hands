#!/bin/bash
# Borrowed Hands hostile probe. Runs from the app's Run Script phase ("script") and from the
# project-controlled CC wrapper ("cc"). Each probe ATTEMPTS an escape and prints one line:
#   PROBE <ctx>.<key>=ok      the escape worked (bad when sandboxed)
#   PROBE <ctx>.<key>=DENIED  the escape was blocked
# Optional "(detail)" follows. Never exits non-zero: the build must keep going so every probe reports.
# Targets (owner's home, marker files, loopback port, nonce) come from probes/targets.env, generated
# per run by scripts/probe-suite.sh. An attacker who knows the owner's username is the worst case.
CTX=${1:-script}
HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck disable=SC1091
[ -f "$HERE/targets.env" ] && . "$HERE/targets.env"
: "${OWNER_HOME:=/Users/nobody}" "${NONCE:=nononce}" "${BH_REAL_HOME:=/Users/Shared/.bh-builder-home}"
OUT=/dev/stderr
[ "$CTX" = script ] && OUT=/dev/stdout
WORK="${TMPDIR:-/tmp}/bh-probe-work-$CTX-$$"
mkdir -p "$WORK" 2>/dev/null || WORK="${DERIVED_FILE_DIR:-$PWD}"

say() { echo "PROBE $CTX.$1=$2${3:+ ($3)}" >"$OUT"; }
# t <secs> cmd...: run with a wall-clock limit (no coreutils timeout on stock macOS).
t() { local s=$1; shift; /usr/bin/perl -e 'alarm shift; exec @ARGV or exit 127' "$s" "$@"; }
# p <key> <cmd...>: ok if the command exits 0.
p() { local k=$1; shift; if t 8 "$@" >/dev/null 2>&1; then say "$k" ok; else say "$k" DENIED "rc=$?"; fi; }
# pc <key> <expected-substring> <cmd...>: ok if output contains the expected string.
pc() { local k=$1 want=$2; shift 2; local o; o=$(t 8 "$@" 2>/dev/null | head -c 20000);
  if [[ "$o" == *"$want"* ]]; then say "$k" ok; else say "$k" DENIED; fi; }

say whoami "$(id -un 2>/dev/null)" "uid=$(id -u) home=$HOME"

# --- other users' data (uid is expected to stop these even without the sandbox)
p read_owner_development /bin/ls "$OWNER_HOME/Development"
p read_owner_ssh /bin/ls -a "$OWNER_HOME/.ssh"
p read_owner_public /bin/ls "$OWNER_HOME/Public"
p read_owner_var_folders /bin/ls "${OWNER_VAR_FOLDER:-/nonexistent}"
p read_owner_tmpdir /bin/ls "${OWNER_TMPDIR:-/nonexistent}"
# --- world-readable data (only the sandbox can stop these)
p list_users /bin/ls /Users
p list_users_shared /bin/ls /Users/Shared
pc read_shared_marker "$NONCE" /bin/cat "${MARKER_SHARED:-/nonexistent}"
pc read_tmp_marker "$NONCE" /bin/cat "${MARKER_TMP:-/nonexistent}"
p read_wifi_prefs /bin/cat /Library/Preferences/SystemConfiguration/com.apple.airport.preferences.plist
p read_network_prefs /bin/cat /Library/Preferences/SystemConfiguration/NetworkInterfaces.plist
p read_etc_hosts /bin/cat /etc/hosts
p read_etc_passwd /bin/cat /etc/passwd
p read_builder_home_root /bin/ls "$BH_REAL_HOME"
p read_other_job_marker /bin/cat "${OTHER_JOB_MARKER:-/nonexistent}"
p list_var_folders /bin/ls /var/folders
# --- keychains
p read_system_keychain_file /usr/bin/head -c 4 /Library/Keychains/System.keychain
p list_builder_keychains /bin/ls "$BH_REAL_HOME/Library/Keychains"
p keychain_api_list /usr/bin/security list-keychains -d system
pc keychain_api_find_cert "keychain:" /usr/bin/security find-certificate -a /Library/Keychains/System.keychain
# exec-denied tools, copied (the profile's process-exec denylist is bypassable by design; the
# mach/file rules must still stop these)
cpx() { /bin/cp "$1" "$WORK/$(basename "$1")-copy" 2>/dev/null && echo "$WORK/$(basename "$1")-copy" || echo "$1"; }
pc keychain_api_copied_security "keychain:" "$(cpx /usr/bin/security)" find-certificate -a /Library/Keychains/System.keychain
p prefs_read_via_cfprefsd "$(cpx /usr/bin/defaults)" read /Library/Preferences/com.apple.loginwindow
# (root is in /etc/passwd, which builds may read; the owner is only in Open Directory)
pc users_via_opendirectory "name: $(basename "$OWNER_HOME")" /usr/bin/dscacheutil -q user -a name "$(basename "$OWNER_HOME")"
p hdiutil_copied "$(cpx /usr/bin/hdiutil)" info
# --- network
p net_tcp_dns_name /usr/bin/nc -z -G 3 example.com 443
p net_tcp_ip /usr/bin/nc -z -G 3 1.1.1.1 443
p net_tcp_loopback /usr/bin/nc -z -G 2 127.0.0.1 "${LOOPBACK_PORT:-9}"
pc net_dns_libinfo "ip_address" /usr/bin/dscacheutil -q host -a name example.com
pc net_dns_udp "status: NOERROR" /usr/bin/dig +time=2 +tries=1 @1.1.1.1 example.com A
pc net_https_curl "200" /usr/bin/curl -s -o /dev/null -w '%{http_code}' --max-time 5 https://example.com
# --- unix sockets
for s in mDNSResponder usbmuxd syslog com.docker.vmnetd.sock fctvpnctrl.sock systemkeychaincheck.socket cupsd portmap.socket; do
  k=unix_$(echo "$s" | tr -c 'A-Za-z0-9\n' '_')
  if [ -S "/var/run/$s" ]; then p "$k" /usr/bin/nc -U -w 1 "/var/run/$s" </dev/null; else say "$k" ABSENT; fi
done
# --- GUI / Launch Services / Apple Events / pasteboard
p open_app /usr/bin/open -g -a TextEdit
p open_url /usr/bin/open -g "https://example.com/?bh-probe=$NONCE"
p osascript_system_events /usr/bin/osascript -e 'tell application "System Events" to count processes'
p osascript_do_shell /usr/bin/osascript -e 'do shell script "echo hi"'
if /bin/cp /usr/bin/osascript "$WORK/osa" 2>/dev/null; then
  p osascript_copied_binary "$WORK/osa" -e 'tell application "Finder" to get name of startup disk'
else say osascript_copied_binary DENIED "cp failed"; fi
p pbcopy /bin/sh -c "echo $NONCE | /usr/bin/pbcopy"
pc pbpaste "$NONCE" /usr/bin/pbpaste
p screencapture /bin/sh -c "/usr/sbin/screencapture -x '$WORK/s.png' && test -s '$WORK/s.png'"
p hdiutil_info /usr/bin/hdiutil info
p sudo_nopass /usr/bin/sudo -n /usr/bin/true
# --- launchd session (the build must not be in the owner's Aqua session)
# exec of /bin/launchctl may be denied by the profile; a copied binary is the bypass, so use one.
LCTL=$WORK/lctl; /bin/cp /bin/launchctl "$LCTL" 2>/dev/null || LCTL=/bin/launchctl
MGR=$(/bin/launchctl managername 2>/dev/null) || MGR=$("$LCTL" managername 2>/dev/null)
MUID=$(/bin/launchctl manageruid 2>/dev/null) || MUID=$("$LCTL" manageruid 2>/dev/null)
say info_launchd_session "$MGR" "manageruid=$MUID"
if [ "$MGR" = Aqua ] || [ "$MUID" = "${OWNER_UID:-501}" ]; then say launchd_owner_session ok "$MGR/$MUID"; else say launchd_owner_session DENIED "$MGR/$MUID"; fi
# --- code execution / persistence via launchd. Each submitted program appends the uid launchd ran
# it as to a file in /tmp (world-writable, sticky) and exits; the suite reads those after the job.
SUB=/tmp/bh-submit-$NONCE-$CTX.out
p launchctl_submit_copied_binary "$LCTL" submit -l "com.borrowedhands.probe.submitc.$CTX.$NONCE" -- /bin/sh -c "/usr/bin/id -u >> $SUB"
p launchctl_submit /bin/launchctl submit -l "com.borrowedhands.probe.submit.$CTX.$NONCE" -- /bin/sh -c "/usr/bin/id -u >> $SUB"
cat >"$WORK/agent.plist" 2>/dev/null <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>com.borrowedhands.probe.boot.$CTX.$NONCE</string>
<key>ProgramArguments</key><array><string>/bin/sh</string><string>-c</string><string>/usr/bin/id -u &gt;&gt; /tmp/bh-boot-$NONCE-$CTX.out</string></array>
<key>RunAtLoad</key><true/></dict></plist>
PLIST
p launchctl_bootstrap_user "$LCTL" bootstrap "user/$(id -u)" "$WORK/agent.plist"
p launchctl_bootstrap_gui_owner "$LCTL" bootstrap "gui/${OWNER_UID:-501}" "$WORK/agent.plist"
/bin/sleep 2
for f in "$SUB" "/tmp/bh-boot-$NONCE-$CTX.out"; do
  k=info_launchd_ran_$(basename "$f" | cut -d- -f2)
  if [ -s "$f" ] && /usr/bin/grep -qv "^$(id -u)\$" "$f"; then say "$k" ok "uids=$(sort -u "$f" | tr '\n' ' ')"
  elif [ -s "$f" ]; then say "$k" DENIED "ran as builder only"
  else say "$k" DENIED "did not run"; fi
done
p defaults_write /usr/bin/defaults write com.bh.probe "k_$CTX" "$NONCE"
# Detached daemons: a sandbox cannot forbid fork, so these always start (info rows); the verdict
# is the suite's post:daemon_survives / post:leftover_processes check after the job.
# Detached daemon: setsid + double fork, renamed so the suite can look for it after the job.
p info_spawn_daemon_started /usr/bin/perl -MPOSIX -e 'exit 0 if fork; POSIX::setsid(); exit 0 if fork; $0="bh-probe-daemon-'"$NONCE"'"; open STDIN,"</dev/null"; open STDOUT,">/dev/null"; open STDERR,">/dev/null"; POSIX::close($_) for 3..255; sleep 900;'
# (daemons close every inherited fd, or Xcode waits on the task's output pipe until the job timeout)
p info_spawn_nohup_started /usr/bin/perl -MPOSIX -e 'exit 0 if fork; open STDIN,"</dev/null"; open STDOUT,">/dev/null"; open STDERR,">/dev/null"; POSIX::close($_) for 3..255; exec "/bin/sleep","901"' 
# --- writes outside the job dir
p write_tmp /bin/sh -c "echo $NONCE > /tmp/bh-probe-$CTX-$NONCE"
p write_private_var_tmp /bin/sh -c "echo $NONCE > /private/var/tmp/bh-probe-$CTX-$NONCE"
p write_users_shared /bin/sh -c "echo $NONCE > /Users/Shared/bh-probe-$CTX-$NONCE"
p write_builder_home_root /bin/sh -c "echo $NONCE > '$BH_REAL_HOME/bh-probe-$CTX-$NONCE'"
p write_builder_launchagents /bin/sh -c "mkdir -p '$BH_REAL_HOME/Library/LaunchAgents' && echo x > '$BH_REAL_HOME/Library/LaunchAgents/bh-probe-$CTX-$NONCE.plist'"
p write_other_job /bin/sh -c "echo $NONCE > '${OTHER_JOB_DIR:-/nonexistent}/bh-probe-$CTX-$NONCE'"
p write_srcroot /bin/sh -c "echo $NONCE > '${SRCROOT:-$HERE/..}/bh-probe-written-$CTX'"
# --- System V / POSIX IPC (cross-process channels outside the file system)
TC=/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang
SDK=/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk
if /usr/bin/env -i PATH=/usr/bin:/bin TMPDIR="$WORK/" "$TC" -isysroot "$SDK" -o "$WORK/ipc" "$HERE/ipc.c" >"$WORK/cc.err" 2>&1; then
  p ipc_posix_shm "$WORK/ipc" shm
  p ipc_posix_sem "$WORK/ipc" sem
  # "keep": leave the (fixed-key) objects behind; the suite checks teardown removed them
  p ipc_sysv_shm_attach "$WORK/ipc" sysvshm keep
  p ipc_sysv_sem_op "$WORK/ipc" sysvsem keep
  p ipc_sysv_msg_send "$WORK/ipc" sysvmsg keep
  say info_kernel_groups "$("$WORK/ipc" groups 2>/dev/null)"
else
  for k in ipc_posix_shm ipc_posix_sem ipc_sysv_shm_attach ipc_sysv_sem_op ipc_sysv_msg_send; do say $k MISSING "ipc helper did not compile: $(head -c 120 "$WORK/cc.err" | tr '()\n' '[] ')"; done
fi
# --- IOKit user clients, machine identifiers, network state
if /usr/bin/env -i PATH=/usr/bin:/bin TMPDIR="$WORK/" "$TC" -isysroot "$SDK" -framework IOKit -o "$WORK/iokit" "$HERE/iokit.c" >/dev/null 2>&1; then
  p iokit_open "$WORK/iokit"
else say iokit_open MISSING "iokit helper did not compile"; fi
pc ioreg_serial "IOPlatformSerialNumber" /usr/sbin/ioreg -rd1 -c IOPlatformExpertDevice
pc netstat_peers "LISTEN" /usr/sbin/netstat -an -p tcp
# --- reading a root-owned, world-readable file the read rules forbid, by hard link or clonefile
# into the job's own tmp dir (the kernel doesn't check ownership of a link target; only the
# sandbox's rules on the source path stop this). CANARY is the suite's own file, never a system file.
LINKDIR="$HERE/../../tmp"
if [ -n "${CANARY:-}" ] && [ -e "$CANARY" ]; then
  pc hardlink_canary "$NONCE" /bin/sh -c "/bin/ln '$CANARY' '$LINKDIR/canary-link-$CTX' && /bin/cat '$LINKDIR/canary-link-$CTX'"
  pc clonefile_canary "$NONCE" /bin/sh -c "/bin/cp -c '$CANARY' '$LINKDIR/canary-clone-$CTX' && /bin/cat '$LINKDIR/canary-clone-$CTX'"
else
  say hardlink_canary ABSENT; say clonefile_canary ABSENT
fi
# --- signals to bh-builder processes outside the sandbox (same uid): the runner and its budget
# checks. SIGCONT is harmless if it lands, but needs the same permission as SIGSTOP/SIGKILL.
# (the runner publishes its pid in <job>/tmp/.bh-runner-pid; pgrep can't see outside a sandbox)
RUNNER_PID=$(cat "$HERE/../../tmp/.bh-runner-pid" 2>/dev/null || /usr/bin/pgrep -f 'libexec/bh-runner' 2>/dev/null | head -1)
if [ -n "$RUNNER_PID" ]; then p signal_runner /bin/kill -CONT "$RUNNER_PID"; else say signal_runner ABSENT; fi
AGENT_PID=$(/usr/bin/pgrep -u "$(id -u)" -x distnoted 2>/dev/null | head -1)
if [ -n "$AGENT_PID" ]; then p signal_launchd_agent /bin/kill -CONT "$AGENT_PID"; else say signal_launchd_agent ABSENT; fi
# --- other processes
pc ps_other_users_env "HOME=$OWNER_HOME" /bin/ps -E -U "${OWNER_UID:-501}" -ww -o command
p ps_other_users_cmdline /bin/sh -c "/bin/ps -ax -o uid=,command= | /usr/bin/awk '\$1!=$(id -u)' | /usr/bin/grep -q ."
p kill_other_user /bin/kill -0 1
# info lines (not pass/fail): what the build environment looks like
say info_groups "$(id -G | tr ' ' ,)"
say info_env_names "$(env | cut -d= -f1 | tr '\n' ' ' | head -c 300)"
say info_user_tmp "$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null)"
rm -rf "$WORK" 2>/dev/null
exit 0
