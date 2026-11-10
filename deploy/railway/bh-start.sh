#!/bin/sh
# Seed HERMES_HOME (a Railway volume at /opt/data) with the Borrowed Hands plugin, then hand off to the stock
# Hermes entrypoint unchanged (exec keeps the PID). Volume handling was verified in a Railway spike.
#  - the plugin dir is replaced on every boot, so the image version wins;
#  - config.default.yaml is copied only when no config.yaml exists; an existing one is never overwritten, only
#    given the plugins.enabled entry if that is missing (comments and other keys kept);
#  - plugin-data/ (server key, pairings, job db) is never touched.
#  - optional GitHub push credential (BH_GITHUB_TOKEN, sealed, + BH_GITHUB_REPO=owner/name): written by
#    bh-github-creds.py to two 0600 files in /etc/bh-github/creds (hermes 0700 dir in a root 0755 dir on the
#    container filesystem: new in every container, never on the volume) plus a root-owned, URL-scoped git config;
#    removed again when the token variable is removed or invalid. Every start also sweeps the old volume location
#    $H/.config/bh-github. This runs first, before any check that can exit, so a start without the token always
#    removes an old credential.
#    Both variables are unset on the next lines, before any child runs, so no child of this script, nothing in s6's
#    container environment and nothing under Hermes inherits them (the repo too, in case the token is pasted into the
#    wrong one). The kernel's copy in /proc/<this pid>/environ keeps them until the final exec replaces this process;
#    nothing of the gateway's uid runs before then. They reach the helper on stdin (printf is a builtin: never argv)
#    and are never printed. Never add set -x.
#  - the script's own commands use a fixed system PATH (the image PATH puts /opt/data/.local/bin, which the gateway
#    can write, ahead of the system dirs); the caller's PATH is restored for the exec.
# BH_ROOT, BH_PY, BH_ENTRYPOINT and BH_GITCONFIG exist for tests; the image uses the defaults.
set -e
bh_github_token="${BH_GITHUB_TOKEN-}"
bh_github_repo="${BH_GITHUB_REPO-}"
unset BH_GITHUB_TOKEN BH_GITHUB_REPO
bh_path_set="${PATH+x}"
bh_path="${PATH-}"
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
H="${HERMES_HOME:-/opt/data}"
R="${BH_ROOT:-/opt/bh}"
PYBIN="${BH_PY:-/opt/hermes/.venv/bin/python}"
ENTRY="${BH_ENTRYPOINT:-/opt/hermes/docker/entrypoint-dispatch.sh}"
GC="${BH_GITCONFIG:-/etc/bh-github/gitconfig}"
# Never fails the boot: any refusal or error leaves no usable credential and Hermes starts as in V1.
if ! {
  printf '%s\000%s' "$bh_github_repo" "$bh_github_token" | \
    "$PYBIN" -I "$R/bh-github-creds.py" "$H" "$GC"
}; then
  # The git config's directory is root's and off the volume, and nothing of the gateway's uid runs yet, so removing
  # it and the credential directory here is safe (rm -r never follows a link inside). The old volume location
  # could not be checked.
  rm -rf -- "$GC" "${GC%/*}/creds"
  echo "[bh-start] github credentials: helper failed; git config and ${GC%/*}/creds removed, but the old" \
    "location $H/.config/bh-github was NOT checked and may still hold an old token; revoke it at GitHub if in doubt" >&2
fi
bh_github_token= bh_github_repo=
# The gateway owns this directory. Root must never follow a config entry it can replace.
if [ -L "$H/config.yaml" ] || { [ -e "$H/config.yaml" ] && [ ! -f "$H/config.yaml" ]; }; then
  echo "[bh-start] ERROR: config.yaml must be a regular file, not a symlink" >&2
  exit 1
fi
# Refuse parent links before root deletes or copies anything beneath them.
for directory in plugins plugin-data; do
  if [ -L "$H/$directory" ]; then
    echo "[bh-start] ERROR: $directory must not be a symlink" >&2
    exit 1
  fi
done
mkdir -p "$H/plugins" "$H/plugin-data"
rm -rf "$H/plugins/borrowed-hands"
cp -r "$R/plugins/borrowed-hands" "$H/plugins/borrowed-hands"
if [ ! -f "$H/config.yaml" ]; then
  "$PYBIN" - "$R/config.default.yaml" "$H/config.yaml" <<'PYSEED'
