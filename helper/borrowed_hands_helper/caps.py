"""What this Mac can do right now (PROTOCOL 6.1) and the production gate (SPEC 9.4, SE-8).

A capability is advertised (and accepted, PROTOCOL 4.2 step 7) only if it is enabled in config **and**
usable: `xcode.build` needs Xcode, the production sandbox install (root-owned dispatcher, no TEST_MODE, no
test profiles) and the pinned sudoers rule; `screen.capture` needs its Screen Recording grant.
The workspace `remote_url` is never sent, only its fingerprint.
"""
from __future__ import annotations

import os
import platform
import re
import stat
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from . import __version__
from ._shared import SANDBOX
from .config import DESTINATIONS, Config, Policy

PREFIX = Path("/opt/borrowed-hands")
DISPATCH = str(PREFIX / "bin" / "bh-dispatch")
SUDO = "/usr/bin/sudo"

Run = Callable[[list], tuple]      # argv -> (returncode, stdout)


def run_cmd(argv: list, timeout: float = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C"})
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


@dataclass(frozen=True)
class InstallStatus:
    ok: bool
    problems: tuple[str, ...]


@dataclass(frozen=True)
class Probes:
    install: InstallStatus
    sudo_ok: bool
    xcode: Optional[tuple[str, str]]       # (version, build)
    screen_granted: bool
    displays: int = 1


@dataclass(frozen=True)
class Host:
    os_version: str
    arch: str


def test_mode_message() -> str:
    return (f"build sandbox is a TEST install (TEST_MODE present): run 'sudo bash {SANDBOX}/setup-bh-builder.sh' "
            f"with no flags")


def _root_owned_file(p: Path, root_uid: int) -> Optional[str]:
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return f"{p} is missing: run 'bh setup'"
    if not stat.S_ISREG(st.st_mode) and not stat.S_ISDIR(st.st_mode):
        return f"{p} is not a regular file or directory"
    if st.st_uid != root_uid:
        return f"{p} is not owned by root: reinstall with 'bh setup'"
    if st.st_mode & 0o022:
        return f"{p} is writable by group or others: reinstall with 'bh setup'"
    return None


def production_install(prefix: Path = PREFIX, root_uid: int = 0, check_parents: bool = True) -> InstallStatus:
    """The production gate: a root-owned, non-writable dispatcher (and its directories), no TEST_MODE,
    and no test profiles. The helper never trusts /opt/borrowed-hands files it did not check here."""
    prefix = Path(prefix)
    problems = []
    dispatcher = prefix / "bin" / "bh-dispatch"
    chain = [dispatcher, prefix / "bin", prefix]
    if check_parents:
        chain += [p for p in prefix.parents if str(p) != "."]
    for p in chain:
        why = _root_owned_file(p, root_uid)
        if why:
            problems.append(why)
            break
    if os.path.lexists(prefix / "TEST_MODE"):
        problems.append(test_mode_message())
    prof = prefix / "libexec" / "profiles"
    try:
        extra = sorted(os.listdir(prof))
    except FileNotFoundError:
        extra = []
    except OSError:
        extra = ["(unreadable)"]
    if extra:
        problems.append(f"test sandbox profiles are installed in {prof}: run 'sudo bash {SANDBOX}/"
                        f"setup-bh-builder.sh' with no flags")
    return InstallStatus(not problems, tuple(problems))


# The sudoers rule pins exact argument lists, one per tier (no globs): a requested build timeout is rounded up
# to the next tier, capped at the runner's maximum.
TIMEOUT_TIERS = (300, 900, 1800, 3600)


def timeout_tier(seconds: int) -> int:
    return next((t for t in TIMEOUT_TIERS if seconds <= t), TIMEOUT_TIERS[-1])


def dispatch_argv(policy: Policy) -> list[str]:
    """The exact dispatcher argv the helper runs under sudo; the installed sudoers lines match it verbatim.
    The xcode executor must use this (prefixed with `/usr/bin/sudo -n`)."""
    return [DISPATCH, "--timeout", str(timeout_tier(policy.build_timeout_s)),
            "--disk-budget-mb", str(policy.disk_budget_mb), "--mem-budget-mb", str(policy.mem_budget_mb)]


def sweep_argv() -> list[str]:
    """Kills every bh-builder process (prints {"status": "swept"}). Used by the start-up kill path."""
    return [DISPATCH, "--sweep-only"]


def cancel_argv() -> list[str]:
    """Cancels a running job whose dispatcher child handle was lost; the job uuid goes on stdin."""
    return [DISPATCH, "--cancel"]


def sudo_rule_ok(policy: Policy, run: Run = run_cmd) -> bool:
    """Ask sudo, without a password and without running anything, whether the pinned rule admits exactly the
    argv the helper will use (`sudo -n -l <cmd>` exits 0 only if it would be allowed). Broader sudo rights
    also pass this; `sudo_rule_file` and `bh setup` check for our own rule."""
    rc, _ = run([SUDO, "-n", "-l", *dispatch_argv(policy)])
    return rc == 0


SUDOERS_PATH = "/etc/sudoers.d/borrowed-hands"


def rule_argvs(policy: Policy) -> list[list[str]]:
    """The six exact dispatcher argv the pinned sudoers rule allows, in file order."""
    return [dispatch_argv(replace(policy, build_timeout_s=t)) for t in TIMEOUT_TIERS] + [sweep_argv(), cancel_argv()]


def rule_commands(policy: Policy) -> list[str]:
    """The six exact command lines of the pinned sudoers rule, in file order."""
    return [" ".join(a) for a in rule_argvs(policy)]


def sudo_rule_file(lstat: Callable = os.lstat) -> tuple[bool, str]:
    """(a), the part a non-root user can see: our rule file is a regular file (not a symlink) owned root:wheel
    with mode 0440. Its bytes are root-only; `bh setup` compares them with sudo before trusting the file."""
    try:
        st = lstat(SUDOERS_PATH)
    except FileNotFoundError:
        return False, f"{SUDOERS_PATH} is missing"
    except OSError as e:
        return False, f"could not check {SUDOERS_PATH} ({e.strerror or e})"
    if not stat.S_ISREG(st.st_mode):
        return False, f"{SUDOERS_PATH} is not a regular file"
    if (st.st_uid, st.st_gid) != (0, 0):
        return False, f"{SUDOERS_PATH} is not owned by root:wheel"
    if stat.S_IMODE(st.st_mode) != 0o440:
        return False, f"{SUDOERS_PATH} does not have mode 0440"
    return True, ""


def sudo_rule_permitted(policy: Policy, run: Run = run_cmd) -> Optional[str]:
    """(b): sudo itself, applying every rule on this Mac in order (later rules, negations, directory specs,
    argument regexes), says each exact dispatcher argv is permitted: `sudo -n -l -- <argv>` exits 0 only if that
    command is permitted (man sudo, EXIT VALUE). Returns None when all six are, else the first one that is not.
    Permitted is not password-free: a later rule that makes a command need a password still passes here, and only
    shows when a build runs (the executor then names it). No password is asked (-n); anything but 0, including
    sudo failing to run, counts as not permitted."""
    for argv in rule_argvs(policy):
        rc, _ = run([SUDO, "-n", "-l", "--", *argv])
        if rc != 0:
            return " ".join(argv)
    return None


_XCODE_RE = re.compile(r"Xcode ([0-9]+(?:\.[0-9]+){0,2})\nBuild version ([0-9A-Za-z]{1,16})\n?\Z")

# The build runner (sandbox/bh-runner DEVELOPER_DIR) always uses this exact copy, whatever xcode-select says.
# The probe checks the same copy, so a passing setup cannot be followed by a build on a different Xcode.
RUNNER_XCODE_APP = "/Applications/Xcode.app"
RUNNER_DEVELOPER_DIR = RUNNER_XCODE_APP + "/Contents/Developer"
RUNNER_XCODEBUILD_SUDO = (f"sudo /usr/bin/env DEVELOPER_DIR={RUNNER_DEVELOPER_DIR} "
                          f"{RUNNER_DEVELOPER_DIR}/usr/bin/xcodebuild")      # fixes target the runner's copy


@dataclass(frozen=True)
class XcodeStatus:
    version: Optional[tuple[str, str]]     # (version, build) when usable, else None
    problem: str = ""                      # what is wrong, empty when usable
    fix: str = ""                          # the exact next step, empty when usable
    note: str = ""                         # informational only (a different global selection), never a failure


def _runner_xcodebuild(*args: str) -> list[str]:
    return ["/usr/bin/env", f"DEVELOPER_DIR={RUNNER_DEVELOPER_DIR}", f"{RUNNER_DEVELOPER_DIR}/usr/bin/xcodebuild",
            *args]


def _app_of(developer_dir: str) -> str:
    return developer_dir[:-len("/Contents/Developer")] if developer_dir.endswith("/Contents/Developer") \
        else developer_dir


def xcode_status(run: Run = run_cmd) -> XcodeStatus:
    """Usable means the runner's own copy exists and reports its version (license accepted, first launch done),
    run exactly as the runner runs it (absolute path, explicit DEVELOPER_DIR). The global xcode-select choice does
    not affect builds, so a different selection is only a note. Each failure names the one command that fixes it,
    pinned to the runner's copy rather than to whatever is selected."""
    rc, out = run(["/usr/bin/xcode-select", "-p"])
    selected = out.strip().rstrip("/") if rc == 0 else ""
    is_selected = bool(selected) and os.path.realpath(selected) == os.path.realpath(RUNNER_DEVELOPER_DIR)
    rc, out = run(_runner_xcodebuild("-version"))
    if rc in (126, 127):
        if selected.endswith(".app/Contents/Developer") and not is_selected:
            return XcodeStatus(None, f"builds use {RUNNER_XCODE_APP}, but the selected Xcode is "
                                     f"{_app_of(selected)} and there is no Xcode at {RUNNER_XCODE_APP}",
                               f"install Xcode at {RUNNER_XCODE_APP} (or rename your copy to that path) and open "
                               f"it once")
        return XcodeStatus(None, f"Xcode is not installed at {RUNNER_XCODE_APP} (builds use that exact copy)",
                           "install Xcode from the App Store and open it once")
    m = _XCODE_RE.match(out.strip() + "\n") if rc == 0 else None
    if m is None:
        lrc, _ = run(_runner_xcodebuild("-license", "check"))
        if lrc != 0:
            return XcodeStatus(None, f"the license of the Xcode at {RUNNER_XCODE_APP} has not been accepted",
                               f"run '{RUNNER_XCODEBUILD_SUDO} -license accept'")
        return XcodeStatus(None, f"Xcode at {RUNNER_XCODE_APP} did not report its version",
                           f"open {RUNNER_XCODE_APP} once to finish installing its components (or run "
                           f"'{RUNNER_XCODEBUILD_SUDO} -runFirstLaunch'), then try again")
    note = "" if is_selected else (f"builds use {RUNNER_XCODE_APP}; xcode-select points at "
                                   f"{_app_of(selected) if selected else 'nothing'}, which builds do not use")
    return XcodeStatus((m.group(1), m.group(2)), note=note)


def xcode_info(run: Run = run_cmd) -> Optional[tuple[str, str]]:
    return xcode_status(run).version


def host(run: Run = run_cmd) -> Host:
    rc, out = run(["/usr/bin/sw_vers", "-productVersion"])
    ver = out.strip() if rc == 0 and re.fullmatch(r"[0-9]+(\.[0-9]+){0,3}", out.strip()) else "0"
    arch = platform.machine()
    return Host(ver, arch if arch in ("arm64", "x86_64") else "arm64")


def probe(policy: Policy, run: Run = run_cmd, screen_granted: Callable[[], bool] = lambda: False) -> Probes:
    return Probes(install=production_install(), sudo_ok=sudo_rule_ok(policy, run), xcode=xcode_info(run),
                  screen_granted=screen_granted())


def usable(cfg: Config, p: Probes) -> frozenset:
    out = set()
    c = cfg.capabilities
    if c.system_info:
        out.add("system.info")
    if c.xcode_build and p.install.ok and p.sudo_ok and p.xcode is not None:
        out.add("xcode.build")
    if c.screen_capture and p.screen_granted:
        out.add("screen.capture")
    return frozenset(out)


def caps_body(cfg: Config, p: Probes, *, busy: bool, queue_depth: int, host: Host) -> dict:
    ok = usable(cfg, p)
    capabilities = []
    if "system.info" in ok:
        capabilities.append({"name": "system.info", "v": 1})
    if "screen.capture" in ok:
        capabilities.append({"name": "screen.capture", "v": 1, "displays": max(1, min(16, p.displays))})
    if "xcode.build" in ok:
        capabilities.append({
            "name": "xcode.build", "v": 1, "xcode": p.xcode[0], "xcode_build": p.xcode[1],
            "workspaces": [{"workspace_id": w.id, "repo_fingerprint": w.fingerprint, "schemes": list(w.schemes),
                            "configurations": list(w.configurations),
                            "destinations": [d for d in w.destinations if d in DESTINATIONS],
                            "expires_at": w.expires_at} for w in cfg.workspaces][:32]})
    return {"helper_version": __version__, "name": cfg.device_name, "os": "macos", "os_version": host.os_version,
            "arch": host.arch, "busy": bool(busy), "queue_depth": max(0, min(100, queue_depth)),
            "capabilities": capabilities}
