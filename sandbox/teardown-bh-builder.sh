#!/bin/bash
# Borrowed Hands: remove the build user, its home, and /opt/borrowed-hands.
#   sudo bash sandbox/teardown-bh-builder.sh
# Touches only our own label (system/com.borrowedhands.runner), /opt/borrowed-hands, and the bh-builder user,
# group, home and per-user caches AFTER checking they are ours: the identity setup-bh-builder.sh creates (user
# bh-builder with exactly uid 470, primary gid 470, our home and shell; group bh-builder with exactly gid 470; home
# a real directory owned 470:470; no other account holding uid 470). uid 470's processes are first killed as uid 470
# (bh-dispatch --sweep-only); only right before the account is deleted, with that identity verified, does root
# boot out uid 470's launchd domains and kill what is left by uid (stop_uid_processes). A Directory Services query
# that fails is never read as "absent": if the accounts cannot be listed, nothing is removed. Anything not
# verifiably ours is left in place and reported, and the script exits 1.
set -uo pipefail
U=bh-builder; ID=470; HOME_DIR=/Users/Shared/.bh-builder-home; PREFIX=/opt/borrowed-hands
DELETE_TIMEOUT=120      # seconds for one `dscl . -delete /Users/bh-builder`; opendirectoryd gives up after ~310

refused=0
# Deleting the account. Observed on a real Mac (macOS 27.2, 2026-11-09), cause NOT proven: a per-user
# `distnoted agent` running as uid 470 appeared the second `dscl . -delete /Users/bh-builder` started, launchd
# respawned it after a kill, and the delete hung until opendirectoryd logged "ODRecordDelete ... active for 310
# seconds, forcing a timeout" and dscl failed with DS Error -14987 (eUndefinedError), twice in a row. With uid 470's
# processes stopped first, the delete finished in 37 s. So the delete runs only after stop_uid_processes, with a
# time limit, and is retried once after stopping them again. `sysadminctl -deleteUser` is not used: it also handles
# home folders and secure tokens, and its behavior for a hidden service account was not shown to be safe here.
# stop_uid_processes: only after the identity fence (uid 470 is verifiably our bh-builder and no one else's). Boots
# out uid 470's launchd domains so nothing respawns (absent domains are fine), kills uid 470's processes by uid, and
# polls until none remain. 0 then; 1 if some keep running or pgrep cannot tell.
stop_uid_processes() {
  local i rc
  launchctl bootout user/$ID >/dev/null 2>&1
  launchctl print gui/$ID >/dev/null 2>&1 && launchctl bootout gui/$ID >/dev/null 2>&1
  for i in 1 2 3 4 5 6 7 8 9 10; do
    pkill -KILL -U $ID
    pgrep -U $ID >/dev/null; rc=$?
    case $rc in
      1) return 0 ;;                                  # no process with real uid 470 left
      0) sleep 1 ;;
      *) echo "could not list uid $ID's processes (pgrep exit $rc)" >&2; return 1 ;;
    esac
  done
  echo "uid $ID still has processes after its launchd domains were booted out and it was killed 10 times:" \
    "$(pgrep -U $ID | tr '\n' ' ')" >&2
  return 1
}
# bounded <seconds> <command...>: run it in the background and wait at most that long; its exit code, or 124 if it
# had to be killed. (macOS has no timeout(1).) Only ever our own direct child is signalled, never its children, so
# the command must be the process itself (dscl), not a shell wrapper such as sh -c that would leave them running.
bounded() {
  local limit=$1 pid i=0; shift
  "$@" & pid=$!
  while kill -0 $pid 2>/dev/null; do
    if (( i++ >= limit )); then
      # bash may already have reaped the child, freeing its pid: signal it only while its parent is still this
      # shell, and with KILL at once (no TERM-then-wait window for the pid to change hands)
      [ "$(ps -o ppid= -p $pid 2>/dev/null | tr -d ' ')" = "$$" ] && kill -KILL $pid 2>/dev/null
      wait $pid 2>/dev/null       # bash reports the killed job ("Killed: 9") here: keep that off the output
      return 124
    fi
    sleep 1
  done
  wait $pid
}
# user_listed: 0 if a working `dscl . -list /Users` shows bh-builder, 1 if it works and does not, 2 if it fails (never
# read as "absent", as in main).
user_listed() {
  local users
  users=$(dscl . -list /Users UniqueID 2>/dev/null) || return 2
  awk -v u=$U '$1 == u { f = 1 } END { exit !f }' <<<"$users"
}
# delete_user: 0 once /Users/bh-builder is deleted; 1 (with the reason) if it is still there after one retry.
# opendirectoryd keeps a delete running after dscl gave up and may still finish it, so a failed attempt counts as
# done when a working listing no longer shows the account.
delete_user() {
  local try rc why=
  for try in 1 2; do
    stop_uid_processes || return 1
    bounded $DELETE_TIMEOUT dscl . -delete /Users/$U; rc=$?
    [ $rc = 0 ] && return 0
    if [ $rc = 124 ]; then why="it did not finish within $DELETE_TIMEOUT s"; else why="dscl exit $rc"; fi
    user_listed; if [ $? = 1 ]; then
      echo "deleting user $U reported failure ($why), but $U is no longer listed: deleted" >&2
      return 0
    fi
    [ $try = 1 ] && echo "deleting user $U failed ($why); stopping uid $ID's processes and retrying once" >&2
  done
  echo "could not delete user $U, twice ($why). opendirectoryd can time out a record delete it cannot finish: it" \
    "logs 'ODRecordDelete ... forcing a timeout' and dscl reports DS Error -14987 (eUndefinedError). Observed on" \
    "macOS 27.2, cause not proven: a uid-$ID distnoted agent starts during the delete; stopping uid $ID's processes" \
    "first let it complete. Check with: ps -U $ID; log show --last 15m --predicate 'process == \"opendirectoryd\"'" >&2
  return 1
}
refuse() { echo "left in place: $*" >&2; refused=1; }
# attr_is <record> <key> <value>: 0 if the attribute is exactly that one value, 1 if it is anything else (other
# value, extra values or text), 2 if it could not be read.
attr_is() {
  local out
  out=$(dscl . -read "$1" "$2" 2>/dev/null) || return 2
  [ "$out" = "$2: $3" ]
}
# user_is_ours: 0 yes, 1 no, 2 could not tell.
user_is_ours() {
  local rc
  attr_is /Users/$U UniqueID $ID; rc=$?; [ $rc = 0 ] || return $rc
  attr_is /Users/$U PrimaryGroupID $ID; rc=$?; [ $rc = 0 ] || return $rc
  attr_is /Users/$U NFSHomeDirectory "$HOME_DIR"; rc=$?; [ $rc = 0 ] || return $rc
  attr_is /Users/$U UserShell /usr/bin/false
}

