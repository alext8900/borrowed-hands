"""`bh service install|uninstall`: the helper as a LaunchAgent in the owner's gui domain (SPEC 8.4).
Touches only the label dev.borrowed-hands.helper."""
from __future__ import annotations

import os
import plistlib
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from . import caps as capsmod

LABEL = "dev.borrowed-hands.helper"
LAUNCHCTL = "/bin/launchctl"
# How long uninstall waits for launchd to finish a bootout. launchd SIGTERMs the helper, which drains or cancels a
# running build (its own stop deadline is 90 s), and SIGKILLs it at ExitTimeOut (120 s, below); so the job is gone
# by then plus launchd's own teardown. Same bound as the CLI's helper-lock fence (Env.stop_wait_s).
STOP_WAIT_S = 130.0
STOP_POLL_S = 0.25


def plist_path(user_home: Path) -> Path:
    return Path(user_home) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_dir(user_home: Path) -> Path:
    return Path(user_home) / "Library" / "Logs" / "BorrowedHands"


def program() -> list[str]:
    """The interpreter of the environment this package is installed in (the uv tool venv), isolated (-I: no
    PYTHON* env, no user site). Never realpath it: that resolves out of the venv to the bare base interpreter,
    which cannot import borrowed_hands_helper."""
    exe = os.path.abspath(sys.executable)
    if not exe.startswith(sys.prefix + os.sep):
        exe = os.path.join(sys.prefix, "bin", "python")
    return [exe, "-I", "-m", "borrowed_hands_helper", "run"]


def plist_bytes(user_home: Path, argv: Optional[list] = None) -> bytes:
    logs = log_dir(user_home)
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": argv or program(),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},     # restart on a crash, not after a clean stop or SIGTERM
        "ThrottleInterval": 10,
        "ExitTimeOut": 120,                         # time to cancel a running build cleanly on stop
        "ProcessType": "Interactive",               # it may show the per-build confirm dialog
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": str(logs / "helper.log"),
        "StandardErrorPath": str(logs / "helper.log"),
    })


def installed(user_home: Path = Path.home()) -> bool:
    return plist_path(user_home).exists()


def install(user_home: Path = Path.home(), uid: Optional[int] = None, run: capsmod.Run = capsmod.run_cmd,
            argv: Optional[list] = None) -> tuple[bool, str]:
    uid = os.getuid() if uid is None else uid
    p = plist_path(user_home)
    p.parent.mkdir(parents=True, exist_ok=True)
    log_dir(user_home).mkdir(parents=True, exist_ok=True, mode=0o700)
    run([LAUNCHCTL, "bootout", f"gui/{uid}/{LABEL}"])            # a previous copy, if any
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(plist_bytes(user_home, argv))
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)
    rc, out = run([LAUNCHCTL, "bootstrap", f"gui/{uid}", str(p)])
    if rc != 0:
        return False, f"launchctl bootstrap failed ({rc}): {out.strip()[:200]}"
    return True, f"the helper now runs in the background and at login (logs: {log_dir(user_home)}/helper.log)"


def uninstall(user_home: Path = Path.home(), uid: Optional[int] = None,
              run: capsmod.Run = capsmod.run_cmd, clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep,
              say: Optional[Callable[[str], None]] = None) -> tuple[bool, str]:
    uid = os.getuid() if uid is None else uid
    target = f"gui/{uid}/{LABEL}"
    run([LAUNCHCTL, "bootout", target])
    # bootout's own exit code cannot tell "not loaded" from "failed", and it can return while launchd is still
    # stopping the job (seen on a real Mac): poll until the job is verifiably gone, up to the stop bound.
    start = clock()
    waiting = False
    while True:
        state = loaded(uid, run)
        if state is False or clock() - start >= STOP_WAIT_S:
            break
        if not waiting and clock() - start >= 1.0 and say is not None:
            say(f"  waiting for the background helper to stop (up to {int(STOP_WAIT_S)} s) ...")
            waiting = True
        sleep(STOP_POLL_S)
    if state is None:
        return False, (f"could not confirm the background helper stopped; check with: launchctl print {target} "
                       f"(exit 113 means it is gone), or stop it with: launchctl bootout {target}")
    if state:
        return False, f"the background helper is still loaded; stop it with: launchctl bootout {target}"
    # Only now that the job is gone: until then the plist stays, so a failed stop leaves the install whole (a login
    # restarts the same helper) and a retry boots it out by label again.
    p = plist_path(user_home)
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass
    except OSError as e:
        from .env import describe_unsafe, paste_command
        return False, (f"could not delete {p} ({e.strerror}); remove it with: "
                       f"{paste_command(['rm', str(p)]) or describe_unsafe(['rm', str(p)])}")
    return True, "the background helper is stopped and removed"


LAUNCHCTL_NOT_FOUND = 113      # launchctl print: "Could not find service ... in domain"


def launchd_state(rc: int) -> Optional[bool]:
    """`launchctl print` exit code -> loaded (True), verifiably not loaded (False), or unknown (None: timeouts,
    a missing launchctl and other errors prove nothing)."""
    return True if rc == 0 else False if rc == LAUNCHCTL_NOT_FOUND else None


def loaded(uid: int, run: capsmod.Run = capsmod.run_cmd) -> Optional[bool]:
    rc, _ = run([LAUNCHCTL, "print", f"gui/{uid}/{LABEL}"])
    return launchd_state(rc)