import os
from pathlib import Path
import sys
import tempfile

source, destination = map(Path, sys.argv[1:])
data = source.read_bytes()
fd, tmp = tempfile.mkstemp(prefix=".bh-config-", dir=destination.parent)
try:
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    # Exclusive publication also refuses a symlink planted after the shell check.
    os.link(tmp, destination)
finally:
    os.unlink(tmp)
PYSEED
  echo "[bh-start] config.yaml created" >&2
else
  "$PYBIN" - "$H/config.yaml" <<'PYCONFIG'
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
from ruamel.yaml import YAML


def merge(p):
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    with open(os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)) as f:
        original = os.fstat(f.fileno())
        if not stat.S_ISREG(original.st_mode):
            raise ValueError("config.yaml must be a regular file")
        d = y.load(f)
    # Empty/comment-only documents and explicit nulls are not an instruction to replace user policy.
    if not isinstance(d, dict):
        raise ValueError("config.yaml must be a non-empty mapping")
    pl = d.get("plugins", {})
    if not isinstance(pl, dict):
        raise ValueError("plugins must be a mapping")
    en = pl.get("enabled", [])
    if not isinstance(en, list) or any(not isinstance(item, str) for item in en):
        raise ValueError("plugins.enabled must be a list of strings")
    if "borrowed-hands" in en:
        print("[bh-start] config.yaml: borrowed-hands already enabled, untouched", file=sys.stderr)
        return
    d["plugins"] = pl
    pl["enabled"] = en
    en.append("borrowed-hands")
    # Serialize before touching disk, then publish only a fully written and fsynced sibling file.
    out = io.StringIO()
    y.dump(d, out)
    fd, tmp = tempfile.mkstemp(prefix=".bh-config-", dir=Path(p).parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(out.getvalue())
            f.flush()
            os.fchown(f.fileno(), original.st_uid, original.st_gid)
            os.fchmod(f.fileno(), stat.S_IMODE(original.st_mode))
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print("[bh-start] config.yaml: added borrowed-hands to plugins.enabled", file=sys.stderr)


if __name__ == "__main__":
    try:
        merge(sys.argv[1])
    except Exception as exc:
        # YAML parser errors can include config values. Emit only our validation diagnostics or error type.
        reason = str(exc) if type(exc) is ValueError else type(exc).__name__
        print(f"[bh-start] ERROR: config merge refused ({reason}); original unchanged; "
              "repair config.yaml before restarting", file=sys.stderr)
        sys.exit(1)
PYCONFIG
fi
if [ "$(id -u)" = 0 ]; then
  chown -R -P hermes:hermes "$H/plugins" "$H/plugin-data" "$H/config.yaml"
fi
# Repair logs from earlier images that created them as root. Fresh logs are created by stock stage2 AS hermes.
# Never leave a root tail following a path the gateway can replace; inspect agent.log as hermes instead.
if [ -L "$H/logs" ]; then
  echo "[bh-start] ERROR: logs must not be a symlink" >&2
  exit 1
fi
if [ "$(id -u)" = 0 ] && [ -d "$H/logs" ]; then
  chown -R -P hermes:hermes "$H/logs"
fi
echo "[bh-start] seeded $H uid=$(id -u) pid=$$" >&2
# Hand git the credential config through the environment, the only place that names it; git reads it after every
# config file. Its `helper =` reset then drops helpers a user-level config adds for the repository URL (the bundled
# GitHub skill suggests `credential.helper store`), so git never copies the token into ~/.git-credentials. A child
# started without these variables (Hermes's execute_code, stdio MCP servers) gets no credential. Not secret.
bh_n="${GIT_CONFIG_COUNT:-0}"
case "$bh_n" in ''|*[!0-9]*|0?*|?????*) bh_n=0 ;; esac
eval "GIT_CONFIG_KEY_$bh_n=include.path GIT_CONFIG_VALUE_$bh_n=\"\$GC\"; export GIT_CONFIG_KEY_$bh_n GIT_CONFIG_VALUE_$bh_n"
GIT_CONFIG_COUNT=$((bh_n + 1))
export GIT_CONFIG_COUNT
if [ -n "$bh_path_set" ]; then PATH="$bh_path"; else unset PATH; fi
exec "$ENTRY" "$@"
