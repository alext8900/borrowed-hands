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
