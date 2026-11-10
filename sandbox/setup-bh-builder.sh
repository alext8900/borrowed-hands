#!/bin/bash
# Borrowed Hands: create the isolated build user and install the runner. Idempotent.
#   sudo bash sandbox/setup-bh-builder.sh [--with-test-profiles]
# Creates (or verifies) user/group bh-builder uid/gid 470: own primary group (NOT staff), hidden,
# no password, shell /usr/bin/false, home /Users/Shared/.bh-builder-home mode 700. Installs
# root-owned code under /opt/borrowed-hands. Adds NO sudoers rule (see docs/SANDBOX.md for the
# production rule that lets the helper run bh-dispatch). Never touches other users or sudoers files.
set -euo pipefail
U=bh-builder; ID=470; HOME_DIR=/Users/Shared/.bh-builder-home; PREFIX=/opt/borrowed-hands
SRC=$(cd "$(dirname "$0")" && pwd)
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }

# claim_dir <path> <may-adopt-nonempty: 0|1>: create <path> exclusively as uid/gid 470 mode 0700, or
# adopt an existing one only if it is a real directory already owned by 470 with mode 0700 (and,
# unless adopting our own previous install, empty). /Users/Shared is world-writable and sticky, so
# anyone could have pre-created the path; never chown/chmod someone else's directory into ours.
claim_dir() {
  local d=$1 adopt=$2 parent; parent=$(dirname "$d")
  [ -d "$parent" ] && [ ! -L "$parent" ] || { echo "$parent is not a real directory; refusing" >&2; return 1; }
  if [ -L "$d" ]; then echo "$d is a symlink; refusing" >&2; return 1; fi
  if [ -e "$d" ]; then
    [ -d "$d" ] || { echo "$d exists and is not a directory; refusing" >&2; return 1; }
    local own mode; own=$(stat -f %u "$d"); mode=$(stat -f %Lp "$d")
    [ "$own" = $ID ] && [ "$mode" = 700 ] || { echo "$d exists (uid $own, mode $mode), not ours; refusing" >&2; return 1; }
    if [ "$adopt" != 1 ] && [ -n "$(ls -A "$d")" ]; then echo "$d exists and is not empty; refusing" >&2; return 1; fi
    return 0
  fi
  mkdir -m 700 "$d" || return 1                       # exclusive: fails if it appeared meanwhile
  chown -h $ID:$ID "$d"
  [ "$(stat -f %u:%Lp "$d")" = "$ID:700" ] && [ ! -L "$d" ] || { echo "$d changed under us; refusing" >&2; return 1; }
}
if [ "${1:-}" = --check-home ]; then   # test hook: run only claim_dir on a given path
  claim_dir "$2" 0; exit $?
fi

# --- per-user folders left by an earlier install (numeric uid reuse). macOS names a user's /private/var/folders
# folder after its GeneratedUID and uid. This script never sets GeneratedUID (and must never adopt or reset an old
# one), so a new bh-builder gets a new folder, but the old one stays, owned by uid 470, and any uid-470 process can
# reach what is in it by path. Before creating the account, refuse unless each such folder is verifiably the empty
# protected skeleton, by the teardown's own rules (sourcing it only defines functions). Read-only: nothing is removed.
# Skipped when bh-builder exists (its folder is in use) or another account holds uid 470 (refused below).
if ! dscl . -read /Users/$U >/dev/null 2>&1 &&
   ! dscl . -list /Users UniqueID | awk -v id=$ID '{ for (i = 2; i <= NF; i++) if ($i == id) f = 1 } END { exit !f }'; then
  # shellcheck source-path=SCRIPTDIR source=teardown-bh-builder.sh disable=SC1091
  source "$SRC/teardown-bh-builder.sh"
  caches_cleared 0 || {
    echo "uid $ID's per-user folders under /private/var/folders, left by an earlier Borrowed Hands install, could not" \
      "be verified empty; a new bh-builder would get uid $ID again and could reach them, so nothing was installed." \
      "Restart this Mac, then run 'bh setup' again before deleting anything by hand (macOS clears temporary folders" \
      "at startup for existing accounts; whether it also clears the folder of a deleted account is not verified)." >&2
    # The only path ever put in a command here is the per-user folder itself, and only when its name is one macOS
    # generates (two and thirty characters of its base32 alphabet), so the quoted word cannot hold anything a build
    # chose. Names inside it are build-chosen: they appear above only as escaped diagnostics.
    a='[0-9_bcdfghjklmnpqrstvwxyz]'
    if [[ $CHECKED_ROOT =~ ^/private/var/folders/$a{2}/$a{30}$ ]]; then
      echo "If it still cannot be verified, temporarily give your Terminal Full Disk Access (System Settings >" \
        "Privacy & Security > Full Disk Access), run: sudo rm -rf '$CHECKED_ROOT' (macOS keeps its protected" \
        "folders; this removes what the earlier install left), then remove that access and run 'bh setup' again." >&2
    else
      echo "If it still cannot be verified, inspect /private/var/folders by hand: no command is shown because the" \
        "folder could not be named safely." >&2
    fi
    exit 1
  }
