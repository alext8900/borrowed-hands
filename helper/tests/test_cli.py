"""`bh` CLI, `bh setup` and `bh selftest` (HE-7 unit part, SPEC 8.4 and 11.2 texts). Every outside effect goes
through a fake Env: no sudo, no launchctl, no real /opt."""
from __future__ import annotations

import asyncio
import json
import os
import plistlib
from pathlib import Path

import pytest

import helpers as H
from fake_server import FakeServer
from borrowed_hands_helper import cli, config, keyfile, service
from borrowed_hands_helper._shared import SANDBOX, bhproto
from borrowed_hands_helper import caps
from borrowed_hands_helper.caps import Host, InstallStatus, XcodeStatus
from borrowed_hands_helper.caps import test_mode_message as tm_message
from borrowed_hands_helper.env import Env
from test_daemon import FAST, FakeBuild, fake_run

SUDO_TEXT = ("Borrowed Hands needs your admin password twice: once to create the hidden build user 'bh-builder' "
             "and install the build sandbox in /opt/borrowed-hands, once to add one sudo rule that lets this "
             "account start a build. It never gets root for anything else.")


CLT = "/Library/Developer/CommandLineTools"
DEV = "/Applications/Xcode.app/Contents/Developer"


def _diag(table):
    return caps.xcode_status(lambda argv: table.get(argv[0], (1, "")))


NOT_INSTALLED = _diag({"/usr/bin/xcode-select": (0, CLT + "\n"), "/usr/bin/env": (127, "")})
NOT_SELECTED = _diag({"/usr/bin/xcode-select": (0, CLT + "\n"),
                      "/usr/bin/env": (0, "Xcode 26.1\nBuild version 17B55\n")})


class FakeService:
    def __init__(self):
        self.calls = []
        self.stop_result = (True, "removed")

    def install(self, user_home=None, uid=None, run=None, argv=None):
        self.calls.append("install")
        return True, "installed"

    def uninstall(self, user_home=None, uid=None, run=None, say=None):
        self.calls.append("uninstall")
        return self.stop_result

    def installed(self, user_home=None):
        return "install" in self.calls


class World:
    """A fake Mac: what is installed, what sudo would do, what the user answers."""

    def __init__(self, tmp_path, answers=(), installed=False, sudo_rule=False, xcode=("26.1", "17B55"),
                 test_mode=False, xcode_diag=None, broad_sudo=False):
        self.out, self.errs, self.sudo_calls = [], [], []
        self.answers = list(answers)
        self.installed, self.rule, self.test_mode = installed, sudo_rule, test_mode
        self.broad = broad_sudo                 # e.g. NOPASSWD: ALL elsewhere: sudo -n works without our rule
        self.stale = False                      # our file is there (root:wheel 0440) but its bytes differ
        self.override = None                    # a command sudo refuses although our exact file is in place
        self.tmp = str(tmp_path)
        self.left: set = set()                  # system paths that still exist (never the real /opt on this Mac)
        self.loaded: set = set()                # launchctl targets / dscl records that still exist
        self.inspect_rc = None                  # set: every launchctl/dscl inspection fails with this code
        self.unknown_paths: set = set()         # paths whose existence cannot be checked (e.g. EACCES)
        self.running = False                    # a helper process still holds the lock (LaunchAgent or bh run)
        self.caches = []                        # uid 470's dirs under /private/var/folders; None: cannot scan
        self.cache_left = None                  # what in them is not macOS's protected skeleton; None: all of it
        self.other_users: dict = {}             # other accounts: name -> UniqueID value(s)
        self.xc = xcode
        self.xdiag = xcode_diag or NOT_INSTALLED
        self.svc = FakeService()
        self.env = Env(home=tmp_path / "BorrowedHands", say=self.out.append, err=self.errs.append,
                       ask=self._ask, sudo=self._sudo, run=self._run, install_status=self._install,
                       user_home=tmp_path / "user", exists=self._exists,
                       sudo_rule_ok=lambda *_: self.rule or self.broad,
                       sudo_rule_file=lambda: (True, "") if (self.rule or self.stale) else
                       (False, "/etc/sudoers.d/borrowed-hands is missing"),
                       sudo_rule_permitted=lambda *_: self.override or (None if (self.rule or self.broad) else
                                                                       "/opt/borrowed-hands/bin/bh-dispatch --cancel"),
                       xcode=lambda: self.xc,
                       xcode_status=lambda: self.xdiag if (self.xdiag.version or not self.xc) else XcodeStatus(self.xc),
                       host=lambda: Host("26.2", "arm64"),
                       timing=FAST, executors={"system.info": object(), "xcode.build": FakeBuild()}, uid=501,
                       service=self.svc, helper_running=lambda home: self.running, stop_wait_s=0,
                       cache_dirs=lambda uid: self.caches,
                       cache_removable=lambda roots, uid: list(roots) if self.cache_left is None else
                       (None if self.cache_left == "unknown" else self.cache_left))

    def _ask(self, prompt):
        self.out.append(f"? {prompt}")
        return self.answers.pop(0) if self.answers else ""

    def _sudo(self, argv):
        self.sudo_calls.append(argv)
        if argv[:1] == ["bash"] and argv[1].endswith("setup-bh-builder.sh"):
            self.installed, self.test_mode = True, False
        if argv[:1] == ["/bin/mv"]:
            self.rule, self.stale = True, False
        if argv[:2] == ["/usr/bin/cmp", "-s"]:
            return 0 if self.rule and not self.stale else 1
        return 0

    def _run(self, argv):
        if argv[0] in ("/bin/launchctl", "/usr/bin/dscl") and self.inspect_rc is not None:
            return self.inspect_rc, ""
        if argv[0] == "/bin/launchctl":
            return (0, "") if argv[-1] in self.loaded else (113, "")
        if argv[0] == "/usr/bin/dscl" and argv[2] == "-list":
            names = [x.rsplit("/", 1)[1] for x in self.loaded if x.startswith(argv[3] + "/")]
            if len(argv) > 4:                   # dscl . -list /Users UniqueID: "name uid" lines
                rows = {"root": "0", "daemon": "1", **{n: "470" for n in names}, **self.other_users}
                return 0, "".join(f"{n} {u}\n" for n, u in rows.items())
            return 0, "\n".join(["root", "daemon", *names, *self.other_users]) + "\n"
        return fake_run(argv)

    def _exists(self, p):
        p = str(p)
        if p in self.unknown_paths:
            return None
        return p in self.left or (p.startswith(self.tmp) and os.path.lexists(p))

    def _install(self):
        if self.test_mode:
            return InstallStatus(False, (tm_message(),))
        return InstallStatus(True, ()) if self.installed else InstallStatus(False, ("/opt/borrowed-hands/bin/bh-dispatch is missing: run 'bh setup'",))

    def text(self):
        return "\n".join(self.out + self.errs)