# macOS keeps a per-user folder skeleton under /private/var/folders that root cannot remove: directories with the
# system no-unlink flag (sunlnk, SF_NOUNLINK, enforced by SIP), and SIP data vaults such as
# 0/com.apple.LaunchServices.dv that the kernel will not even let root stat. Nothing here clears system flags.
SF_NOUNLINK=1048576     # 0x00100000, <sys/stat.h>
# shown <path>: a path for a diagnostic, single-quoted (an embedded ' as '\'') so it is one unambiguous word that no
# shell option can expand: bash's %q leaves a mid-word # bare, a glob operator in zsh with extendedglob. A path with
# a raw control character (ls -b already escapes those in names) falls back to %q, whose $'...' form shows them
# escaped. Names under T/, C/ and X/ are chosen by build code, and ls -b leaves space, ~, $( and * as they are.
# Never put such a path into a command the owner is told to run.
shown() {
  case $1 in *[[:cntrl:]]*) printf '%q' "$1"; return ;; esac
  local q="'\\''" s=$1
  printf "'%s'" "${s//\'/$q}"
}
# entry_kind <path>: lstat only (a symlink reports itself, never its target). 0 a directory owned by uid 470 with
# SF_NOUNLINK; 3 the kernel refuses even root a stat (a data vault); 1 anything else; 2 could not be inspected.
entry_kind() {
  local out flags
  if out=$(LC_ALL=C stat -f '%HT:%u:%f' "$1" 2>&1); then
    flags=${out##*:}
    [[ $flags =~ ^(0|[1-9][0-9]{0,9})$ ]] || return 2
    [ "${out%:*}" = "Directory:$ID" ] && (( flags & SF_NOUNLINK )) && return 0
    return 1
  fi
  case $out in *": Operation not permitted") return 3 ;; esac
  return 2
}
# TemporaryItems (<folder>/T/TemporaryItems and <folder>/T/com.apple.trustd/TemporaryItems) has no sunlnk flag but
# is protected by TCC Full Disk Access (kTCCServiceSystemPolicyAllFiles): nobody here, root included, can list it,
# so rm -rf cannot empty it, and no uid-470 context can either. stat still works. Emptiness proof, used ONLY because
# it is the one thing that can be checked: on APFS a directory's st_size is 32 * (entries + 2) and its st_nlink is
# entries + 2. That is observed APFS behavior (macOS 27.2, for files, subdirectories, dotfiles, symlinks and fifos),
# not a documented API, and other file systems differ. So it is accepted only when every property is exactly what
# macOS creates; any deviation (name, place, type, owner, group, mode, flags, size, link count, a listing error
# other than EPERM, a volume that is not APFS) fails closed. See docs/research/tempitems-uid-reuse-investigation.md.
TEMP_ITEMS_EMPTY="Directory:$ID:$ID:40700:0:64:2"    # %HT:%u:%g:%p:%f:%z:%l: dir 0700, no flags, 0 entries
TEMP_ITEMS_KEPT=()
RECOVERY="Restart this Mac (macOS clears these temporary folders at startup), then run 'bh uninstall' again before"
RECOVERY+=" deleting anything by hand. If it still cannot be verified, temporarily give your Terminal Full Disk"
RECOVERY+=" Access (System Settings > Privacy & Security > Full Disk Access), rerun 'bh uninstall', then remove"
RECOVERY+=" that access."
# on_apfs <path>: 0 if the file system holding <path> is APFS. df statfs(2)s the path itself (so a mount point, a
# firmlink or /private/var -> Data volume resolve as the kernel sees them, not by matching a path prefix against the
# mount table) and with -T prints it only if its f_fstypename is in the list, else nothing and exit 1; -Y adds the
# type column, checked too.
on_apfs() {
  local out
  out=$(LC_ALL=C df -P -Y -T apfs "$1" 2>/dev/null) || return 1
  awk 'NR == 2 && $2 == "apfs" { ok = 1 } END { exit !(ok && NR == 2) }' <<<"$out"
}
# empty_temporary_items <path> <may-remove 0|1>: 0 if <path> is verified empty (stat first, so ls never lists
# through a symlink); with 1, then rmdir it: removed, or refused with EPERM (kept, still verified empty). rmdir is the
# only removal ever tried on it: it cannot delete contents, and ENOTEMPTY means it was not empty after all.
empty_temporary_items() {
  local p=$1 out why
  out=$(LC_ALL=C stat -f '%HT:%u:%g:%p:%f:%z:%l' "$p" 2>&1)
  if [ "$out" != "$TEMP_ITEMS_EMPTY" ]; then why="stat $out, not $TEMP_ITEMS_EMPTY"
  elif out=$(LC_ALL=C ls -1Ab "$p" 2>&1); then        # e.g. Full Disk Access: rm -rf should then have removed it
    if [ -z "$out" ]; then why="it can be listed and is empty, yet rm -rf left it"
    else why="it can be listed and is not empty"; fi
  elif [ "$out" != "ls: $p: Operation not permitted" ]; then why="listing failed with: $out"
  elif ! on_apfs "$p"; then why="not on an APFS volume, so its size does not prove it empty"
  elif [ "$2" != 1 ]; then TEMP_ITEMS_KEPT+=("$p"); return 0
  elif out=$(LC_ALL=C rmdir "$p" 2>&1); then return 0
  elif [ "$out" = "rmdir: $p: Operation not permitted" ]; then TEMP_ITEMS_KEPT+=("$p"); return 0
  else why="rmdir: $out"
  fi
  echo "left behind (protected TemporaryItems folder, could not verify it is empty: $why): $(shown "$p")" >&2
  return 1
}
# protected_tree <dir> <per-user folder> <may-remove 0|1>: 0 if everything left in <dir> (already a protected
# folder) is itself the protected skeleton, recursively, or a verified-empty TemporaryItems at exactly its place;
# 1 if anything removable or uninspectable is left. A listing that fails is never "empty"; ls -A lists names without
# stat-ing them, and -b escapes odd bytes so a crafted name cannot pose as two entries.
protected_tree() {
  local names e
  names=$(LC_ALL=C ls -1Ab "$1") || { echo "could not list $(shown "$1")" >&2; return 1; }
  while IFS= read -r e; do
    [ -n "$e" ] || continue
    entry_kind "$1/$e"
    case $? in
      0) protected_tree "$1/$e" "$2" "$3" || return 1 ;;
      3) ;;                 # a data vault inside a protected folder: nobody can read or remove it
      *) case "$1/$e" in
           "$2/T/TemporaryItems" | "$2/T/com.apple.trustd/TemporaryItems") empty_temporary_items "$1/$e" "$3" || return 1 ;;
           *) echo "left behind (not a macOS-protected folder): $(shown "$1/$e")" >&2; return 1 ;;
         esac ;;
    esac
  done <<<"$names"
}
# caches_cleared [may-remove: 1, or 0 for setup's read-only preflight]: 0 if nothing of uid 470's is left under
# /private/var/folders but the protected skeleton (reported, one line per folder); 1 if anything else is left or the
# check itself fails; then CHECKED_ROOT is the per-user folder it failed in ("" if the search itself failed).
CHECKED_ROOT=
# shellcheck disable=SC2034  # CHECKED_ROOT is read by setup-bh-builder.sh, which sources this file
caches_cleared() {
  local may=${1:-1} roots root kept=()
  CHECKED_ROOT=
  roots=$(find /private/var/folders -maxdepth 2 -user $ID -prune 2>/dev/null) || return 1
  while IFS= read -r root; do
    [ -n "$root" ] || continue
    CHECKED_ROOT=$root
    entry_kind "$root" || { echo "left behind (not a macOS-protected folder): $(shown "$root")" >&2; return 1; }
    protected_tree "$root" "$root" "$may" || return 1
    kept+=("$root")
  done <<<"$roots"
  for root in ${kept[@]+"${kept[@]}"}; do
    echo "left by macOS (protected per-user folders, nothing removable left): $(shown "$root"); they stay owned" \
      "by uid $ID and setup checks them before it creates bh-builder again; nothing to do"
  done
  for root in ${TEMP_ITEMS_KEPT[@]+"${TEMP_ITEMS_KEPT[@]}"}; do
    echo "left by macOS (protected TemporaryItems folder, verified empty by its size and link count): $(shown "$root")"
  done
}

