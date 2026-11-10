"""Capabilities (PROTOCOL 6.1), the production gate (SPEC 9.4, SE-8) and system.info (CA-6)."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import helpers as H
from borrowed_hands_helper import caps, schema
from borrowed_hands_helper.caps import InstallStatus, Probes
from borrowed_hands_helper.executors import AcceptedJob
from borrowed_hands_helper.executors.system_info import SystemInfoExecutor

GOOD = Probes(install=InstallStatus(True, ()), sudo_ok=True, xcode=("26.1", "17B55"), screen_granted=False)


def fake_prefix(tmp_path: Path, test_mode=False, profiles=()) -> Path:
    p = tmp_path / "opt" / "borrowed-hands"
    (p / "bin").mkdir(parents=True)
    (p / "libexec" / "profiles").mkdir(parents=True)
    (p / "bin" / "bh-dispatch").write_text("#!/usr/bin/python3\n")
    os.chmod(p / "bin" / "bh-dispatch", 0o755)
    if test_mode:
        (p / "TEST_MODE").write_text("")
    for name in profiles:
        (p / "libexec" / "profiles" / name).write_text("")
    return p


def test_install_check_requires_root_ownership(tmp_path):
    # files we created are owned by us, not root: the check must say so (and never trust them)
    st = caps.production_install(fake_prefix(tmp_path), root_uid=0)
    assert not st.ok and any("root" in p for p in st.problems)


def test_install_check_ok_when_owner_matches(tmp_path):
    st = caps.production_install(fake_prefix(tmp_path), root_uid=os.getuid(), check_parents=False,
                                 account=lambda: None)
    assert st.ok, st.problems


def test_install_check_test_mode_message(tmp_path):
    st = caps.production_install(fake_prefix(tmp_path, test_mode=True), root_uid=os.getuid(), check_parents=False)
    assert not st.ok
    assert any(p.startswith("build sandbox is a TEST install (TEST_MODE present): run 'sudo bash ")
               and p.endswith("/sandbox/setup-bh-builder.sh' with no flags") for p in st.problems)


def test_install_check_test_profiles(tmp_path):
    st = caps.production_install(fake_prefix(tmp_path, profiles=("allow-all.sb",)), root_uid=os.getuid(),
                                 check_parents=False)
    assert not st.ok and any("profiles" in p for p in st.problems)


def test_install_check_missing(tmp_path):
    st = caps.production_install(tmp_path / "nope", root_uid=0)
    assert not st.ok and any("bh setup" in p for p in st.problems)


def test_install_check_group_writable_dispatcher(tmp_path):
    p = fake_prefix(tmp_path)
    os.chmod(p / "bin" / "bh-dispatch", 0o775)
    assert not caps.production_install(p, root_uid=os.getuid(), check_parents=False).ok


def test_usable_actions_gate():
    cfg = H.paired_config(caps=("system.info", "xcode.build", "screen.capture"))
    assert caps.usable(cfg, GOOD) == {"system.info", "xcode.build"}
    bad = Probes(install=InstallStatus(False, ("TEST_MODE",)), sudo_ok=True, xcode=GOOD.xcode, screen_granted=True)
    assert caps.usable(cfg, bad) == {"system.info", "screen.capture"}               # SE-8
    assert "xcode.build" not in caps.usable(cfg, Probes(GOOD.install, False, GOOD.xcode, False))
    assert "xcode.build" not in caps.usable(cfg, Probes(GOOD.install, True, None, False))
    assert caps.usable(H.paired_config(caps=()), GOOD) == frozenset()                 # pairing grants nothing


def test_caps_body_matches_schema_and_hides_urls():
    cfg = H.paired_config(caps=("system.info", "xcode.build"))
    body = caps.caps_body(cfg, GOOD, busy=True, queue_depth=2, host=caps.Host("26.2", "arm64"))
    schema.validate("caps", body)
    assert body["busy"] is True and body["queue_depth"] == 2
    xb = [c for c in body["capabilities"] if c["name"] == "xcode.build"][0]
    assert xb["workspaces"][0]["repo_fingerprint"] == cfg.workspaces[0].fingerprint
    assert "github.com" not in repr(body)


def test_caps_body_after_pairing_is_empty():
    body = caps.caps_body(H.paired_config(caps=(), ws=False), GOOD, busy=False, queue_depth=0,
                          host=caps.Host("26.2", "arm64"))
    schema.validate("caps", body)
    assert body["capabilities"] == []


def test_caps_body_no_xcode_build_on_test_install():
    cfg = H.paired_config(caps=("system.info", "xcode.build"))
    bad = Probes(install=InstallStatus(False, ("x",)), sudo_ok=True, xcode=GOOD.xcode, screen_granted=False)
    body = caps.caps_body(cfg, bad, busy=False, queue_depth=0, host=caps.Host("26.2", "arm64"))
    assert [c["name"] for c in body["capabilities"]] == ["system.info"]


def test_caps_expiry_is_ms():
    cfg = H.paired_config().with_workspace(H.workspace(expires_at=1893456000000), replace_existing=True)
    body = caps.caps_body(cfg, GOOD, busy=False, queue_depth=0, host=caps.Host("26.2", "arm64"))
    xb = [c for c in body["capabilities"] if c["name"] == "xcode.build"][0]
    assert xb["workspaces"][0]["expires_at"] == 1893456000000


DEV = "/Applications/Xcode.app/Contents/Developer"
SEL = ("/usr/bin/xcode-select", "-p")
VER = ("/usr/bin/env", f"DEVELOPER_DIR={DEV}", f"{DEV}/usr/bin/xcodebuild", "-version")
LIC = ("/usr/bin/env", f"DEVELOPER_DIR={DEV}", f"{DEV}/usr/bin/xcodebuild", "-license", "check")


def test_xcode_info_parse():
    def run_with(version_out):
        return lambda argv: {SEL: (0, DEV + "\n"), VER: (0, version_out)}.get(tuple(argv), (1, ""))
    assert caps.xcode_info(run_with("Xcode 26.1\nBuild version 17B55\n")) == ("26.1", "17B55")
    assert caps.xcode_info(lambda argv: (1, "")) is None
    assert caps.xcode_info(run_with("Xcode 26.1 beta\nBuild version 17B55\n")) is None


def test_sudo_rule_probe_lists_the_exact_argv_the_helper_will_run():
    from borrowed_hands_helper.config import Policy
    seen = []

    def run(argv):
        seen.append(argv)
        return 0, ""
    assert caps.sudo_rule_ok(Policy(), run) is True
    assert seen == [["/usr/bin/sudo", "-n", "-l", "/opt/borrowed-hands/bin/bh-dispatch", "--timeout", "900",
                     "--disk-budget-mb", "20480", "--mem-budget-mb", "6144"]]
    assert caps.sudo_rule_ok(Policy(), lambda argv: (1, "sudo: a password is required")) is False


# ---- the sudo rule (R2 r3): (a) our file, exactly; (b) sudo itself confirms each exact command. No parsing of
# sudo's policy language: sudo applies every later rule, negation, directory spec and argument regex itself.
import stat as statmod
from types import SimpleNamespace

D = "/opt/borrowed-hands/bin/bh-dispatch"
RULE_ARGV = [[D, "--timeout", str(t), "--disk-budget-mb", "20480", "--mem-budget-mb", "6144"]
             for t in (300, 900, 1800, 3600)] + [[D, "--sweep-only"], [D, "--cancel"]]


def fake_lstat(mode=statmod.S_IFREG | 0o440, uid=0, gid=0, missing=False, error=None):
    def lstat(path):
        assert path == "/etc/sudoers.d/borrowed-hands"
        if missing:
            raise FileNotFoundError(path)
        if error:
            raise error
        return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)
    return lstat


def test_rule_file_must_be_a_root_wheel_0440_regular_file():
    assert caps.sudo_rule_file(lstat=fake_lstat()) == (True, "")
    for kw, needle in [({"missing": True}, "missing"), ({"mode": statmod.S_IFLNK | 0o777}, "regular file"),
                       ({"mode": statmod.S_IFDIR | 0o755}, "regular file"), ({"uid": 501}, "root:wheel"),
                       ({"gid": 20}, "root:wheel"), ({"mode": statmod.S_IFREG | 0o644}, "0440"),
                       ({"mode": statmod.S_IFREG | 0o400}, "0440"), ({"error": PermissionError(13, "denied")}, "check")]:
        ok, why = caps.sudo_rule_file(lstat=fake_lstat(**kw))
        assert not ok and needle in why, (kw, why)


def test_rule_effective_asks_sudo_about_each_exact_argv():
    from borrowed_hands_helper.config import Policy
    seen = []

    def run(argv):
        seen.append(argv)
        return 0, ""
    assert caps.sudo_rule_permitted(Policy(), run) is None
    assert seen == [["/usr/bin/sudo", "-n", "-l", "--", *a] for a in RULE_ARGV]
    assert caps.rule_commands(Policy()) == [" ".join(a) for a in RULE_ARGV]


@pytest.mark.parametrize("refused", range(6))
def test_rule_effective_names_the_command_sudo_refuses(refused):
    # e.g. a later "(root) PASSWD: /opt/borrowed-hands/bin/" or "!... ^--cancel$" rule: sudo applies it, we don't
    from borrowed_hands_helper.config import Policy

    def run(argv):
        return (1, "") if argv[4:] == RULE_ARGV[refused] else (0, "")
    assert caps.sudo_rule_permitted(Policy(), run) == " ".join(RULE_ARGV[refused])


def test_rule_effective_fails_closed_when_sudo_cannot_run():
    from borrowed_hands_helper.config import Policy
    assert caps.sudo_rule_permitted(Policy(), lambda argv: (127, "")) == " ".join(RULE_ARGV[0])


def test_the_sudo_listing_parser_is_gone():
    for name in ("sudo_rule_installed", "rule_effective", "_privilege_entries", "_runas_root", "_touches"):
        assert not hasattr(caps, name), name


@pytest.mark.parametrize("asked,tier", [(60, 300), (300, 300), (301, 900), (900, 900), (901, 1800),
                                        (1800, 1800), (2000, 3600), (3600, 3600), (99999, 3600)])
def test_timeout_maps_up_to_a_fixed_tier(asked, tier):
    assert caps.timeout_tier(asked) == tier


def test_dispatch_argv():
    from borrowed_hands_helper.config import Policy
    assert caps.dispatch_argv(Policy(build_timeout_s=1000, disk_budget_mb=500, mem_budget_mb=700)) == [
        "/opt/borrowed-hands/bin/bh-dispatch", "--timeout", "1800", "--disk-budget-mb", "500", "--mem-budget-mb", "700"]


# ---- system.info: exactly the narrowed fields
def fake_run(argv):
    table = {
        ("/usr/bin/sw_vers", "-productVersion"): "26.2\n",
        ("/usr/bin/sw_vers", "-buildVersion"): "25C56\n",
        ("/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"): "Apple M4\n",
        ("/usr/sbin/sysctl", "-n", "hw.memsize"): str(16 * 2**30) + "\n",
        SEL: DEV + "\n",
        VER: "Xcode 26.1\nBuild version 17B55\n",
    }
    return 0, table[tuple(argv)]


def test_system_info_result_shape():
    ex = SystemInfoExecutor(run=fake_run, arch=lambda: "arm64", disk_free=lambda: 100 * 2**30 + 5)
    job = AcceptedJob(job_id="0b9f3c2e-1d4a-4c5b-8e6f-7a8b9c0d1e2f", action="system.info", params={}, contract={},
                      contract_hash="x", expires_at=0, approval_ref="none", workspace=None)

    async def emit(state):
        pass
    out = asyncio.run(ex.run(job, emit))
    assert out.state == "succeeded" and out.error is None
    assert out.result == {"os_version": "26.2", "os_build": "25C56", "arch": "arm64", "cpu": "Apple M4",
                          "memory_gb": 16, "xcode": {"version": "26.1", "build": "17B55"}, "disk_free_gb": 100}
    schema.validate("job-result", {"job_id": job.job_id, "state": "succeeded", "result": out.result, "error": None})


def test_system_info_without_xcode():
    def run(argv):
        if tuple(argv) == VER:
            return 127, ""
        return fake_run(argv)
    ex = SystemInfoExecutor(run=run, arch=lambda: "arm64", disk_free=lambda: 0)
    job = AcceptedJob("0b9f3c2e-1d4a-4c5b-8e6f-7a8b9c0d1e2f", "system.info", {}, {}, "x", 0, "none", None)

    async def emit(state):
        pass
    assert asyncio.run(ex.run(job, emit)).result["xcode"] is None


# ---- Xcode: the probe checks the exact copy the build runner uses (bh-runner DEVELOPER_DIR)
CLT = "/Library/Developer/CommandLineTools"


def xrun(table):
    def run(argv):
        return table.get(tuple(argv), (1, ""))
    return run


def test_runner_developer_dir_matches_the_probe():
    import re
    from borrowed_hands_helper._shared import SANDBOX
    m = re.search(r'^DEVELOPER_DIR = "([^"]+)"$', (SANDBOX / "bh-runner").read_text(), re.M)
    assert m and m.group(1) == caps.RUNNER_DEVELOPER_DIR == DEV


def test_xcode_status_ok_reports_the_runner_copy():
    table = {SEL: (0, DEV + "\n"), VER: (0, "Xcode 26.1\nBuild version 17B55\n")}
    s = caps.xcode_status(xrun(table))
    assert s.version == ("26.1", "17B55") and s.problem == "" and s.fix == ""
    assert caps.xcode_info(xrun(table)) == ("26.1", "17B55")


def test_xcode_selected_elsewhere_still_builds_with_the_runner_copy():
    # R6: the runner sets DEVELOPER_DIR itself, so another global selection is a note, never a failure
    for selected in (CLT, "/Applications/Xcode-beta.app/Contents/Developer"):
        s = caps.xcode_status(xrun({SEL: (0, selected + "\n"), VER: (0, "Xcode 26.1\nBuild version 17B55\n")}))
        assert s.version == ("26.1", "17B55") and s.problem == "" and s.fix == ""
        assert selected.removesuffix("/Contents/Developer") in s.note and "/Applications/Xcode.app" in s.note
    s = caps.xcode_status(xrun({SEL: (0, DEV + "\n"), VER: (0, "Xcode 26.1\nBuild version 17B55\n")}))
    assert s.note == ""
    s = caps.xcode_status(xrun({SEL: (1, ""), VER: (0, "Xcode 26.1\nBuild version 17B55\n")}))
    assert s.version == ("26.1", "17B55")


def test_xcode_selected_elsewhere_and_missing_from_the_runner_path():
    beta = "/Applications/Xcode-beta.app/Contents/Developer"
    s = caps.xcode_status(xrun({SEL: (0, beta + "\n"), VER: (127, "")}))
    assert s.version is None and "/Applications/Xcode-beta.app" in s.problem
    assert "/Applications/Xcode.app" in s.problem + s.fix


def test_xcode_not_installed():
    s = caps.xcode_status(xrun({SEL: (0, CLT + "\n"), VER: (127, "")}))
    assert s.version is None and "not installed at /Applications/Xcode.app" in s.problem
    assert "App Store" in s.fix


def test_xcode_license_and_first_launch_fixes_target_the_runner_copy():
    pinned = f"sudo /usr/bin/env DEVELOPER_DIR={DEV} {DEV}/usr/bin/xcodebuild"
    for selected in (DEV, CLT):            # a bare 'sudo xcodebuild' would act on whatever is selected
        s = caps.xcode_status(xrun({SEL: (0, selected + "\n"), VER: (69, ""), LIC: (1, "")}))
        assert s.version is None and "license" in s.problem and s.fix == f"run '{pinned} -license accept'"
        s = caps.xcode_status(xrun({SEL: (0, selected + "\n"), VER: (1, ""), LIC: (0, "")}))
        assert s.version is None and f"'{pinned} -runFirstLaunch'" in s.fix
        assert "sudo xcodebuild" not in s.fix


# ---- Security review round 1 F3: an open "administer your computer" prompt from an uninstall can delete bh-builder after the
# command ended, leaving the dispatcher in place. A dispatcher without its build user is not a ready install.
def _pw(uid, gid):
    from types import SimpleNamespace
    return lambda name: SimpleNamespace(pw_uid=uid, pw_gid=gid) if name == "bh-builder" else None


def _gone(name):
    raise KeyError(name)


def test_install_check_needs_the_build_user(tmp_path):
    p = fake_prefix(tmp_path)
    ok = caps.production_install(p, root_uid=os.getuid(), check_parents=False,
                                 account=lambda: caps.builder_account_problem(_pw(470, 470), _gr(470)))
    assert ok.ok, ok.problems
    st = caps.production_install(p, root_uid=os.getuid(), check_parents=False,
                                 account=lambda: caps.builder_account_problem(_gone, _gr(470)))
    assert not st.ok and st.problems == (caps.BUILDER_MISSING,), st.problems
    assert "bh uninstall" in caps.BUILDER_MISSING and "bh setup" in caps.BUILDER_MISSING


@pytest.mark.parametrize("uid,gid", [(471, 470), (470, 20), (501, 20)])
def test_install_check_refuses_a_bh_builder_that_is_not_ours(tmp_path, uid, gid):
    st = caps.production_install(fake_prefix(tmp_path), root_uid=os.getuid(), check_parents=False,
                                 account=lambda: caps.builder_account_problem(_pw(uid, gid), _gr(470)))
    assert not st.ok and any("bh-builder" in p and f"{uid}:{gid}" in p for p in st.problems), st.problems


def test_the_default_install_check_looks_up_the_account():
    import inspect
    assert inspect.signature(caps.production_install).parameters["account"].default is caps.builder_account_problem


def _gr(gid):
    from types import SimpleNamespace
    return lambda name: SimpleNamespace(gr_gid=gid) if name == "bh-builder" else None


# ---- Security review round 2 N5: the LaunchDaemon names both UserName and GroupName bh-builder: the group must be there too
@pytest.mark.parametrize("group,ok", [(_gr(470), True), (_gone, False), (_gr(20), False)])
def test_install_check_needs_the_build_group(tmp_path, group, ok):
    st = caps.production_install(fake_prefix(tmp_path), root_uid=os.getuid(), check_parents=False,
                                 account=lambda: caps.builder_account_problem(_pw(470, 470), group))
    assert st.ok is ok, st.problems
    if not ok:
        assert any("group bh-builder" in p for p in st.problems), st.problems
