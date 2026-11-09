#!/bin/bash
# Borrowed Hands: remove the build user, its home, and /opt/borrowed-hands.
#   sudo bash sandbox/teardown-bh-builder.sh
# Touches only our own label (system/com.borrowedhands.runner), /opt/borrowed-hands, and the bh-builder user,
# group, home and per-user caches AFTER checking they are ours: the identity setup-bh-builder.sh creates (user
# bh-builder with exactly uid 470, primary gid 470, our home and shell; group bh-builder with exactly gid 470; home
# a real directory owned 470:470; no other account holding uid 470). uid 470's processes are killed as uid 470,
# never by root. A Directory Services query that fails is never read as "absent": if the accounts cannot be
# listed, nothing is removed. Anything not verifiably ours is left in place and reported, and the script exits 1.
set -uo pipefail
U=bh-builder; ID=470; HOME_DIR=/Users/Shared/.bh-builder-home; PREFIX=/opt/borrowed-hands

refused=0
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
# protected_tree <dir>: 0 if everything left in <dir> (already a protected folder) is itself the protected skeleton,
# recursively; 1 if anything removable or uninspectable is left. A listing that fails is never "empty"; ls -A
# lists names without stat-ing them, and -b escapes odd bytes so a crafted name cannot pose as two entries.
protected_tree() {
  local names e
  names=$(LC_ALL=C ls -1Ab "$1") || { echo "could not list $1" >&2; return 1; }
  while IFS= read -r e; do
    [ -n "$e" ] || continue
    entry_kind "$1/$e"
    case $? in
      0) protected_tree "$1/$e" || return 1 ;;
      3) ;;                 # a data vault inside a protected folder: nobody can read or remove it
      *) echo "left behind (not a macOS-protected folder): $1/$e" >&2; return 1 ;;
    esac
  done <<<"$names"
}
# caches_cleared: after the removal, 0 if nothing of uid 470's is left under /private/var/folders but the protected
# skeleton (reported, one line per folder); 1 if anything else is left or the check itself fails.
caches_cleared() {
  local roots root kept=()
  roots=$(find /private/var/folders -maxdepth 2 -user $ID -prune 2>/dev/null) || return 1
  while IFS= read -r root; do
    [ -n "$root" ] || continue
    entry_kind "$root" || { echo "left behind (not a macOS-protected folder): $root" >&2; return 1; }
    protected_tree "$root" || return 1
    kept+=("$root")
  done <<<"$roots"
  for root in ${kept[@]+"${kept[@]}"}; do
    echo "left by macOS (protected per-user folders, nothing removable left): $root; macOS removes or reuses" \
      "these; nothing to do"
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
      if ! caches_cleared; then
        echo "could not remove uid $ID's caches under /private/var/folders; kept user $U and $PREFIX so" \
          "'bh uninstall' (or this script) can retry" >&2
        exit 1
      fi
      dscl . -delete /Users/$U && ours=1 || { echo "could not delete user $U" >&2; fail=1; }
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