def bh(world, *argv):
    return cli.main(list(argv), env=world.env)


def arun(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


# ------------------------------------------------------------------ grants and caps
def paired(world):
    keyfile.ensure_private_dir(world.env.home)
    keyfile.create_device_key(world.env.home)
    cfg = H.paired_config(caps=(), ws=False)
    config.save(world.env.home, cfg)


def test_grant_add_list_revoke(tmp_path):
    w = World(tmp_path)
    paired(w)
    rc = bh(w, "grant", "add", "xcode.build", "--workspace", "demo", "--repo", H.REMOTE, "--project",
            "Demo.xcodeproj", "--scheme", "Demo", "--scheme", "Demo Tests", "--destination",
            "generic/platform=iOS Simulator", "--confirm", "--expires", "7d")
    assert rc == 0, w.text()
    ws = config.load(w.env.home).workspace("demo")
    assert ws.schemes == ("Demo", "Demo Tests") and ws.configurations == ("Debug",) and ws.confirm
    assert ws.destinations == ("generic/platform=iOS Simulator",) and ws.expires_at is not None
    assert bh(w, "grant", "list") == 0 and "demo" in w.text()
    assert bh(w, "grant", "revoke", "demo") == 0 and config.load(w.env.home).workspaces == ()
    assert bh(w, "grant", "revoke", "demo") == 2


@pytest.mark.parametrize("args,needle", [
    (["--repo", "http://github.com/a/b"], "https"),
    (["--repo", "https://tok@github.com/a/b"], "https"),
    (["--destination", "platform=iOS Simulator,name=iPhone 16"], "destination"),
    (["--scheme", "Alex's App"], "scheme"),
])
def test_grant_add_refuses_bad_input_with_exit_2(tmp_path, args, needle):
    w = World(tmp_path)
    paired(w)
    base = {"--workspace": "demo", "--repo": H.REMOTE, "--project": "Demo.xcodeproj", "--scheme": "Demo"}
    for k, v in zip(args[::2], args[1::2]):
        base[k] = v
    argv = ["grant", "add", "xcode.build"] + [x for kv in base.items() for x in kv]
    assert bh(w, *argv) == 2
    assert needle in w.text() and config.load(w.env.home).workspaces == ()


def test_caps_enable_requires_production_install(tmp_path):
    w = World(tmp_path, test_mode=True)
    paired(w)
    assert bh(w, "caps", "enable", "xcode.build") == 1
    assert tm_message() in w.text() and not config.load(w.env.home).capabilities.xcode_build
    w.test_mode, w.installed = False, True
    assert bh(w, "caps", "enable", "xcode.build") == 0 and config.load(w.env.home).capabilities.xcode_build
    assert bh(w, "caps", "enable", "system.info") == 0
    assert bh(w, "caps", "disable", "system.info") == 0 and not config.load(w.env.home).capabilities.system_info
    assert bh(w, "caps", "enable", "screen.capture") == 1        # not built in 0.1
    assert bh(w, "caps", "enable", "shell") == 2


def test_status_and_jobs_when_unpaired(tmp_path):
    w = World(tmp_path)
    keyfile.ensure_private_dir(w.env.home)
    assert bh(w, "status") == 0 and "not paired" in w.text()
    assert bh(w, "jobs") == 0


def test_cancel_marks_the_job(tmp_path):
    from borrowed_hands_helper.jobstore import JobStore
    w = World(tmp_path)
    paired(w)
    st = JobStore(w.env.home / "jobs.sqlite3")
    jid = "33333333-3333-4333-8333-333333333333"
    st.claim(job_id=jid, nonce="n", contract_hash="h", contract=b"{}", action="xcode.build", workspace_id="demo",
             expires_at=10**13, now=1)
    st.set_state(jid, "queued", 2)
    assert bh(w, "cancel", jid) == 0 and st.cancel_requests() == [jid]
    assert bh(w, "cancel", "44444444-4444-4444-8444-444444444444") == 2


# ------------------------------------------------------------------ service plist
def test_service_plist(tmp_path):
    data = plistlib.loads(service.plist_bytes(tmp_path, ["/x/python", "-I", "-m", "borrowed_hands_helper", "run"]))
    assert data["Label"] == "dev.borrowed-hands.helper" and data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["RunAtLoad"] is True and data["StandardOutPath"].endswith("Library/Logs/BorrowedHands/helper.log")
    calls = []
    ok, _ = service.install(tmp_path, uid=501, run=lambda a: (calls.append(a), (0, ""))[1],
                            argv=["/x/python", "run"])
    assert ok and calls == [["/bin/launchctl", "bootout", "gui/501/dev.borrowed-hands.helper"],
                            ["/bin/launchctl", "bootstrap", "gui/501", str(service.plist_path(tmp_path))]]
    assert service.uninstall(tmp_path, uid=501, run=lambda a: (113, "") if a[1] == "print" else (0, ""))[0]
    assert not service.plist_path(tmp_path).exists()


def test_service_uninstall_reports_a_helper_that_is_still_loaded(tmp_path):
    target = "gui/501/dev.borrowed-hands.helper"
    now = [0.0]
    fast = {"clock": lambda: now[0], "sleep": lambda s: now.__setitem__(0, now[0] + s)}
    ok, msg = service.uninstall(tmp_path, uid=501, run=lambda a: (0, "") if a[1] == "print" else (5, ""), **fast)
    assert not ok and f"launchctl bootout {target}" in msg
    ok, msg = service.uninstall(tmp_path, uid=501, run=lambda a: (113, "") if a[1] == "print" else (0, ""), **fast)
    assert ok
    # R3: a failed inspection (timeout, missing launchctl, ...) is not proof the helper stopped
    ok, msg = service.uninstall(tmp_path, uid=501, run=lambda a: (127, "") if a[1] == "print" else (0, ""), **fast)
    assert not ok and "could not confirm" in msg and f"launchctl print {target}" in msg


class StopClock:
    """`launchctl print` exit codes in order (the last one repeats), on a fake clock that sleep() advances."""

    def __init__(self, prints):
        self.prints, self.now, self.calls, self.said = list(prints), 0.0, [], []

    def run(self, argv):
        self.calls.append(argv[1])
        if argv[1] != "print":
            return 0, ""
        return (self.prints.pop(0) if len(self.prints) > 1 else self.prints[0]), ""

    def sleep(self, s):
        self.now += s

    def uninstall(self, home):
        (home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
        service.plist_path(home).write_bytes(b"plist")
        return service.uninstall(home, uid=501, run=self.run, clock=lambda: self.now, sleep=self.sleep,
                                 say=self.said.append)


def test_service_uninstall_waits_for_launchd_to_finish_the_bootout(tmp_path):
    # 2026-11-09, real Mac: bootout returned, the job was still loaded for a few seconds, then gone (113)
    c = StopClock([0, 0, 0, 0, 0, 0, 113])
    ok, msg = c.uninstall(tmp_path)
    assert ok, msg
    assert c.calls[0] == "bootout" and c.calls.count("print") == 7
    assert not service.plist_path(tmp_path).exists()
    assert any("waiting for the background helper to stop" in s for s in c.said), c.said


def test_service_uninstall_is_quiet_when_the_helper_stops_at_once(tmp_path):
    c = StopClock([113])
    ok, _ = c.uninstall(tmp_path)
    assert ok and c.said == [] and c.now == 0


def test_service_uninstall_gives_up_at_the_stop_deadline(tmp_path):
    c = StopClock([0])
    ok, msg = c.uninstall(tmp_path)
    assert not ok and "still loaded" in msg and "launchctl bootout gui/501/dev.borrowed-hands.helper" in msg
    assert service.STOP_WAIT_S <= c.now < service.STOP_WAIT_S + 1          # bounded, and it really waited
    assert service.STOP_WAIT_S >= 120                                       # at least the LaunchAgent ExitTimeOut
    # the job was never confirmed gone: keep its plist, so the install stays whole and a retry starts over
    assert service.plist_path(tmp_path).exists()


def test_service_uninstall_unknown_state_is_not_stopped(tmp_path):
    c = StopClock([127])
    ok, msg = c.uninstall(tmp_path)
    assert not ok and "could not confirm" in msg
    assert c.now >= service.STOP_WAIT_S and service.plist_path(tmp_path).exists()
    c = StopClock([127, 0, 113])                                            # a transient failure, then gone
    ok, msg = c.uninstall(tmp_path)
    assert ok, msg


# ------------------------------------------------------------------ bh uninstall: verified, with a checklist
TEARDOWN = ["bash", str(SANDBOX / "teardown-bh-builder.sh")]


def uninstall_world(tmp_path):
    w = World(tmp_path, installed=True, sudo_rule=True, answers=["y"])
    keyfile.ensure_private_dir(w.env.home)
    return w


def test_uninstall_clean_mac_prints_a_checklist_and_succeeds(tmp_path):
    w = uninstall_world(tmp_path)
    rc = bh(w, "uninstall")
    text = w.text()
    assert rc == 0, text
    assert TEARDOWN in w.sudo_calls
    assert ["/bin/rm", "-f", "/etc/sudoers.d/borrowed-hands", "/etc/sudoers.d/.borrowed-hands.new"] in w.sudo_calls
    for item in ("background helper stopped", "LaunchAgent removed", "pairing", "build runner stopped", "caches",
                 "/opt/borrowed-hands", "bh-builder user", "bh-builder group", "/Users/Shared/.bh-builder-home",
                 "sudo rule"):
        assert any(l.startswith("  ok ") and item in l for l in w.out), (item, text)
    assert "LEFT" not in text and "uv tool uninstall borrowed-hands-helper" in text
    assert not w.env.home.exists()


def test_uninstall_reports_leftovers_with_exact_commands_and_fails(tmp_path):
    w = uninstall_world(tmp_path)
    w.left |= {"/opt/borrowed-hands", "/etc/sudoers.d/borrowed-hands"}
    w.loaded |= {"/Users/bh-builder", "gui/501/dev.borrowed-hands.helper"}
    rc = bh(w, "uninstall")
    text = w.text()
    assert rc == 1, text
    assert "Uninstalled." not in text
    left = [l for l in w.out + w.errs if "LEFT" in l]
    assert any("/opt/borrowed-hands" in l and "teardown-bh-builder.sh" in l for l in left), left
    assert any("bh-builder user" in l and "teardown-bh-builder.sh" in l for l in left), left
    assert any("sudo rule" in l and "sudo rm -f /etc/sudoers.d/borrowed-hands" in l for l in left), left
    assert any("background helper" in l and "launchctl bootout gui/501/dev.borrowed-hands.helper" in l for l in left)
    assert "4 item(s) remain" in text


def test_uninstall_keeps_going_when_teardown_fails_and_still_removes_the_sudo_rule(tmp_path):
    w = uninstall_world(tmp_path)
    real = w._sudo

    def sudo(argv):
        if argv == TEARDOWN:
            w.sudo_calls.append(argv)
            w.left.add("/opt/borrowed-hands")
            return 1
        return real(argv)
    w.env.sudo = sudo
    rc = bh(w, "uninstall")
    assert rc == 1
    assert any(c[:2] == ["/bin/rm", "-f"] for c in w.sudo_calls)          # the rule is gone even so
    assert any("LEFT" in l and "/opt/borrowed-hands" in l for l in w.out + w.errs)


def test_helper_lock_probe_fails_closed(tmp_path):
    # R4 r2: uninstall's own probe; an unopenable lock is "cannot tell", never "not running"
    import fcntl
    from borrowed_hands_helper import daemon
    from borrowed_hands_helper.env import helper_lock_state
    home = tmp_path / "BorrowedHands"
    assert helper_lock_state(home) is False                          # nothing set up
    keyfile.ensure_private_dir(home)
    assert helper_lock_state(home) is False                          # no lock file: no helper ever started
    lock = home / daemon.LOCK_NAME
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        assert helper_lock_state(home) is False                      # free lock
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert helper_lock_state(home) is True                       # held
        os.chmod(lock, 0)
        assert helper_lock_state(home) is None                       # held, but cannot be opened: unknown
        assert daemon.daemon_running(home) is False                  # the daemon's own semantics are unchanged
    finally:
        os.chmod(lock, 0o600)
        os.close(fd)


def test_uninstall_fences_on_an_unreadable_held_lock(tmp_path):
    import fcntl
    from borrowed_hands_helper import daemon
    from borrowed_hands_helper.env import helper_lock_state
    w = uninstall_world(tmp_path)
    w.env.helper_running = helper_lock_state                         # the production probe, not a boolean fake
    lock = w.env.home / daemon.LOCK_NAME
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.chmod(lock, 0)
        rc = bh(w, "uninstall")
    finally:
        os.chmod(lock, 0o600)
        os.close(fd)
    assert rc == 1 and w.sudo_calls == [] and w.env.home.exists(), w.text()
    assert "could not check whether the helper is running" in w.text().lower()


def test_a_failed_teardown_is_never_reported_as_verified(tmp_path):
    # r2: teardown failed on something the checklist cannot see (uid 470's caches under /var/folders); every
    # path the checklist inspects is gone, but the uninstall must still fail and say why
    w = uninstall_world(tmp_path)
    real = w._sudo

    def sudo(argv):
        if argv == TEARDOWN:
            w.sudo_calls.append(argv)
            return 1
        return real(argv)
    w.env.sudo = sudo
    rc = bh(w, "uninstall")
    text = w.text()
    assert rc == 1, text
    assert "Uninstalled and verified" not in text
    assert any(l.startswith("  LEFT ") and "teardown" in l and "teardown-bh-builder.sh" in l for l in w.out), text


def test_a_retry_cannot_inherit_success_while_caches_remain(tmp_path):
    # r3: the first run failed on the caches; on the retry teardown exits 0, yet uid 470's caches are still there
    w = uninstall_world(tmp_path)
    w.caches = ["/private/var/folders/ab/xyz123"]
    rc = bh(w, "uninstall")
    text = w.text()
    assert rc == 1 and "Uninstalled and verified" not in text, text
    line = next(l for l in w.out if "caches" in l)
    assert line.startswith("  LEFT ") and "remove it with: sudo rm -rf /private/var/folders/ab/xyz123" in line, line


def test_uid_folders_scans_two_levels_and_fails_closed(tmp_path):
    from borrowed_hands_helper.env import uid_folders
    root = tmp_path / "folders"
    (root / "ab" / "mine").mkdir(parents=True)
    (root / "cd").mkdir()
    assert uid_folders(os.getuid(), str(root)) == [str(root / "ab" / "mine")]
    assert uid_folders(470, str(root)) == []
    os.chmod(root / "cd", 0)
    try:
        assert uid_folders(470, str(root)) is None                    # an unreadable level is not "none found"
    finally:
        os.chmod(root / "cd", 0o755)
    assert uid_folders(470, str(tmp_path / "missing")) is None


def test_cache_advice_never_deletes_another_accounts_uid_470_files(tmp_path):
    # r4: setup refused this Mac because alice already has uid 470; those folders are hers
    w = uninstall_world(tmp_path)
    w.caches = ["/private/var/folders/ab/foreign-cache"]
    w.other_users = {"alice": "470"}
    rc = bh(w, "uninstall")
    line = next(l for l in w.out if "caches" in l and "build user caches" in l)
    assert rc == 1 and "rm -rf" not in line and "alice" in line and "not touched" in line, line
    assert not line.startswith("  ok ")


def test_cache_advice_is_non_destructive_when_accounts_cannot_be_listed(tmp_path):
    w = uninstall_world(tmp_path)
    w.caches = ["/private/var/folders/ab/foreign-cache"]
    w.inspect_rc = 70                                                   # every dscl/launchctl query fails
    rc = bh(w, "uninstall")
    line = next(l for l in w.out if "build user caches" in l)
    assert rc == 1 and line.startswith("  UNKNOWN ") and "rm -rf" not in line and "could not check" in line, line


def test_cache_check_is_unknown_when_it_cannot_scan(tmp_path):
    w = uninstall_world(tmp_path)
    w.caches = None
    rc = bh(w, "uninstall")
    line = next(l for l in w.out if "caches" in l)
    assert rc == 1 and line.startswith("  UNKNOWN ") and "find /private/var/folders -maxdepth 2 -user 470" in line


def test_cache_check_points_at_teardown_while_the_build_user_exists(tmp_path):
    w = uninstall_world(tmp_path)
    w.caches = ["/private/var/folders/ab/xyz123"]
    w.loaded.add("/Users/bh-builder")
    bh(w, "uninstall")
    line = next(l for l in w.out if "caches" in l)
    assert line.startswith("  LEFT ") and "teardown-bh-builder.sh" in line and "rm -rf" not in line, line


SKEL = "/private/var/folders/xq/k3v9w1m7p2d8hs4t6b0yzc5r0000fp"


def test_the_macos_protected_skeleton_is_ok_with_a_note(tmp_path):
    # 2026-11-09, real Mac: after teardown only sunlnk dirs (and a data vault) remain; root cannot remove them
    w = uninstall_world(tmp_path)
    w.caches, w.cache_left = [SKEL], []
    rc = bh(w, "uninstall")
    text = w.text()
    line = next(l for l in w.out if "build user caches" in l)
    assert line.startswith("  ok ") and "left by macOS" in line and SKEL in line and "nothing to do" in line, line
    assert "rm -rf" not in line and "LEFT" not in text, text
    assert rc == 0 and "Uninstalled and verified" in text, text


def test_cache_advice_removes_only_what_is_not_protected(tmp_path):
    w = uninstall_world(tmp_path)
    w.caches, w.cache_left = [SKEL], [f"{SKEL}/X/leftover"]
    rc = bh(w, "uninstall")
    line = next(l for l in w.out if "build user caches" in l)
    assert rc == 1 and line.startswith("  LEFT "), line
    assert line.endswith(f"remove it with: sudo rm -rf {SKEL}/X/leftover"), line             # never the sunlnk dir


def test_cache_contents_that_cannot_be_inspected_are_unknown(tmp_path):
    w = uninstall_world(tmp_path)
    w.caches, w.cache_left = [SKEL], "unknown"
    rc = bh(w, "uninstall")
    line = next(l for l in w.out if "build user caches" in l)
    assert rc == 1 and line.startswith("  UNKNOWN ") and "rm -rf" not in line, line


def test_the_protected_skeleton_still_points_at_teardown_while_the_build_user_exists(tmp_path):
    # only the root teardown, with the account verified as ours, can look inside the 0700 folders
    w = uninstall_world(tmp_path)
    w.caches, w.cache_left = [SKEL], []
    w.loaded.add("/Users/bh-builder")
    rc = bh(w, "uninstall")
    line = next(l for l in w.out if "build user caches" in l)
    assert rc == 1 and line.startswith("  LEFT ") and "teardown-bh-builder.sh" in line, line


# ---- security review: every copy-paste command in the checklist is shell-quoted, and none is printed for a path
# holding control characters (names under T/ are chosen by build code running as uid 470)
import shlex  # noqa: E402

QUOTE, SUBST, NEWLINE = f"{SKEL}/T/it's", f"{SKEL}/T/$(touch pwned)", f"{SKEL}/T/x\nsudo rm -rf ~"


def step_of(line):
    return line.split("; remove it with: ", 1)[1]


def test_cache_rm_advice_quotes_hostile_names(tmp_path):
    w = uninstall_world(tmp_path)
    w.caches, w.cache_left = [SKEL], [QUOTE, SUBST]
    bh(w, "uninstall")
    line = next(l for l in w.out if "build user caches" in l)
    assert shlex.split(step_of(line)) == ["sudo", "rm", "-rf", QUOTE, SUBST], line


@pytest.mark.parametrize("bad", [NEWLINE, f"{SKEL}/T/esc\x1b[2Jx", f"{SKEL}/T/bidi‮gnp.txt", f"{SKEL}/T/del\x7f"])
def test_no_command_is_printed_for_a_path_with_control_characters(tmp_path, bad):
    w = uninstall_world(tmp_path)
    w.caches, w.cache_left = [SKEL], [QUOTE, bad]
    rc = bh(w, "uninstall")
    line = next(l for l in w.out if "build user caches" in l)
    assert rc == 1 and line.startswith("  LEFT ") and "\n" not in line, line
    assert "remove it with: no command shown" in line and "control characters" in line, line
    assert "remove it with: sudo" not in line and "\r" not in line, line
    assert bad not in line and not any(c in line for c in "\x1b‮\x7f"), line      # described, escaped


def test_checklist_paths_from_the_home_option_are_quoted(tmp_path):
    w = uninstall_world(tmp_path)
    odd = tmp_path / "it's $(id)"
    w.env = cli.replace(w.env, home=odd, user_home=odd)
    w.left |= {str(odd), str(service.plist_path(odd))}
    lines = {what: (state, step) for what, state, step in cli.uninstall_checklist(w.env)}
    state, step = next(v for k, v in lines.items() if k.startswith("pairing"))
    assert state == "LEFT" and shlex.split(step) == ["rm", "-rf", str(odd)], step
    state, step = next(v for k, v in lines.items() if k.startswith("LaunchAgent"))
    assert state == "LEFT" and shlex.split(step) == ["rm", str(service.plist_path(odd))], step
    w.unknown_paths.add(str(odd))
    lines = {what: (state, step) for what, state, step in cli.uninstall_checklist(w.env)}
    state, step = next(v for k, v in lines.items() if k.startswith("pairing"))
    assert state == "UNKNOWN" and shlex.split(step.split("check with: ", 1)[1]) == ["ls", "-ld", str(odd)], step


def test_paste_command_quotes_or_refuses():
    from borrowed_hands_helper.env import paste_command
    argv = ["rm", "-rf", "a'b", "$(x)", "`y`", "c d;e"]
    assert shlex.split(paste_command(argv)) == argv
    assert shlex.split(paste_command(argv, sudo=True)) == ["sudo", *argv]
    for bad in ("a\nb", "a\rb", "a\x00b", "a\x1bb", "a‮b", "a b", "a\x85b"):
        assert paste_command(["rm", bad]) is None, repr(bad)


def test_service_unlink_failure_advice_is_quoted(tmp_path):
    home = tmp_path / "it's $(id)"
    service.plist_path(home).mkdir(parents=True)                # a directory: unlink fails with an OSError
    ok, msg = service.uninstall(home, uid=501, run=lambda a: (113, "") if a[1] == "print" else (0, ""))
    assert not ok and shlex.split(msg.split("remove it with: ", 1)[1]) == ["rm", str(service.plist_path(home))], msg


class FakeTree:
    """lstat/listdir over {path: (mode, uid, flags) | errno}, for removable_entries."""

    def __init__(self, tree, unlistable=()):
        self.tree, self.unlistable, self.listed = tree, dict(unlistable), []

    def lstat(self, p):
        import stat as st
        from types import SimpleNamespace
        v = self.tree.get(p, errno.ENOENT)
        if isinstance(v, int):
            raise OSError(v, os.strerror(v), p)
        mode, uid, flags = v
        return SimpleNamespace(st_mode=mode | (st.S_IFDIR if mode == 0o755 else st.S_IFREG if mode == 0o644
                                                else st.S_IFLNK), st_uid=uid, st_flags=flags)

    def listdir(self, p):
        self.listed.append(p)
        if p in self.unlistable:
            raise OSError(self.unlistable[p], os.strerror(self.unlistable[p]), p)
        return sorted(c.rsplit("/", 1)[1] for c in self.tree if c.rsplit("/", 1)[0] == p)


import errno  # noqa: E402

D, F, L, NOUNLINK = 0o755, 0o644, 0o777, 0x00100000
REAL = {SKEL: (D, 470, NOUNLINK), f"{SKEL}/0": (D, 470, NOUNLINK), f"{SKEL}/0/com.apple.LaunchServices.dv": errno.EPERM,
        f"{SKEL}/T": (D, 470, NOUNLINK), f"{SKEL}/C": (D, 470, NOUNLINK), f"{SKEL}/X": (D, 470, NOUNLINK)}


def removable(tree, unlistable=()):
    from borrowed_hands_helper.env import removable_entries
    t = FakeTree(tree, unlistable)
    return removable_entries([SKEL], 470, lstat=t.lstat, listdir=t.listdir), t


def test_removable_entries_accepts_only_the_protected_skeleton():
    # T and C are 0700 to everyone but uid 470 and root: unreadable here, checked by the root teardown
    unreadable = {f"{SKEL}/T": errno.EACCES, f"{SKEL}/C": errno.EACCES}
    assert removable(REAL, unreadable)[0] == []
    assert removable({**REAL, f"{SKEL}/X/f": (F, 470, 0)}, unreadable)[0] == [f"{SKEL}/X/f"]
    assert removable({**REAL, f"{SKEL}/X/d": (D, 470, 0), f"{SKEL}/X/d/f": (F, 470, 0)})[0] == [f"{SKEL}/X/d"]
    assert removable({**REAL, f"{SKEL}/X/u": (D, 501, NOUNLINK)})[0] == [f"{SKEL}/X/u"]       # not uid 470's
    assert removable({**REAL, SKEL: (D, 470, 0)})[0] == [SKEL]                                # the root has no flag
    assert removable({**REAL, f"{SKEL}/X/uchg": (D, 470, 0x2)})[0] == [f"{SKEL}/X/uchg"]     # a user flag only


def test_removable_entries_never_follows_a_symlink_and_fails_closed():
    res, t = removable({**REAL, f"{SKEL}/X/link": (L, 470, NOUNLINK)})
    assert res == [f"{SKEL}/X/link"] and f"{SKEL}/X/link" not in t.listed
    assert removable({**REAL, SKEL: errno.EPERM})[0] is None                  # a data vault only inside a folder
    assert removable({**REAL, f"{SKEL}/X/io": errno.EIO})[0] is None
    assert removable(REAL, {f"{SKEL}/X": errno.EIO})[0] is None              # only "not readable by me" is skipped


def test_uninstall_failed_inspections_are_unknown_not_ok(tmp_path):
    # R3: nothing on disk, but launchctl/dscl could not be asked: that is not a verified removal
    w = uninstall_world(tmp_path)
    w.inspect_rc = 127
    w.svc.stop_result = (True, "removed")
    w.unknown_paths.add("/Users/Shared/.bh-builder-home")
    rc = bh(w, "uninstall")
    text = w.text()
    assert rc == 1, text
    assert "Uninstalled and verified" not in text
    unknown = [l for l in w.out if l.startswith("  UNKNOWN ")]
    for item in ("background helper stopped", "build runner stopped", "bh-builder user", "bh-builder group",
                 "/Users/Shared/.bh-builder-home"):
        assert any(item in l for l in unknown), (item, unknown)
    assert any("check with: /bin/launchctl print" in l for l in unknown) and any("dscl . -list" in l for l in unknown)


def test_uninstall_stops_before_teardown_when_the_helper_did_not_stop(tmp_path):
    # R4: never dismantle the sandbox, sudo rule or local state under a helper that may still be running
    w = uninstall_world(tmp_path)
    w.svc.stop_result = (False, "the background helper is still loaded; stop it with: launchctl bootout x")
    rc = bh(w, "uninstall")
    text = w.text()
    assert rc == 1 and w.sudo_calls == [], (w.sudo_calls, text)
    assert w.env.home.exists()
    assert "still running" in text and "bh uninstall" in text
    assert any(l.startswith("  LEFT ") for l in w.out)                  # the checklist still reports the state


def test_uninstall_stops_before_teardown_while_a_foreground_helper_runs(tmp_path):
    w = uninstall_world(tmp_path)
    w.running = True                                                    # `bh run` in another terminal
    rc = bh(w, "uninstall")
    assert rc == 1 and w.sudo_calls == [] and w.env.home.exists(), w.text()
    assert "still running" in w.text()


def test_uninstall_continues_after_a_filesystem_error_in_unpair(tmp_path, monkeypatch):
    # R5: an immutable key or job database must not abort the rest of the cleanup or the checklist
    from borrowed_hands_helper import account

    async def boom(*a, **k):
        raise PermissionError(1, "Operation not permitted", "jobs.sqlite3")
    monkeypatch.setattr(account, "unpair", boom)
    w = uninstall_world(tmp_path)
    paired(w)
    rc = bh(w, "uninstall")
    text = w.text()
    assert TEARDOWN in w.sudo_calls and any(c[:2] == ["/bin/rm", "-f"] for c in w.sudo_calls), text
    assert "Removal check:" in text and "Operation not permitted" in text
    assert rc == 0                 # the local state directory was still removed afterwards


def test_uninstall_declined_removes_nothing(tmp_path):
    w = uninstall_world(tmp_path)
    w.answers = ["n"]
    assert bh(w, "uninstall") == 1 and w.sudo_calls == [] and "Nothing removed." in w.text()


# ------------------------------------------------------------------ bh setup
def setup_answers(pairing):
    return [pairing, "y", "y", "demo", H.REMOTE, "Demo.xcodeproj", "Demo", "", "1", "n"]


def test_setup_clean_mac_end_to_end(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            w = World(tmp_path, answers=setup_answers(srv.pairing_string()))
            w.env.pair_insecure_local = True
            rc = await asyncio.get_running_loop().run_in_executor(None, lambda: bh(w, "setup"))
            return rc, w, srv
        finally:
            await srv.stop()
    rc, w, srv = arun(main())
    text = w.text()
    assert SUDO_TEXT in text
    # the production install, never test profiles; each sudo command shown before it ran
    setup_cmd = ["bash", str(SANDBOX / "setup-bh-builder.sh")]
    assert w.sudo_calls[0] == setup_cmd
    assert all("--with-test-profiles" not in c for c in w.sudo_calls)
    for c in w.sudo_calls:
        assert ("sudo " + " ".join(c if "'" not in " ".join(c) else c)).split()[0] == "sudo"
        assert any(line.startswith("  $ sudo ") and c[-1] in line for line in w.out), c
    # sudoers: validated with visudo before it is moved into place, mode 0440, pinned to the owner's uid
    flat = [" ".join(c) for c in w.sudo_calls]
    i_install = next(i for i, c in enumerate(flat) if c.startswith("/usr/bin/install"))
    i_visudo = next(i for i, c in enumerate(flat) if c.startswith("/usr/sbin/visudo -cf"))
    i_mv = next(i for i, c in enumerate(flat) if c.startswith("/bin/mv"))
    assert i_install < i_visudo < i_mv and "-m 0440" in flat[i_install]
    assert "/etc/sudoers.d/borrowed-hands" in flat[i_mv]
    cfg = config.load(w.env.home)
    assert cfg.server is not None and cfg.capabilities.system_info and cfg.capabilities.xcode_build
    assert cfg.workspace("demo").schemes == ("Demo",) and not cfg.workspace("demo").confirm
    assert w.svc.calls == ["install"]
    assert "Self-test" in text and rc == 0, text
    assert "ask Hermes: \"Build my demo project on my Mac\"" in w.out[-1], w.out[-1]


def test_sandbox_installer_points_users_at_bh_uninstall():
    # a uv-installed user has no repo checkout, so a repo-relative teardown path means nothing to them (F5)
    echoes = [l for l in (SANDBOX / "setup-bh-builder.sh").read_text().splitlines() if l.lstrip().startswith("echo")]
    last = echoes[-1]
    assert "bh uninstall" in last and "sandbox/teardown" not in last, last


def test_sudoers_pins_exact_argument_lists(tmp_path):
    from borrowed_hands_helper import setup
    from borrowed_hands_helper.config import Policy
    text = setup.sudoers_text(501, Policy())
    D = "#501 ALL=(root) NOPASSWD: /opt/borrowed-hands/bin/bh-dispatch"
    assert text.splitlines() == [
        f"{D} --timeout 300 --disk-budget-mb 20480 --mem-budget-mb 6144",
        f"{D} --timeout 900 --disk-budget-mb 20480 --mem-budget-mb 6144",
        f"{D} --timeout 1800 --disk-budget-mb 20480 --mem-budget-mb 6144",
        f"{D} --timeout 3600 --disk-budget-mb 20480 --mem-budget-mb 6144",
        f"{D} --sweep-only",
        f"{D} --cancel",
    ]
    for line in text.splitlines():                     # no glob, no character class, no wildcard of any kind
        cmd = line.split("NOPASSWD: ", 1)[1]
        assert not set(cmd) & set("*?[]\\!"), line
    other = setup.sudoers_text(502, Policy(build_timeout_s=60, disk_budget_mb=1000, mem_budget_mb=2000))
    assert other.startswith("#502 ") and "--disk-budget-mb 1000 --mem-budget-mb 2000" in other


@pytest.mark.skipif(not os.path.exists("/usr/sbin/visudo"), reason="needs visudo")
def test_sudoers_file_passes_visudo(tmp_path):
    import subprocess
    from borrowed_hands_helper import setup
    from borrowed_hands_helper.config import Policy
    f = tmp_path / "borrowed-hands"
    f.write_text(setup.sudoers_text(501, Policy()))
    r = subprocess.run(["/usr/sbin/visudo", "-cf", str(f)], capture_output=True, text=True)   # no sudo: a check only
    assert r.returncode == 0 and "parsed OK" in r.stdout + r.stderr


def test_setup_rerun_is_safe_and_skips_done_steps(tmp_path):
    w = World(tmp_path, installed=True, sudo_rule=True, answers=["n"] * 10)
    paired(w)
    rc = bh(w, "setup")
    # nothing to redo: the only admin command compares the installed rule with what it must be
    assert [c[:2] for c in w.sudo_calls] == [["/usr/bin/cmp", "-s"]], w.sudo_calls
    assert w.sudo_calls[0][-1] == "/etc/sudoers.d/borrowed-hands"
    assert "already paired" in w.text()
    assert rc in (0, 1)


def test_setup_installs_its_exact_rule_even_when_broad_sudo_already_works(tmp_path):
    w = World(tmp_path, installed=True, sudo_rule=False, broad_sudo=True, answers=[""])
    bh(w, "setup")
    flat = [" ".join(c) for c in w.sudo_calls]
    assert any(c.startswith("/usr/bin/install ") for c in flat), w.text()
    assert any(c.startswith("/usr/sbin/visudo -cf /etc/sudoers.d/.borrowed-hands.new") for c in flat)
    assert any(c == "/bin/mv -f /etc/sudoers.d/.borrowed-hands.new /etc/sudoers.d/borrowed-hands" for c in flat)
    assert "Sudo rule: already installed" not in w.text()


def test_selftest_reports_the_exact_rule_not_just_sudo_reachability(tmp_path):
    def mutate(w, s):
        w.rule, w.broad = False, True
    rc, w = run_selftest(tmp_path, mutate=mutate)
    text = w.text()
    assert rc == 1 and "FAIL  sudo rule: installed" in text and "/etc/sudoers.d/borrowed-hands" in text, text
    assert "PASS  sudo rule: permitted" in text and "sudo confirms each of the six build commands is allowed" in text
    assert "broader sudo" in text and "fix: run 'bh setup'" in text


def test_setup_replaces_a_rule_file_whose_bytes_differ(tmp_path):
    # (a): metadata alone is not "installed"; the bytes must be exactly sudoers_text(uid, policy)
    w = World(tmp_path, installed=True, sudo_rule=False, answers=[""])
    w.stale = True
    bh(w, "setup")
    flat = [" ".join(c) for c in w.sudo_calls]
    i_cmp = next(i for i, c in enumerate(flat) if c.startswith("/usr/bin/cmp -s "))
    i_mv = next(i for i, c in enumerate(flat) if c.startswith("/bin/mv -f "))
    assert i_cmp < i_mv and any(c.startswith("/usr/sbin/visudo -cf") for c in flat), flat

    assert "Sudo rule: already installed" not in w.text()


def test_setup_reports_an_overriding_rule_and_touches_no_other_sudoers_file(tmp_path):
    # (b): our exact file is in place, but sudo itself refuses one exact command: another rule overrides ours
    w = World(tmp_path, installed=True, sudo_rule=True, answers=[""])
    w.override = "/opt/borrowed-hands/bin/bh-dispatch --cancel"
    rc = bh(w, "setup")
    text = w.text()
    assert rc != 0, text
    assert "another sudo rule" in text and "/opt/borrowed-hands/bin/bh-dispatch --cancel" in text
    assert [c[:2] for c in w.sudo_calls] == [["/usr/bin/cmp", "-s"]], w.sudo_calls   # no install, mv or edits
    assert "[4/7]" not in text


def test_setup_reports_an_override_found_right_after_installing(tmp_path):
    w = World(tmp_path, installed=True, sudo_rule=False, answers=[""])
    w.override = "/opt/borrowed-hands/bin/bh-dispatch --sweep-only"
    rc = bh(w, "setup")
    assert rc != 0 and "another sudo rule" in w.text() and "--sweep-only" in w.text(), w.text()


def test_selftest_reports_installed_and_effective_separately(tmp_path):
    def mutate(w, s):
        w.override = "/opt/borrowed-hands/bin/bh-dispatch --cancel"
    rc, w = run_selftest(tmp_path, mutate=mutate)
    text = w.text()
    assert rc == 1, text
    assert "PASS  sudo rule: installed" in text
    assert "FAIL  sudo rule: permitted" in text and "bh-dispatch --cancel" in text


def test_setup_partial_failure_says_what_was_already_done(tmp_path):
    w = World(tmp_path, answers=[""])
    real = w._sudo

    def sudo(argv):
        return 1 if argv[0] == "/usr/sbin/visudo" else real(argv)
    w.env.sudo = sudo
    assert bh(w, "setup") == 1
    err = "\n".join(w.errs)
    assert "nothing else was changed" not in w.text()
    assert "Setup stopped at [3/7] sudo rule" in err and "exit 1" in err, err
    assert "Already done: Xcode check, build sandbox" in err
    assert "may be partly applied" in err and "run 'bh setup' again" in err and "bh uninstall" in err


def test_setup_stops_without_xcode(tmp_path):
    w = World(tmp_path, xcode=None)
    assert bh(w, "setup") == 1
    assert any("Xcode is not installed at /Applications/Xcode.app" in e and "App Store" in e for e in w.errs), w.errs
    assert w.sudo_calls == []


def test_setup_with_another_xcode_selected_continues_and_notes_it(tmp_path):
    # R6: builds use /Applications/Xcode.app whatever is selected, so setup must not stop here
    w = World(tmp_path, xcode=NOT_SELECTED.version, xcode_diag=NOT_SELECTED, answers=[""])
    bh(w, "setup")
    text = w.text()
    assert "[1/7] Xcode 26.1 (17B55) at /Applications/Xcode.app: ok" in text, text
    assert CLT in text and "[2/7]" in text


def test_setup_rejected_pairing_code(tmp_path):
    async def main():
        srv = await FakeServer(pair_error="expired").start()
        try:
            w = World(tmp_path, installed=True, sudo_rule=True, answers=[srv.pairing_string()])
            w.env.pair_insecure_local = True
            return await asyncio.get_running_loop().run_in_executor(None, lambda: bh(w, "setup")), w
        finally:
            await srv.stop()
    rc, w = arun(main())
    assert rc == 2 and "pairing code rejected; run /hands pair again" in w.text()


# ------------------------------------------------------------------ bh selftest
def selftest_world(tmp_path, srv, **kw):
    w = World(tmp_path, installed=True, sudo_rule=True, **kw)
    return w


async def paired_with(srv, w):
    from borrowed_hands_helper import account
    rc = await account.pair(w.env.home, srv.pairing_string(), name="Test Mac", insecure_local=True, timing=FAST,
                            host=lambda: Host("26.2", "arm64"), out=lambda *_: None)
    assert rc == 0
    cfg = config.load(w.env.home).with_capability("system.info", True).with_capability("xcode.build", True)
    config.save(w.env.home, cfg.with_workspace(H.workspace()))


def run_selftest(tmp_path, mutate=None, server_kw=None, argv=("selftest",)):
    async def main():
        srv = await FakeServer(**(server_kw or {})).start()
        try:
            w = selftest_world(tmp_path, srv)
            await paired_with(srv, w)
            if server_kw:
                pass
            if mutate:
                mutate(w, srv)
            rc = await asyncio.get_running_loop().run_in_executor(None, lambda: bh(w, *argv))
            return rc, w
        finally:
            await srv.stop()
    return arun(main())


def test_selftest_all_pass(tmp_path):
    rc, w = run_selftest(tmp_path)
    assert rc == 0, w.text()
    assert "FAIL" not in w.text() and w.text().count("PASS") >= 7


def test_selftest_fails_on_test_mode(tmp_path):
    rc, w = run_selftest(tmp_path, mutate=lambda w, s: setattr(w, "test_mode", True))
    assert rc == 1 and tm_message() in w.text()


def test_selftest_fails_without_sudo_rule(tmp_path):
    rc, w = run_selftest(tmp_path, mutate=lambda w, s: setattr(w, "rule", False))
    text = w.text()
    assert rc == 1 and "FAIL  sudo rule: installed" in text and "FAIL  sudo rule: permitted" in text, text


def test_selftest_fails_without_xcode(tmp_path):
    rc, w = run_selftest(tmp_path, mutate=lambda w, s: setattr(w, "xc", None))
    assert rc == 1 and "Xcode is not installed at /Applications/Xcode.app" in w.text()
    assert "App Store" in w.text()


def test_selftest_with_another_xcode_selected_passes_with_a_note(tmp_path):
    def mutate(w, s):
        w.xc, w.xdiag = NOT_SELECTED.version, NOT_SELECTED
    rc, w = run_selftest(tmp_path, mutate=mutate)
    text = w.text()
    assert rc == 0, text
    line = next(l for l in w.out if l.startswith("  PASS  Xcode"))
    assert "/Applications/Xcode.app" in line and CLT in line, line


def test_selftest_on_a_never_set_up_mac_points_at_setup_not_re_pairing(tmp_path):
    w = World(tmp_path, installed=True, sudo_rule=True)
    assert not w.env.home.exists()
    rc = bh(w, "selftest")
    text = w.text()
    assert rc == 1 and "bh pair" not in text and "again" not in text, text
    assert "PASS  key and config permissions: nothing stored yet" in text
    assert "FAIL  paired: not paired" in text and "fix: run 'bh setup'" in text


def test_selftest_unsafe_storage_on_an_unpaired_mac_does_not_say_pair_again(tmp_path):
    w = World(tmp_path, installed=True, sudo_rule=True)
    keyfile.ensure_private_dir(w.env.home)
    os.chmod(w.env.home, 0o755)
    rc = bh(w, "selftest")
    text = w.text()
    assert rc == 1 and "chmod 700" in text and "bh pair" not in text, text


def test_selftest_fails_on_server_key_proof(tmp_path):
    rc, w = run_selftest(tmp_path, mutate=lambda w, s: setattr(s, "bad_server_sig", True))
    assert rc == 1 and "server failed to prove its key; check the pairing string" in w.text()
    # a paired Mac gets the full re-pair sequence ('bh pair' alone refuses while the helper runs)
    assert "fix: re-pair: run 'bh unpair', then 'bh setup'" in w.text()


def test_selftest_fails_on_unsafe_key_file(tmp_path):
    rc, w = run_selftest(tmp_path, mutate=lambda w, s: os.chmod(w.env.home / keyfile.KEY_NAME, 0o644))
    assert rc == 1 and "0600" in w.text()


def test_selftest_build_needs_the_xcode_executor(tmp_path):
    def drop(w, s):
        w.env.executors = {"system.info": object()}
    rc, w = run_selftest(tmp_path, mutate=drop, argv=("selftest", "--build", "demo"))
    assert rc == 1 and "xcode.build" in w.text()
