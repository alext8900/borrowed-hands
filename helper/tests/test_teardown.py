"""sandbox/teardown-bh-builder.sh (R1): it must only remove a bh-builder account, group, home and caches that are
verifiably ours, and leave anything else in place with a nonzero exit.

The harness sources the script (which only defines functions and constants), points PREFIX and HOME_DIR at a temp
directory, replaces every destructive or privileged command (dscl, rm, find, chflags, chmod, stat, launchctl, id,
sleep) with a shell function that only logs, then calls `main`. Nothing is deleted and nothing runs as root."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from borrowed_hands_helper._shared import SANDBOX

SCRIPT = SANDBOX / "teardown-bh-builder.sh"
CACHES = "find /private/var/folders -maxdepth 2 -user 470"
OURS_USER = {"UniqueID": "470", "PrimaryGroupID": "470", "NFSHomeDirectory": "/Users/Shared/.bh-builder-home",
             "UserShell": "/usr/bin/false"}

EPERM, MISSING = "EPERM", "MISSING"
STUBS = r'''
log() { printf '%s\n' "$*" >> "$T/log"; }
id() { echo 0; }
sleep() { /bin/sleep 0.02; }         # every "second" is 20 ms: the bounded delete still waits, just briefly
# nextof <name>: the next line of $T/<name> on each call (empty once it runs out): scripted answers in order
nextof() { local n; n=$(cat "$T/$1.n" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$T/$1.n"
           sed -n "${n}p" "$T/$1" 2>/dev/null; }
launchctl() {
  log "launchctl $*"
  case "$1 $2" in "print gui/470") [ -f "$T/gui" ] && return 0 ;; esac
  [ "$1" = print ] && return 113
  return 0
}
pkill() { log "pkill $*"; return 1; }
kill() { [ "$1" = -0 ] || log "kill $*"; builtin kill "$@"; }
# ps -o ppid= -p <pid>: the real answer, or $T/ppid (another parent: the pid now belongs to someone else)
ps() { log "ps $*"; if [ -f "$T/ppid" ]; then cat "$T/ppid"; else command ps "$@"; fi; }
pgrep() { log "pgrep $*"; case $(nextof pgrep) in found) echo 4242; return 0 ;; error) return 3 ;; esac; return 1; }
rm() { log "rm $*"; }
find() { log "find $*"; case "$*" in *-exec*) ;; *) cat "$T/roots" ;; esac; }
chflags() { log "chflags $*"; }
chmod() { log "chmod $*"; }
stat() {
  local p fmt=; for p; do :; done; printf '%s\n' "$*" >> "$T/statlog"; [ "$1" = -f ] && fmt=$2
  local k=${p//\//|}           # the fake tree is flat: one file per path
  if [ -f "$T/eperm/$k" ]; then echo "stat: $p: stat: Operation not permitted" >&2; return 1; fi
  if [ "$fmt" = '%HT:%u:%g:%p:%f:%z:%l' ] && [ -f "$T/full/$k" ]; then cat "$T/full/$k"; return; fi
  if [ -f "$T/fstat/$k" ]; then cat "$T/fstat/$k"; return; fi
  [ -f "$T/stat$p" ] && cat "$T/stat$p" || return 1
}
ls() {
  local p d; for p; do :; done; log "ls $*"; d=${p//\//|}
  if [ -f "$T/lserr/$d" ]; then echo "ls: $p: $(cat "$T/lserr/$d")" >&2; return 1; fi
  [ -f "$T/ls/$d" ] && cat "$T/ls/$d" || return 1
}
# df -P -Y -T apfs <path>: like the real one, prints the header and the path's file system only when its type is in
# the -T list, else nothing and exit 1. fstype "lie" prints a non-APFS row with exit 0 (a df that ignored -T).
df() {
  log "df $*"; local ty; ty=$(cat "$T/fstype")
  case " $* " in *" -T apfs "*) ;; *) return 2 ;; esac
  case $ty in
    apfs) printf 'Filesystem    Type 512-blocks Used Available Capacity Mounted on\n/dev/disk3s5  apfs 8 4 4 50%% /System/Volumes/Data\n' ;;
    apfs-err) printf 'Filesystem    Type 512-blocks Used Available Capacity Mounted on\n/dev/disk3s5  apfs 8 4 4 50%% /System/Volumes/Data\n'
              return 1 ;;
    lie) printf 'Filesystem    Type 512-blocks Used Available Capacity Mounted on\n/dev/disk4s1  hfs 8 4 4 50%% /Volumes/X\n' ;;
    *) return 1 ;;
  esac
}
rmdir() {
  log "rmdir $*"; local r; r=$(cat "$T/rmdir")
  [ "$r" = ok ] && return 0
  echo "rmdir: $1: $r" >&2; return 1
}
dscl_base() {
  # dscl . -read <path> [attr] | -list <dir> <attr> | -delete <path>
  case "$2" in
    -read) [ -f "$T/ds$3" ] || return 56
           if [ $# -ge 4 ]; then grep "^$4: " "$T/ds$3" || return 56; else cat "$T/ds$3"; fi ;;
    -list) [ -f "$T/listfail" ] && return 70
           for f in "$T/ds$3"/*; do [ -f "$f" ] || continue
             printf '%s %s\n' "$(basename "$f")" "$(sed -n "s/^$4: //p" "$f")"; done ;;
    -delete) log "dscl-delete $3"
             case "$3" in /Users/*)
               case $(nextof delete) in
                 hang) exec /bin/sleep 30 ;;          # only ever run in the background by the bounded delete
                 hangshort) exec /bin/sleep 2 ;;      # for tests where nothing may kill it
                 # opendirectoryd finished the delete although dscl hung or failed (the record is gone)
                 hangdel) /bin/rm -f "$T/ds$3"; exec /bin/sleep 30 ;;
                 errdel) /bin/rm -f "$T/ds$3"; echo "<dscl_cmd> DS Error: -14136 (eDSRecordNotFound)" >&2; return 1 ;;
                 errdel_listfail) /bin/rm -f "$T/ds$3"; : > "$T/listfail"; return 1 ;;
                 err) echo "<dscl_cmd> DS Error: -14987 (eUndefinedError)" >&2; return 1 ;;
               esac ;;
             esac ;;
  esac
}
dscl() { dscl_base "$@"; }
'''


def make_tree(t: Path, folders=None, unlistable=(), full=None, lserr=None, fstype="apfs",
              rmdir="Operation not permitted", users=(), groups=(), deletes=(), pgreps=(), gui=False, ppid=None):
    """The fake world the stubs read. folders: what is left of uid 470 under /private/var/folders after the
    removal, {path: lstat answer}: the '%HT:%u:%f' text, EPERM (the kernel refuses even root a stat: a SIP data
    vault) or MISSING (listed, but the stat fails some other way). Depth-two paths are what the second `find`
    reports; each directory not in `unlistable` or `lserr` gets an `ls` listing of its children.
    full: {path: '%HT:%u:%g:%p:%f:%z:%l' text} for TemporaryItems candidates (added to `folders` in the short
    form); listing them fails with lserr[path] (default "Operation not permitted"; None: listable, empty).
    fstype: what `df -T apfs` sees; rmdir: "ok" or the error text rmdir prints.
    deletes: each `dscl . -delete /Users/...` in turn: "ok", "hang" (never finishes) or "err" (DS Error -14987);
    pgreps: each `pgrep -U 470` in turn: "none", "found" or "error"; gui: a gui/470 launchd domain exists.
    ppid: what `ps -o ppid=` answers instead of the truth (the delete's pid now belongs to someone else)."""
    (t).mkdir(parents=True, exist_ok=True)
    if ppid is not None:
        (t / "ppid").write_text(ppid + "\n")
    (t / "delete").write_text("".join(d + "\n" for d in deletes))
    (t / "pgrep").write_text("".join(d + "\n" for d in pgreps))
    if gui:
        (t / "gui").write_text("")
    full, lserr, folders = dict(full or {}), dict(lserr or {}), dict(folders or {})
    for p, text in full.items():
        ty, uid, _gid, _mode, flags, _size, _nlink = text.split(":")
        folders[p] = f"{ty}:{uid}:{flags}"
        lserr.setdefault(p, "Operation not permitted")
    for d in ("ds/Users", "ds/Groups", "prefix/bin", "eperm", "fstat", "ls", "full", "lserr"):
        (t / d).mkdir(parents=True, exist_ok=True)
    for p, text in full.items():
        (t / "full" / p.replace("/", "|")).write_text(text + "\n")
    for p, msg in lserr.items():
        if msg is not None:
            (t / "lserr" / p.replace("/", "|")).write_text(msg + "\n")
    lserr = {p for p, msg in lserr.items() if msg is not None}
    for p, kind in folders.items():
        key = p.replace("/", "|")
        if kind != MISSING:
            (t / ("eperm" if kind == EPERM else "fstat") / key).write_text("" if kind == EPERM else kind + "\n")
        if kind.startswith("Directory") and p not in unlistable and p not in lserr:
            kids = sorted(c.rsplit("/", 1)[1] for c in folders if c.rsplit("/", 1)[0] == p)
            (t / "ls" / key).write_text("".join(k + "\n" for k in kids))
    (t / "roots").write_text("".join(p + "\n" for p in folders if p.count("/") == 5))   # /private/var/folders/x/y
    (t / "statlog").write_text("")
    (t / "fstype").write_text(fstype + "\n")
    (t / "rmdir").write_text(rmdir + "\n")
    (t / "log").write_text("")
    for name, attrs in users:
        (t / "ds/Users" / name).write_text("".join(f"{k}: {v}\n" for k, v in attrs.items())
                                           .replace("/Users/Shared/.bh-builder-home", str(t / "home")))
    for name, gid in groups:
        (t / "ds/Groups" / name).write_text(f"PrimaryGroupID: {gid}\n")


def run_teardown(tmp_path: Path, users=(), groups=(), home_stat=None, sweep_rc=0, extra="", folders=None,
                 unlistable=(), main="main", **tree):
    """See make_tree for folders, unlistable and **tree (full, lserr, fstype, rmdir)."""
    t = tmp_path / "t"
    make_tree(t, folders, unlistable, users=users, groups=groups, **tree)
    home = t / "home"
    if home_stat is not None:
        (t / f"stat{home}").parent.mkdir(parents=True, exist_ok=True)
        (t / f"stat{home}").write_text(home_stat + "\n")
    disp = t / "prefix/bin/bh-dispatch"
    disp.write_text(f"#!/bin/bash\necho \"sweep $*\" >> '{t}/log'\nexit {sweep_rc}\n")
    os.chmod(disp, 0o755)
    body = (f"T='{t}'\nset -uo pipefail\nsource '{SCRIPT}'\n{STUBS}\n{extra}\n"
            f"PREFIX='{t}/prefix'; HOME_DIR='{home}'\n{main}\n")
    p = subprocess.run(["/bin/bash", "-c", body], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin"})
    return p.returncode, (t / "log").read_text(), p.stdout + p.stderr, str(home), f"{t}/prefix"


def test_our_install_is_removed_completely(tmp_path):
    rc, log, out, home, prefix = run_teardown(tmp_path, users=[("bh-builder", OURS_USER)],
                                              groups=[("bh-builder", "470")], home_stat="Directory:470:470")
    assert rc == 0, out + log
    assert "sweep --sweep-only" in log
    assert "dscl-delete /Users/bh-builder" in log and "dscl-delete /Groups/bh-builder" in log
    assert f"rm -rf {home}" in log and f"rm -rf {prefix}" in log
    assert CACHES in log


def test_foreign_uid_470_keeps_its_home_and_caches(tmp_path):
    # another account owns uid 470 (setup refused it); there is no bh-builder user
    alice = {**OURS_USER, "NFSHomeDirectory": "/Users/alice", "UserShell": "/bin/zsh"}
    rc, log, out, home, prefix = run_teardown(tmp_path, users=[("alice", alice)], home_stat="Directory:470:470")
    assert rc == 1, out + log
    assert CACHES not in log, log
    assert f"rm -rf {home}" not in log and "dscl-delete" not in log, log
    assert "left in place" in out and "uid 470" in out
    assert f"rm -rf {prefix}" in log                         # our root-owned sandbox is still removed


@pytest.mark.parametrize("attr,value", [("UniqueID", "471"), ("PrimaryGroupID", "20"),
                                        ("NFSHomeDirectory", "/Users/bh"), ("UserShell", "/bin/zsh")])
def test_a_bh_builder_user_that_is_not_ours_is_refused(tmp_path, attr, value):
    rc, log, out, home, _ = run_teardown(tmp_path, users=[("bh-builder", {**OURS_USER, attr: value})],
                                         groups=[("bh-builder", "470")], home_stat="Directory:470:470")
    assert rc == 1, out + log
    assert "sweep" not in log and "dscl-delete /Users/bh-builder" not in log, log
    assert f"rm -rf {home}" not in log, log                  # its home belongs to that account, not to us
    assert CACHES not in log
    assert "left in place" in out and "bh-builder" in out


def test_a_group_with_another_gid_is_refused(tmp_path):
    rc, log, out, _, _ = run_teardown(tmp_path, users=[("bh-builder", OURS_USER)], groups=[("bh-builder", "999")],
                                      home_stat="Directory:470:470")
    assert rc == 1 and "dscl-delete /Groups/bh-builder" not in log, out + log
    assert "dscl-delete /Users/bh-builder" in log and "999" in out


@pytest.mark.parametrize("kind", ["Symbolic Link:470:470", "Directory:501:20", "Directory:470:20", "Regular File:470:470"])
def test_a_home_that_is_not_ours_is_refused(tmp_path, kind):
    rc, log, out, home, _ = run_teardown(tmp_path, users=[("bh-builder", OURS_USER)],
                                         groups=[("bh-builder", "470")], home_stat=kind)
    assert rc == 1, out + log
    assert f"rm -rf {home}" not in log and f"{home}" not in log.replace(f"rm -rf {home}", ""), log
    assert "left in place" in out


def test_rerun_after_partial_teardown_removes_an_orphaned_home_but_not_caches(tmp_path):
    # user already deleted, nobody else holds uid 470: the 470-owned home is ours to finish removing, but per-user
    # caches are only removed while the account itself is verified ours
    rc, log, out, home, _ = run_teardown(tmp_path, home_stat="Directory:470:470")
    assert rc == 0, out + log
    assert f"rm -rf {home}" in log and CACHES not in log


def test_sweep_failure_still_refuses_to_delete_the_user(tmp_path):
    rc, log, out, _, _ = run_teardown(tmp_path, users=[("bh-builder", OURS_USER)], groups=[("bh-builder", "470")],
                                      home_stat="Directory:470:470", sweep_rc=1)
    assert rc == 1 and "dscl-delete /Users/bh-builder" not in log and CACHES not in log


# ---- round 2 (re-review): a failed query is never "absent", values must be exact, a foreign uid fences all
ALICE = {**OURS_USER, "NFSHomeDirectory": "/Users/alice", "UserShell": "/bin/zsh"}
LIST_FAILS = 'dscl() { if [ "$2" = -list ]; then return 70; fi; dscl_base "$@"; }'
ALL_FAIL = 'dscl() { case "$2" in -delete) dscl_base "$@";; *) return 70;; esac; }'
READ_FAILS = 'dscl() { if [ "$2" = -read ]; then return 70; fi; dscl_base "$@"; }'


def nothing_destroyed(log, home):
    return (f"rm -rf {home}" not in log and "sweep" not in log and "dscl-delete" not in log
            and CACHES not in log)


@pytest.mark.parametrize("users,extra", [
    ([("alice", ALICE)], LIST_FAILS),                    # foreign uid 470, but the account list cannot be read
    ([("bh-builder", OURS_USER)], ALL_FAIL),             # every Directory Services query fails
    ([("bh-builder", OURS_USER)], LIST_FAILS),
])
def test_a_failed_account_listing_refuses_everything(tmp_path, users, extra):
    rc, log, out, home, prefix = run_teardown(tmp_path, users=users, groups=[("bh-builder", "470")],
                                              home_stat="Directory:470:470", extra=extra)
    assert rc == 1, out + log
    assert nothing_destroyed(log, home) and f"rm -rf {prefix}" not in log, log
    assert "could not inspect" in out


def test_a_failed_read_of_our_user_is_not_absence(tmp_path):
    rc, log, out, home, _ = run_teardown(tmp_path, users=[("bh-builder", OURS_USER)], groups=[("bh-builder", "470")],
                                         home_stat="Directory:470:470", extra=READ_FAILS)
    assert rc == 1, out + log
    assert nothing_destroyed(log, home), log
    assert "could not inspect" in out


@pytest.mark.parametrize("attr,value", [
    ("NFSHomeDirectory", "/Users/Shared/.bh-builder-home foreign"),     # expected path plus trailing text
    ("NFSHomeDirectory", "/Users/Shared/.bh-builder-home /Users/x"),    # two values
    ("UniqueID", "470 501"),                                            # multi-valued uid
    ("PrimaryGroupID", "470 20"),
    ("UserShell", "/usr/bin/false extra"),
    ("NFSHomeDirectory", "/Users/Shared/.bh-builder-home/"),            # trailing slash: not the exact value
])
def test_attributes_must_be_exact_single_values(tmp_path, attr, value):
    rc, log, out, home, _ = run_teardown(tmp_path, users=[("bh-builder", {**OURS_USER, attr: value})],
                                         groups=[("bh-builder", "470")], home_stat="Directory:470:470")
    assert rc == 1, out + log
    assert nothing_destroyed(log, home), log


def test_group_gid_must_be_exact(tmp_path):
    rc, log, out, _, _ = run_teardown(tmp_path, users=[("bh-builder", OURS_USER)], groups=[("bh-builder", "470 20")],
                                      home_stat="Directory:470:470")
    assert rc == 1 and "dscl-delete /Groups/bh-builder" not in log, out + log


def test_a_foreign_duplicate_uid_fences_the_sweep_too(tmp_path):
    # bh-builder matches exactly, but alice also has uid 470: the dispatcher sweep kills by numeric uid
    rc, log, out, home, _ = run_teardown(tmp_path, users=[("bh-builder", OURS_USER), ("alice", ALICE)],
                                         groups=[("bh-builder", "470")], home_stat="Directory:470:470")
    assert rc == 1, out + log
    assert nothing_destroyed(log, home), log
    assert "uid 470" in out and "alice" in out


def test_a_foreign_multi_valued_uid_is_still_foreign(tmp_path):
    rc, log, out, home, _ = run_teardown(tmp_path, users=[("bh-builder", OURS_USER),
                                                          ("alice", {**ALICE, "UniqueID": "501 470"})],
                                         groups=[("bh-builder", "470")], home_stat="Directory:470:470")
    assert rc == 1 and nothing_destroyed(log, home), out + log


def test_failed_cache_cleanup_fails_the_teardown(tmp_path):
    extra = 'find() { log "find $*"; case "$1" in /private/var/folders) return 1;; esac; }'
    rc, log, out, _, _ = run_teardown(tmp_path, users=[("bh-builder", OURS_USER)], groups=[("bh-builder", "470")],
                                      home_stat="Directory:470:470", extra=extra)
    assert CACHES in log
    assert rc == 1 and "removed bh-builder" not in out and "/private/var/folders" in out, out + log
    # r3: keep the verified identity and the dispatcher, so a retry can verify uid 470 again and redo the caches
    # (an absent account can never authorize a cache sweep, by design)
    assert "dscl-delete /Users/bh-builder" not in log and "dscl-delete /Groups/bh-builder" not in log, log
    assert not any(l.startswith("rm -rf ") and l.endswith("/prefix") for l in log.splitlines()), log
    assert log.index(CACHES) > log.index("sweep --sweep-only")


# ---- 2026-11-09, a real Mac (macOS 27.2): what rm -rf as root cannot remove from uid 470's per-user folder. Each
# directory carries the system no-unlink flag (sunlnk, SIP-enforced); 0/ also holds a LaunchServices data vault
# that the kernel will not let anyone stat, root included. The empty TemporaryItems dirs (no flag) were removed.
SKEL = "/private/var/folders/xq/k3v9w1m7p2d8hs4t6b0yzc5r0000fp"
PROT = "Directory:470:1048576"                           # %HT:%u:%f; 1048576 = SF_NOUNLINK (0x00100000)
REAL_SKELETON = {SKEL: PROT, f"{SKEL}/0": PROT, f"{SKEL}/0/com.apple.LaunchServices.dv": EPERM,
                 f"{SKEL}/T": PROT, f"{SKEL}/T/com.apple.trustd": PROT, f"{SKEL}/C": PROT, f"{SKEL}/X": PROT}
REMOVE = "find /private/var/folders -maxdepth 2 -user 470 -prune -exec rm -rf"


def ours(tmp_path, **kw):
    return run_teardown(tmp_path, users=[("bh-builder", OURS_USER)], groups=[("bh-builder", "470")],
                        home_stat="Directory:470:470", **kw)


def kept_everything_for_a_retry(log):
    return ("dscl-delete /Users/bh-builder" not in log and "dscl-delete /Groups/bh-builder" not in log
            and not any(l.startswith("rm -rf ") and l.endswith("/prefix") for l in log.splitlines()))


RM_FAILS = 'find() { log "find $*"; case "$*" in *-exec*) return 1 ;; *) cat "$T/roots" ;; esac; }'


@pytest.mark.parametrize("extra", ["", RM_FAILS])        # on the real Mac rm -rf exits 1 on the protected dirs
def test_the_macos_protected_skeleton_is_a_known_residual_not_a_failure(tmp_path, extra):
    rc, log, out, home, prefix = ours(tmp_path, folders=REAL_SKELETON, extra=extra)
    assert rc == 0, out + log
    line = next(l for l in out.splitlines() if "left by macOS" in l)
    assert "protected per-user folders" in line and SKEL in line and "nothing to do" in line, out
    assert "dscl-delete /Users/bh-builder" in log and "dscl-delete /Groups/bh-builder" in log
    assert f"rm -rf {home}" in log and f"rm -rf {prefix}" in log and "removed bh-builder" in out
    assert log.index(REMOVE) < log.index(f"ls -1Ab {SKEL}")             # classified after removing what it can
    assert "chflags" not in log.split("dscl-delete")[0]                 # never touches system flags


@pytest.mark.parametrize("left,why", [
    ({f"{SKEL}/T/build.log": "Regular File:470:0"}, "a file rm could not remove"),
    ({f"{SKEL}/T/TemporaryItems": "Directory:470:0"}, "a directory without the flag"),
    ({f"{SKEL}/T/TemporaryItems": "Directory:470:0", f"{SKEL}/T/TemporaryItems/x": "Regular File:470:0"}, "nested"),
    ({f"{SKEL}/X/deep": PROT, f"{SKEL}/X/deep/f": "Regular File:470:0"}, "a file deep in protected folders"),
    ({f"{SKEL}/T/held": "Directory:470:2"}, "only a user flag (uchg), not sunlnk"),
    ({f"{SKEL}/T/foreign": "Directory:501:1048576"}, "protected, but not uid 470's"),
    ({f"{SKEL}/T/gone": MISSING}, "an entry that cannot be inspected (not EPERM)"),
    ({f"{SKEL}/T/odd": "Directory:470:"}, "unreadable flags"),
    ({f"{SKEL}/T/odd": "Directory:470:08"}, "flags that are not a plain decimal"),
    ({SKEL: "Directory:470:0"}, "the per-user folder itself has no flag"),
    ({SKEL: EPERM}, "a data vault is tolerated only inside a protected folder"),
])
def test_anything_beyond_the_protected_skeleton_still_fails_and_keeps_the_account(tmp_path, left, why):
    rc, log, out, _, _ = ours(tmp_path, folders={**REAL_SKELETON, **left})
    assert rc == 1, (why, out + log)
    assert "left by macOS" not in out and "/private/var/folders" in out, (why, out)
    assert kept_everything_for_a_retry(log), (why, log)


def test_a_protected_folder_that_cannot_be_listed_is_not_empty(tmp_path):
    rc, log, out, _, _ = ours(tmp_path, folders=REAL_SKELETON, unlistable={f"{SKEL}/T"})
    assert rc == 1 and kept_everything_for_a_retry(log) and "left by macOS" not in out, out + log


def test_the_residual_check_itself_failing_is_a_failure(tmp_path):
    extra = 'find() { log "find $*"; case "$*" in *-exec*) ;; *) return 1 ;; esac; }'
    rc, log, out, _, _ = ours(tmp_path, folders=REAL_SKELETON, extra=extra)
    assert rc == 1 and kept_everything_for_a_retry(log), out + log


def test_the_flag_check_never_follows_a_symlink(tmp_path):
    link = f"{SKEL}/T/link"
    rc, log, out, _, _ = ours(tmp_path, folders={**REAL_SKELETON, link: "Symbolic Link:470:1048576"})
    assert rc == 1 and kept_everything_for_a_retry(log), out + log
    statlog = (tmp_path / "t" / "statlog").read_text().splitlines()
    assert any(l.endswith(link) for l in statlog), statlog
    assert not any(" -L" in f" {l}" for l in statlog), statlog          # lstat only
    assert f"ls -1Ab {link}" not in log                                  # never listed through


def test_a_foreign_uid_470_never_inspects_or_removes_its_folders(tmp_path):
    rc, log, out, home, _ = run_teardown(tmp_path, users=[("alice", ALICE)], home_stat="Directory:470:470",
                                         folders=REAL_SKELETON)
    assert rc == 1 and CACHES not in log and "ls " not in log and "left by macOS" not in out, out + log


# ---- 2026-11-09 (docs/research/tempitems-uid-reuse-investigation.md): TemporaryItems (T/TemporaryItems,
# T/com.apple.trustd/TemporaryItems) has no sunlnk flag but is TCC Full Disk Access protected: nobody, root
# included, can list it, so rm -rf cannot empty it. stat works, and on APFS a directory's st_size is
# 32 * (entries + 2) and st_nlink is entries + 2 (observed, not documented), so 64/2 proves it empty. Only that exact
# shape is accepted; every single deviation fails closed with the recovery path.
TI, TTI = f"{SKEL}/T/TemporaryItems", f"{SKEL}/T/com.apple.trustd/TemporaryItems"
EMPTY_TI = "Directory:470:470:40700:0:64:2"              # %HT:%u:%g:%p:%f:%z:%l
RECOVERY = ("Restart this Mac (macOS clears these temporary folders at startup), then run 'bh uninstall' again "
            "before deleting anything by hand. If it still cannot be verified, temporarily give your Terminal Full "
            "Disk Access (System Settings > Privacy & Security > Full Disk Access), rerun 'bh uninstall', then "
            "remove that access.")


def test_verified_empty_temporary_items_are_a_known_residual(tmp_path):
    rc, log, out, home, prefix = ours(tmp_path, folders=REAL_SKELETON, full={TI: EMPTY_TI, TTI: EMPTY_TI})
    assert rc == 0, out + log
    assert "dscl-delete /Users/bh-builder" in log and f"rm -rf {prefix}" in log and "removed bh-builder" in out
    for p in (TI, TTI):
        assert f"rmdir {p}" in log, log                                  # tried, refused with EPERM: kept
        line = next(l for l in out.splitlines() if l.endswith(f"'{p}'"))
        assert "left by macOS" in line and "verified empty" in line, out
        assert f"ls -1Ab {p}" in log and f"df -P -Y -T apfs {p}" in log, log
    assert log.index(f"df -P -Y -T apfs {TI}") < log.index(f"rmdir {TI}")
    assert "Full Disk Access" not in out, out


def test_a_verified_empty_temporary_items_that_rmdir_removes_is_gone(tmp_path):
    rc, log, out, _, _ = ours(tmp_path, folders=REAL_SKELETON, full={TI: EMPTY_TI}, rmdir="ok")
    assert rc == 0, out + log
    assert f"rmdir {TI}" in log and not any(TI in l for l in out.splitlines()), out


def _ti(**kw):
    vals = dict(zip(("ty", "uid", "gid", "mode", "flags", "size", "nlink"), EMPTY_TI.split(":")))
    vals.update(kw)
    return ":".join(vals.values())


@pytest.mark.parametrize("full,kw,why", [
    ({TI: _ti(size="96")}, {}, "size 96: one entry"),
    ({TI: _ti(nlink="3")}, {}, "nlink 3: one subdirectory"),
    ({TI: _ti(size="96", nlink="3")}, {}, "one entry by both measures"),
    ({TTI: _ti(size="128")}, {}, "the trustd TemporaryItems is not empty"),
    ({f"{SKEL}/T/TemporaryItems2": EMPTY_TI}, {}, "another name"),
    ({f"{SKEL}/T/temporaryitems": EMPTY_TI}, {}, "another case"),
    ({f"{SKEL}/C/TemporaryItems": EMPTY_TI}, {}, "another parent (C)"),
    ({f"{SKEL}/X/TemporaryItems": EMPTY_TI}, {}, "another parent (X)"),
    ({f"{SKEL}/T/com.apple.trustd/x/TemporaryItems": EMPTY_TI}, {"folders": {f"{SKEL}/T/com.apple.trustd/x": PROT}},
     "deeper"),
    ({f"{SKEL}/TemporaryItems": EMPTY_TI}, {}, "top of the per-user folder"),
    ({"/private/var/folders/xq/TemporaryItems": EMPTY_TI}, {}, "top-level per-user folder itself"),
    ({TI: _ti(mode="40755")}, {}, "mode 0755"),
    ({TI: _ti(mode="41700")}, {}, "sticky bit"),
    ({TI: _ti(mode="42700")}, {}, "setgid bit"),
    ({TI: _ti(uid="501")}, {}, "owner 501"),
    ({TI: _ti(gid="20")}, {}, "group 20"),
    ({TI: _ti(flags="2")}, {}, "uchg flag"),
    ({TI: _ti(flags="32768")}, {}, "hidden flag"),
    ({TI: _ti(ty="Symbolic Link", mode="120700")}, {}, "a symlink"),
    ({TI: _ti(ty="Regular File", mode="100700")}, {}, "a file"),
    ({TI: _ti(ty="Symbolic Link")}, {}, "the type field alone says symlink (it is also encoded in %p)"),
    ({TI: EMPTY_TI}, {"lserr": {TI: "Permission denied"}}, "ls EACCES"),
    ({TI: EMPTY_TI}, {"lserr": {TI: "No such file or directory"}}, "ls ENOENT"),
    ({TI: EMPTY_TI}, {"lserr": {TI: None}}, "listable (not TCC-protected): rm -rf should have removed it"),
    ({TI: EMPTY_TI}, {"fstype": "hfs"}, "not APFS"),
    ({TI: EMPTY_TI}, {"fstype": "lie"}, "df ignored the type filter and shows another file system"),
    ({TI: EMPTY_TI}, {"fstype": "apfs-err"}, "df printed an APFS row but failed"),
    ({TI: EMPTY_TI}, {"rmdir": "Directory not empty"}, "rmdir ENOTEMPTY"),
    ({TI: EMPTY_TI}, {"rmdir": "Resource busy"}, "rmdir fails some other way"),
])
def test_temporary_items_not_verifiably_empty_fail_closed_with_the_recovery_path(tmp_path, full, kw, why):
    kw = dict(kw)
    rc, log, out, _, _ = ours(tmp_path, folders={**REAL_SKELETON, **kw.pop("folders", {})}, full=full, **kw)
    assert rc == 1, (why, out + log)
    assert kept_everything_for_a_retry(log), (why, log)
    assert "left by macOS" not in out and RECOVERY in out, (why, out)
    assert "rm -rf /private/var/folders" not in out, (why, out)        # never an rm suggestion for it
    if "rmdir" not in why:
        assert "rmdir" not in log, (why, log)                          # rmdir only after the emptiness proof


@pytest.mark.parametrize("kids,reason", [
    ([], "it can be listed and is empty, yet rm -rf left it"),
    (["f"], "it can be listed and is not empty"),
])
def test_a_listable_temporary_items_still_fails_with_an_accurate_reason(tmp_path, kids, reason):
    # Security review round 1 P3: with Full Disk Access, ls works; rm -rf should then have removed it, so stay fail-closed, but
    # say what was seen
    extra = {f"{TI}/{k}": "Regular File:470:0" for k in kids}
    rc, log, out, _, _ = ours(tmp_path, folders={**REAL_SKELETON, **extra}, full={TI: EMPTY_TI}, lserr={TI: None})
    assert rc == 1 and reason in out and "not the protected folder" not in out, out


def test_the_recovery_path_never_suggests_full_disk_access_for_a_build_binary(tmp_path):
    out = run_teardown(tmp_path, main='echo "$RECOVERY"')[2]
    assert out == RECOVERY + "\n", out
    text = SCRIPT.read_text() + SETUP.read_text()
    for bad in ("/bin/sh", "bh-runner", "bh-dispatch"):
        assert not any("Full Disk Access" in l and bad in l for l in text.splitlines()), bad


def test_the_emptiness_proof_is_flagged_as_observed_not_documented():
    text = SCRIPT.read_text()
    assert "32 * (entries + 2)" in text and "not a documented" in text


def test_the_read_only_check_never_removes_anything(tmp_path):
    # setup's preflight runs the same classification with may-remove 0: no rm, no rmdir
    rc, log, out, _, _ = run_teardown(tmp_path, folders=REAL_SKELETON, full={TI: EMPTY_TI},
                                      main="caches_cleared 0; echo rc=$?")
    assert "rc=0" in out and "rmdir" not in log and "rm " not in log, out + log


# ---- setup's preflight (numeric uid reuse): a new bh-builder gets uid 470 again, and a new GeneratedUID, so a new
# per-user folder; the old one stays owned by 470 and reachable by path. Refuse to install over anything in it that
# is not verifiably the empty protected skeleton. Run setup itself with every command it reaches stubbed on PATH;
# the first `dscl -create` ends the run with 99 (= the preflight let the install go ahead).
SETUP = SANDBOX / "setup-bh-builder.sh"
SETUP_STUBS = r'''
dscl() { case "$2" in -create) log "dscl-create $3 ${4:-}"; exit 99 ;; *) dscl_base "$@" ;; esac; }
mkdir() { log "mkdir $*"; exit 98; }
chown() { log "chown $*"; exit 98; }
install() { log "install $*"; exit 98; }
dseditgroup() { log "dseditgroup $*"; exit 98; }
'''
# Security review round 1 P3: a restart clearing a deleted account's folder is unverified (research doc section 4): say so
SETUP_RECOVERY = ("Restart this Mac, then run 'bh setup' again before deleting anything by hand (macOS clears "
                  "temporary folders at startup for existing accounts; whether it also clears the folder of a deleted "
                  "account is not verified).")


def run_setup(tmp_path: Path, users=(), groups=(), **tree):
    t = tmp_path / "t"
    make_tree(t, users=users, groups=groups, **tree)
    lib, fake = t / "stubs.sh", t / "bin"
    lib.write_text(f"T='{t}'\n{STUBS}\n{SETUP_STUBS}\n")
    fake.mkdir()
    for name in ("id", "dscl", "find", "stat", "ls", "df", "rmdir", "rm", "chflags", "chmod", "chown", "mkdir",
                 "install", "dseditgroup", "launchctl", "sleep"):
        (fake / name).write_text(f"#!/bin/bash\nsource '{lib}'\n{name} \"$@\"\n")
        os.chmod(fake / name, 0o755)
    p = subprocess.run(["/bin/bash", str(SETUP)], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{fake}:/usr/bin:/bin"})
    return p.returncode, (t / "log").read_text(), p.stdout + p.stderr


def test_setup_installs_when_no_earlier_folder_exists(tmp_path):
    rc, log, out = run_setup(tmp_path)
    assert rc == 99 and "dscl-create /Groups/bh-builder" in log, out + log
    assert CACHES in log and "rmdir" not in log, log


def test_setup_installs_over_an_empty_earlier_skeleton(tmp_path):
    rc, log, out = run_setup(tmp_path, folders=REAL_SKELETON, full={TI: EMPTY_TI, TTI: EMPTY_TI})
    assert rc == 99 and "dscl-create /Groups/bh-builder" in log, out + log
    assert "rmdir" not in log and not any(l.startswith("rm ") for l in log.splitlines()), log   # read-only
    assert f"ls -1Ab {SKEL}" in log and SKEL in out, out + log


@pytest.mark.parametrize("left,kw,why", [
    ({}, {"full": {TI: _ti(size="96")}}, "TemporaryItems holds one entry"),
    ({}, {"full": {TI: EMPTY_TI}, "fstype": "hfs"}, "cannot prove it empty off APFS"),
    ({}, {"full": {TI: EMPTY_TI}, "lserr": {TI: "Permission denied"}}, "not the TCC refusal"),
    ({f"{SKEL}/C/build.db": "Regular File:470:0"}, {}, "a cache file from the earlier install"),
    ({SKEL: "Directory:470:0"}, {}, "a folder without the protection flag"),
])
def test_setup_refuses_an_earlier_folder_that_is_not_verifiably_empty(tmp_path, left, kw, why):
    rc, log, out = run_setup(tmp_path, folders={**REAL_SKELETON, **left}, **kw)
    assert rc == 1, (why, out + log)
    assert "dscl-create" not in log and "rmdir" not in log, (why, log)
    assert SETUP_RECOVERY in out and "uid 470" in out, (why, out)


def test_setup_refuses_when_the_folders_cannot_be_checked(tmp_path):
    rc, log, out = run_setup(tmp_path, folders=REAL_SKELETON, unlistable={f"{SKEL}/T"})
    assert rc == 1 and "dscl-create" not in log, out + log


def test_setup_does_not_inspect_the_folders_of_an_existing_account(tmp_path):
    # rerunning setup over our own live account: its folder is in use, not left over
    live = {**REAL_SKELETON, f"{SKEL}/C/live.db": "Regular File:470:0"}
    rc, log, out = run_setup(tmp_path, users=[("bh-builder", OURS_USER)], groups=[("bh-builder", "470")],
                             folders=live)
    assert rc == 99 and CACHES not in log, out + log
    rc, log, out = run_setup(tmp_path / "w", users=[("bh-builder", {**OURS_USER, "UniqueID": "471"})],
                             groups=[("bh-builder", "470")], folders=live)
    assert rc == 1 and "wrong uid" in out and CACHES not in log, out + log      # setup's own refusal, not ours
    for i, alice in enumerate((ALICE, {**ALICE, "UniqueID": "501 470"})):
        rc, log, out = run_setup(tmp_path / f"a{i}", users=[("alice", alice)], folders=live)
        assert CACHES not in log, out + log                              # another account's files: not ours to judge


# ---- Security review round 1 P1: names under T/, C/, X/ are chosen by build code. `ls -b` escapes only non-printables and
# backslash, so space, ~, $( and * reach the messages. Every build-chosen path is printed shell-escaped (printf %q)
# and only in diagnostics; the one command setup tells the owner to run names only the macOS-generated folder.
import shlex  # noqa: E402

HOSTILE = ["x ~", "y $(touch pwned)", "z *", "q'uote", "a\\nb", "w`id`", "~", "s;rm -rf ~",
           "a#b", "#x", "x'y"]                                                      # as ls -b lists them


def _single_quoted(p):
    return "'" + p.replace("'", "'\\''") + "'"


@pytest.mark.parametrize("name", HOSTILE)
def test_shown_single_quotes_so_no_shell_option_can_glob_it(tmp_path, name):
    # Security review round 2 P3: bash's %q leaves a mid-word # bare, a glob operator in zsh with extendedglob
    p = f"{SKEL}/T/{name}"
    out = run_teardown(tmp_path, main=f"shown {shlex.quote(p)}; echo")[2]
    assert out == _single_quoted(p) + "\n", out


def test_shown_escapes_raw_control_characters(tmp_path):
    out = run_teardown(tmp_path, main="shown $'/x/a\\nb\\033c'; echo")[2]
    assert out == "$'/x/a\\nb\\Ec'\n" and "\x1b" not in out, repr(out)


def _left_behind_paths(out):
    return [shlex.split(l.rsplit("): ", 1)[1]) for l in out.splitlines() if l.startswith("left behind")]


def _commands(out):
    # diagnostics ("left behind ...: '<path>'") are not commands; a quoted name may itself read "rm -rf"
    return [l for l in out.splitlines() if ("sudo" in l or "rm -rf" in l) and not l.startswith("left behind")]


@pytest.mark.parametrize("name", HOSTILE)
def test_teardown_prints_build_chosen_names_escaped(tmp_path, name):
    p = f"{SKEL}/T/{name}"
    rc, log, out, _, _ = ours(tmp_path, folders={**REAL_SKELETON, p: "Regular File:470:0"})
    assert rc == 1 and _left_behind_paths(out) == [[p]], out             # one word, exactly the name
    assert not any(name in l for l in _commands(out)), out


@pytest.mark.parametrize("name", HOSTILE)
def test_setup_names_only_the_macos_folder_in_its_command(tmp_path, name):
    p = f"{SKEL}/C/{name}"
    rc, log, out = run_setup(tmp_path, folders={**REAL_SKELETON, p: "Regular File:470:0"})
    assert rc == 1 and _left_behind_paths(out) == [[p]], out
    cmds = _commands(out)
    assert cmds and not any(name in l for l in cmds), out
    assert f"sudo rm -rf '{SKEL}'" in out, out
    assert shlex.split(out.split("run: ", 1)[1].split(" (", 1)[0]) == ["sudo", "rm", "-rf", SKEL], out


@pytest.mark.parametrize("root", ["/private/var/folders/xq/k3v9w1m7p2d8hs4t6b0yzc5r0000fpX",
                                  "/private/var/folders/xa/k3v9w1m7p2d8hs4t6b0yzc5r0000fp",
                                  "/private/var/folders/xq/k3v9w1m7p2d8hs4t6b0yzc5r0000f'"])
def test_setup_shows_no_command_for_a_folder_name_macos_would_not_generate(tmp_path, root):
    rc, log, out = run_setup(tmp_path, folders={root: "Directory:470:0"})
    assert rc == 1 and "sudo rm" not in out and "rm -rf" not in out, out


def test_setup_never_sets_a_generated_uid():
    assert "GeneratedUID" not in "\n".join(l for l in SETUP.read_text().splitlines()
                                            if not l.lstrip().startswith("#"))


# ---- round 3, a real Mac (macOS 27.2, 2026-11-09): the TemporaryItems proof passed as root (stat read exactly
# Directory:470:470:40700:0:64:2, ls failed with exactly "ls: <path>: Operation not permitted"), but
# `dscl . -delete /Users/bh-builder` hung twice: opendirectoryd logged "ODRecordDelete ... active for 310 seconds,
# forcing a timeout", then ODErrorPluginOperationTimeout and DS Error -14987 (eUndefinedError). Observed, cause not
# proven: a uid-470 `distnoted agent` started the second the delete began and launchd respawned it after a kill;
# with uid 470's processes stopped a bounded delete finished in 37 s. So, after the identity fence only: boot out
# uid 470's launchd domains, kill its processes, confirm none remain, then delete with a time limit, retry once.
FAST = "DELETE_TIMEOUT=50"               # 50 stub seconds = 1 s
USER_DELETE = "dscl-delete /Users/bh-builder"


def lines(log, prefix):
    return [i for i, l in enumerate(log.splitlines()) if l.startswith(prefix)]


def test_uid_470_processes_are_stopped_before_the_user_delete(tmp_path):
    rc, log, out, _, _ = ours(tmp_path, extra=FAST)
    assert rc == 0, out + log
    d = lines(log, USER_DELETE)[0]
    boot, kill, check = (lines(log, p)[0] for p in ("launchctl bootout user/470", "pkill -KILL -U 470",
                                                    "pgrep -U 470"))
    assert lines(log, "sweep --sweep-only")[0] < boot < kill < check < d, log
    assert "launchctl bootout gui/470" not in log                         # no GUI domain: nothing to boot out


def test_a_gui_domain_for_uid_470_is_booted_out_too(tmp_path):
    rc, log, out, _, _ = ours(tmp_path, extra=FAST, gui=True)
    assert rc == 0 and lines(log, "launchctl bootout gui/470")[0] < lines(log, USER_DELETE)[0], out + log


def test_lingering_processes_are_killed_again_until_none_remain(tmp_path):
    rc, log, out, _, _ = ours(tmp_path, extra=FAST, pgreps=["found", "found", "none"])
    assert rc == 0, out + log
    d = lines(log, USER_DELETE)[0]
    assert len([i for i in lines(log, "pkill -KILL -U 470") if i < d]) == 3, log


@pytest.mark.parametrize("pgreps,why", [(["found"] * 30, "processes keep running"),
                                        (["error"], "pgrep itself failed")])
def test_processes_that_cannot_be_stopped_keep_the_account(tmp_path, pgreps, why):
    rc, log, out, home, prefix = ours(tmp_path, extra=FAST, pgreps=pgreps)
    assert rc == 1 and kept_everything_for_a_retry(log), (why, out + log)
    assert f"rm -rf {home}" not in log and "uid 470" in out and "bh uninstall" in out, (why, out)


@pytest.mark.parametrize("first", ["hang", "err"])
def test_a_delete_that_times_out_or_fails_is_retried_once_after_stopping_processes_again(tmp_path, first):
    import time
    t0 = time.monotonic()
    rc, log, out, home, prefix = ours(tmp_path, extra=FAST, deletes=[first, "ok"])
    assert time.monotonic() - t0 < 20, "the hung delete was not bounded"
    assert rc == 0 and "removed bh-builder" in out, out + log
    d1, d2 = lines(log, USER_DELETE)
    assert [i for i in lines(log, "pkill -KILL -U 470") if d1 < i < d2], log      # stopped again in between
    assert [i for i in lines(log, "launchctl bootout user/470") if d1 < i < d2], log
    assert "retrying once" in out and ("did not finish within 50 s" in out if first == "hang" else "exit 1" in out), out
    assert f"rm -rf {home}" in log and f"rm -rf {prefix}" in log


@pytest.mark.parametrize("deletes", [["hang", "hang"], ["err", "err"], ["hang", "err"]])
def test_a_second_failed_delete_fails_closed_naming_opendirectoryd(tmp_path, deletes):
    rc, log, out, home, prefix = ours(tmp_path, extra=FAST, deletes=deletes)
    assert rc == 1 and len(lines(log, USER_DELETE)) == 2, out + log
    assert "opendirectoryd" in out and "-14987" in out and "distnoted" in out, out
    assert "kept user bh-builder and" in out and "bh uninstall" in out, out
    assert "removed bh-builder" not in out and "could not delete user bh-builder, twice (" in out, out
    assert "could not delete user bh-builder\n" not in out, out                  # never the old generic line
    assert not any(l.startswith("rm -rf ") and l.endswith("/prefix") for l in log.splitlines()), log   # rerun sweeps
    assert f"rm -rf {home}" not in log and "dscl-delete /Groups/bh-builder" not in log, log


@pytest.mark.parametrize("users", [
    [("alice", ALICE)],                                                    # another account holds uid 470
    [("bh-builder", {**OURS_USER, "UserShell": "/bin/zsh"})],              # a bh-builder that is not ours
    [("bh-builder", OURS_USER), ("alice", ALICE)],                         # ours, but uid 470 is shared
])
def test_nothing_is_stopped_or_killed_when_the_identity_fence_fails(tmp_path, users):
    rc, log, out, _, _ = run_teardown(tmp_path, users=users, groups=[("bh-builder", "470")],
                                      home_stat="Directory:470:470", extra=FAST)
    assert rc == 1, out + log
    for p in ("launchctl bootout user/470", "launchctl bootout gui/470", "pkill", "pgrep", USER_DELETE):
        assert p not in log, (p, log)


# ---- Security review round 3 P3: bash reaps a background child before `wait`, so its pid can be free (and, in theory, reused)
# while `bounded` still holds it. Signal only a pid whose parent is still this shell, and with KILL at once: no
# TERM-then-sleep window.
def test_a_timed_out_delete_is_killed_at_once_after_a_parent_check(tmp_path):
    rc, log, out, _, _ = ours(tmp_path, extra=FAST, deletes=["hang", "ok"])
    assert rc == 0, out + log
    check, kill = lines(log, "ps -o ppid= -p")[0], lines(log, "kill -KILL")[0]
    assert check < kill and not lines(log, "kill -TERM"), log


def test_a_pid_that_is_no_longer_our_child_is_never_signalled(tmp_path):
    rc, log, out, _, _ = ours(tmp_path, extra=FAST, deletes=["hangshort", "ok"], ppid="1")
    assert rc == 0 and lines(log, "ps -o ppid= -p"), out + log
    assert not lines(log, "kill -KILL") and not lines(log, "kill -TERM"), log


# ---- Security review round 3 P3: opendirectoryd keeps a timed-out ODRecordDelete running after dscl is gone and may finish it.
# After a failed attempt, a listing that works and lacks bh-builder means it is deleted (the script's own meaning of
# "absent"); a listing that fails never does.
@pytest.mark.parametrize("first", ["hangdel", "errdel"])
def test_a_delete_that_completed_server_side_counts_as_done(tmp_path, first):
    rc, log, out, home, prefix = ours(tmp_path, extra=FAST, deletes=[first])
    assert rc == 0 and "removed bh-builder" in out, out + log
    assert len(lines(log, USER_DELETE)) == 1 and "no longer listed" in out, out + log
    assert f"rm -rf {home}" in log and f"rm -rf {prefix}" in log and "dscl-delete /Groups/bh-builder" in log


def test_a_failed_listing_after_a_failed_delete_is_not_gone(tmp_path):
    rc, log, out, _, prefix = ours(tmp_path, extra=FAST, deletes=["errdel_listfail", "err"])
    assert rc == 1 and len(lines(log, USER_DELETE)) == 2, out + log
    assert "no longer listed" not in out and f"rm -rf {prefix}" not in log, out + log


def test_killing_a_hung_delete_prints_no_job_notice(tmp_path):
    # Security review round 3 P3: non-interactive bash reports a background job killed by a signal ("Terminated: 15", "Killed: 9")
    rc, log, out, _, _ = ours(tmp_path, extra=FAST, deletes=["hang", "ok"])
    assert rc == 0 and lines(log, "kill -KILL"), out + log
    assert "Killed" not in out and "Terminated" not in out, out


def test_bounded_never_prints_a_job_notice_under_repetition(tmp_path):
    out = run_teardown(tmp_path, main="for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do "
                                      "bounded 1 /bin/sleep 5; echo rc=$?; done")[2]
    assert out.count("rc=124") == 20, out
    assert "Killed" not in out and "Terminated" not in out, out