main() {
  [ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
  cd /
  launchctl print system/com.borrowedhands.runner >/dev/null 2>&1 && launchctl bootout system/com.borrowedhands.runner
  local users groups others ours=0 had_user=0 had_group=0 fail=0 kind rc
  users=$(dscl . -list /Users UniqueID 2>/dev/null) && groups=$(dscl . -list /Groups PrimaryGroupID 2>/dev/null) || {
    echo "could not inspect the user accounts (dscl failed); nothing was removed. Retry, or check: dscl . -list /Users" >&2
    exit 1
  }
  awk -v u=$U '$1 == u { f = 1 } END { exit !f }' <<<"$users" && had_user=1
  awk -v u=$U '$1 == u { f = 1 } END { exit !f }' <<<"$groups" && had_group=1
  # any other account with uid 470 among its values: the sweep kills by numeric uid, so it must not run at all
  others=$(awk -v u=$U -v id=$ID '$1 != u { for (i = 2; i <= NF; i++) if ($i == id) print $1 }' <<<"$users")
  if [ -n "$others" ]; then
    refuse "uid $ID also belongs to $(echo $others); not sweeping uid $ID or removing the build user, group, home" \
      "or caches"
  elif [ $had_user = 1 ]; then
    user_is_ours; rc=$?
    if [ $rc = 0 ]; then
      # Kill uid 470's processes from a child that has become uid 470 (bh-dispatch --sweep-only):
      # the kernel then can't let it signal anything else.
      local swept=0
      if [ -x $PREFIX/bin/bh-dispatch ]; then
        for _ in 1 2 3; do $PREFIX/bin/bh-dispatch --sweep-only >/dev/null && { swept=1; break; }; sleep 1; done
      fi
      if [ $swept != 1 ]; then
        echo "could not sweep uid $ID's processes (3 tries); NOT removing the user. Investigate: ps -U $ID" >&2
        exit 1
      fi
      # Files uid 470 owns elsewhere (per-user temp/cache dirs under /var/folders), while uid 470 is still
      # verifiably our account: once the account is gone nothing can authorize this again, so on failure keep the
      # account and the dispatcher and stop, and a rerun verifies, sweeps and retries. rm -rf removes all it can and
      # fails on macOS's protected skeleton, so judge what is left, not rm's exit code.
      find /private/var/folders -maxdepth 2 -user $ID -prune -exec rm -rf {} + 2>/dev/null
      if ! caches_cleared 1; then
        echo "could not remove uid $ID's caches under /private/var/folders; kept user $U and $PREFIX so" \
          "'bh uninstall' (or this script) can retry. $RECOVERY" >&2
        exit 1
      fi
      # Keep the dispatcher on failure too: a rerun needs it to sweep before it may try again.
      if ! delete_user; then
        echo "kept user $U and $PREFIX so 'bh uninstall' (or this script) can retry" >&2
        exit 1
      fi
      ours=1
    elif [ $rc = 2 ]; then
      refuse "could not inspect user $U (dscl . -read /Users/$U failed); not removing it, its group, home or caches"
    else
      refuse "user $U is not the Borrowed Hands build user (expected exactly uid $ID, gid $ID, home $HOME_DIR," \
        "shell /usr/bin/false); not removing it, its group, home or caches"
    fi
  fi
  # The group, home and caches are ours only while no other account holds uid 470 and the user is ours or gone.
  local clear=0
  [ -z "$others" ] && { [ $ours = 1 ] || [ $had_user = 0 ]; } && clear=1
  if [ $had_group = 1 ] && [ $clear = 1 ]; then
    attr_is /Groups/$U PrimaryGroupID $ID; rc=$?
    if [ $rc = 0 ]; then
      dscl . -delete /Groups/$U || { echo "could not delete group $U" >&2; fail=1; }
    elif [ $rc = 2 ]; then
      refuse "could not inspect group $U (dscl . -read /Groups/$U failed)"
    else
      refuse "group $U does not have exactly gid $ID ($(dscl . -read /Groups/$U PrimaryGroupID 2>/dev/null | tr '\n' ' '))"
    fi
  fi
  kind=$(stat -f '%HT:%u:%g' "$HOME_DIR" 2>/dev/null || true)       # lstat: a symlink reports itself
  if [ -n "$kind" ] && [ "$kind" != "Directory:$ID:$ID" ]; then
    refuse "$HOME_DIR is not a bh-builder directory ($kind)"
  elif [ -n "$kind" ] && [ $clear = 0 ]; then
    refuse "$HOME_DIR: its account was not verified as ours and removed"
  elif [ -n "$kind" ]; then           # ours, or orphaned by an earlier partial teardown (no account holds uid 470)
    # Root must not change metadata on inodes uid 470 merely linked in (the kernel lets any user
    # hard-link any file on the volume). Only touch uid-470-owned dirs and single-link files on
    # this filesystem; find does not follow symlinks by default. rm -rf only unlinks.
    find -x "$HOME_DIR" -user $ID \( -type d -o \( -type f -links 1 \) \) -exec chflags 0 {} + \
      || echo "warning: could not clear flags under $HOME_DIR" >&2
    find -x "$HOME_DIR" -user $ID -type d -exec chmod u+rwx {} + \
      || echo "warning: could not reset modes under $HOME_DIR" >&2
    rm -rf "$HOME_DIR" || { echo "could not remove $HOME_DIR" >&2; fail=1; }
  fi
  if [ -d "$PREFIX" ]; then rm -rf "$PREFIX" || { echo "could not remove $PREFIX" >&2; fail=1; }; fi
  [ $fail = 0 ] && [ $refused = 0 ] || exit 1
  echo "removed $U, $HOME_DIR, $PREFIX"
}

# Sourcing defines the functions only (the unit-test harness stubs every command); running the script removes.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
