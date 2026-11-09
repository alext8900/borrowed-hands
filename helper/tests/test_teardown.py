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
sleep() { :; }
launchctl() { return 113; }
rm() { log "rm $*"; }
find() { log "find $*"; case "$*" in *-exec*) ;; *) cat "$T/roots" ;; esac; }
chflags() { log "chflags $*"; }
chmod() { log "chmod $*"; }
stat() {
  local p; for p; do :; done; printf '%s\n' "$*" >> "$T/statlog"
  local k=${p//\//|}           # the fake tree is flat: one file per path
  if [ -f "$T/eperm/$k" ]; then echo "stat: $p: stat: Operation not permitted" >&2; return 1; fi
  if [ -f "$T/fstat/$k" ]; then cat "$T/fstat/$k"; return; fi
  [ -f "$T/stat$p" ] && cat "$T/stat$p" || return 1
}
ls() { local d; for d; do :; done; log "ls $*"; d=${d//\//|}; [ -f "$T/ls/$d" ] && cat "$T/ls/$d" || return 1; }
dscl_base() {
  # dscl . -read <path> [attr] | -list <dir> <attr> | -delete <path>
  case "$2" in
    -read) [ -f "$T/ds$3" ] || return 56
           if [ $# -ge 4 ]; then grep "^$4: " "$T/ds$3" || return 56; else cat "$T/ds$3"; fi ;;
    -list) for f in "$T/ds$3"/*; do [ -f "$f" ] || continue
             printf '%s %s\n' "$(basename "$f")" "$(sed -n "s/^$4: //p" "$f")"; done ;;
    -delete) log "dscl-delete $3" ;;
  esac
}
dscl() { dscl_base "$@"; }
'''


def run_teardown(tmp_path: Path, users=(), groups=(), home_stat=None, sweep_rc=0, extra="", folders=None,
                 unlistable=()):
    """folders: what is left of uid 470 under /private/var/folders after the removal, {path: lstat answer}: the
    '%HT:%u:%f' text, EPERM (the kernel refuses even root a stat: a SIP data vault) or MISSING (listed, but the
    stat fails some other way). Depth-two paths are what the second `find` reports; each directory not in
    `unlistable` gets an `ls` listing of its children."""
    t = tmp_path / "t"
    for d in ("ds/Users", "ds/Groups", "prefix/bin"):
        (t / d).mkdir(parents=True, exist_ok=True)
    folders = folders or {}
    for d in ("eperm", "fstat", "ls"):
        (t / d).mkdir(parents=True, exist_ok=True)
    for p, kind in folders.items():
        key = p.replace("/", "|")
        if kind != MISSING:
            (t / ("eperm" if kind == EPERM else "fstat") / key).write_text("" if kind == EPERM else kind + "\n")
        if kind.startswith("Directory") and p not in unlistable:
            kids = sorted(c.rsplit("/", 1)[1] for c in folders if c.rsplit("/", 1)[0] == p)
            (t / "ls" / key).write_text("".join(k + "\n" for k in kids))
    (t / "roots").write_text("".join(p + "\n" for p in folders if p.count("/") == 5))   # /private/var/folders/x/y
    (t / "statlog").write_text("")
    for name, attrs in users:
        (t / "ds/Users" / name).write_text("".join(f"{k}: {v}\n" for k, v in attrs.items())
                                           .replace("/Users/Shared/.bh-builder-home", str(t / "home")))
    for name, gid in groups:
        (t / "ds/Groups" / name).write_text(f"PrimaryGroupID: {gid}\n")
    home = t / "home"
    if home_stat is not None:
        (t / f"stat{home}").parent.mkdir(parents=True, exist_ok=True)
        (t / f"stat{home}").write_text(home_stat + "\n")
    disp = t / "prefix/bin/bh-dispatch"
    disp.write_text(f"#!/bin/bash\necho \"sweep $*\" >> '{t}/log'\nexit {sweep_rc}\n")
    os.chmod(disp, 0o755)
    (t / "log").write_text("")
    body = (f"T='{t}'\nset -uo pipefail\nsource '{SCRIPT}'\n{STUBS}\n{extra}\n"
            f"PREFIX='{t}/prefix'; HOME_DIR='{home}'\nmain\n")
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