fi

# Deleting bh-builder makes macOS ask '"<app>" would like to administer your computer' (TCC, attributed to the app
# running this terminal), and an unanswered prompt stays open after the uninstall ended; Allow then deletes the
# account. Creating the account has never been seen to ask (the unified log shows no SysAdminFiles request there),
# so a prompt on screen now is most likely such a leftover: say so instead of telling the user to click it.
echo "Creating the account has not been seen to ask for permission. If macOS shows a prompt asking whether your" \
  "terminal app may \"administer your computer\" now, it is most likely an earlier 'bh uninstall' still waiting to" \
  "delete bh-builder, and Allow would delete it: do not click Allow; finish 'bh uninstall' first" \
  "(docs/QUICKSTART.md, Uninstall)."
# --- group
if dscl . -read /Groups/$U >/dev/null 2>&1; then
  [ "$(dscl . -read /Groups/$U PrimaryGroupID | awk '{print $2}')" = $ID ] || { echo "group $U exists with wrong gid" >&2; exit 1; }
else
  dscl . -list /Groups PrimaryGroupID | awk '{print $2}' | grep -qx $ID && { echo "gid $ID taken" >&2; exit 1; }
  dscl . -create /Groups/$U
  dscl . -create /Groups/$U PrimaryGroupID $ID
  dscl . -create /Groups/$U Password '*'
fi
# --- user
HAD_USER=0
if dscl . -read /Users/$U >/dev/null 2>&1; then
  HAD_USER=1
  [ "$(dscl . -read /Users/$U UniqueID | awk '{print $2}')" = $ID ] || { echo "user $U exists with wrong uid" >&2; exit 1; }
else
  dscl . -list /Users UniqueID | awk '{print $2}' | grep -qx $ID && { echo "uid $ID taken" >&2; exit 1; }
  dscl . -create /Users/$U
  dscl . -create /Users/$U UniqueID $ID
fi
dscl . -create /Users/$U PrimaryGroupID $ID
dscl . -create /Users/$U UserShell /usr/bin/false
dscl . -create /Users/$U RealName "Borrowed Hands Builder"
dscl . -create /Users/$U NFSHomeDirectory $HOME_DIR
dscl . -create /Users/$U Password '*'
dscl . -create /Users/$U IsHidden 1
# Supplementary groups: remove any DIRECT membership (GroupMembership) in any group. Never edit
# other groups' NestedGroups: on macOS, everyone(12) and localaccounts(61) are implicit for every
# local user, and groups such as _lpoperator and com.apple.sharepoint.group.* nest them, so those
# show up in `id -G` for every local account and can't be removed per user without changing them
# for everyone. (InitGroups=false in the LaunchDaemon does not help: macOS resolves group
# membership dynamically, so the build's getgroups() still lists those implicit groups.)
for g in $(dscl . -list /Groups GroupMembership | awk -v u=$U '{for(i=2;i<=NF;i++) if($i==u) print $1}'); do
  dseditgroup -o edit -d $U -t user "$g"
done
for g in staff admin; do
  dseditgroup -o checkmember -m $U $g 2>/dev/null | grep -q '^yes' && { echo "$U is still in $g" >&2; exit 1; }
done
extra=$(id -G $U | tr ' ' '\n' | grep -vxE "$ID|12|61|100|70[1-9]" || true)
[ -z "$extra" ] || { echo "$U has unexpected groups: $extra" >&2; exit 1; }
# --- home: create exclusively or adopt only our own (see claim_dir)
[ -L /Users/Shared ] && { echo "/Users/Shared is a symlink; refusing" >&2; exit 1; }
claim_dir $HOME_DIR "$HAD_USER" || exit 1
claim_dir $HOME_DIR/jobs "$HAD_USER" || exit 1
# --- code (root-owned, not writable by anyone else)
install -d -o root -g wheel -m 755 $PREFIX $PREFIX/bin $PREFIX/libexec $PREFIX/libexec/profiles $PREFIX/run
install -o root -g wheel -m 755 "$SRC/bh-dispatch" $PREFIX/bin/bh-dispatch
install -o root -g wheel -m 755 "$SRC/bh-runner" $PREFIX/libexec/bh-runner
install -o root -g wheel -m 644 "$SRC/builder.sb" "$SRC/git.sb" $PREFIX/libexec/
# Test mode is opt-in: without --with-test-profiles, no alternative profile is installed and the
# TEST_MODE flag is removed, so bh-dispatch only ever uses builder.sb and https remotes.
rm -f $PREFIX/libexec/profiles/*.sb $PREFIX/TEST_MODE
if [ "${1:-}" = --with-test-profiles ]; then
  for f in "$SRC"/../tests/profiles/*.sb; do install -o root -g wheel -m 644 "$f" $PREFIX/libexec/profiles/; done
  install -o root -g wheel -m 644 /dev/null $PREFIX/TEST_MODE
  echo "TEST MODE: --profile/--allow-file-remote enabled (rerun without --with-test-profiles to disable)"
fi
id $U
echo "installed to $PREFIX. To remove Borrowed Hands from this Mac later, run: bh uninstall"
