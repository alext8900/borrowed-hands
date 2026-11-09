"""Everything the CLI, `bh setup` and `bh selftest` touch outside the process, in one injectable bundle.
Tests replace these; nothing here runs sudo on its own: `sudo` always prints the command first."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import caps as capsmod
from .config import DEFAULT_DIR
from .connection import AiohttpTransport, Timing


def _say(text: str = "") -> None:
    print(text, flush=True)


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _sudo(argv: list[str]) -> int:
    """Run one admin command interactively (sudo may ask for the password). The caller has already shown it."""
    return subprocess.run(["/usr/bin/sudo", *argv]).returncode


def path_state(p: str) -> Optional[bool]:
    """True if something is at p (lstat: a symlink counts), False if it is verifiably absent, None if it could not
    be checked (permission or I/O error), which must never be reported as removed."""
    try:
        os.lstat(p)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None


def helper_lock_state(home: Path) -> Optional[bool]:
    """Uninstall's fail-closed check of the helper's lock (held by the LaunchAgent and by `bh run`): True held,
    False verifiably free or never created, None when it cannot be opened or tested. The daemon's own
    `daemon_running` treats open errors as "not running", which is right for starting a daemon but would let
    uninstall dismantle the sandbox under a live helper."""
    import fcntl
    from .daemon import LOCK_NAME
    try:
        fd = os.open(Path(home) / LOCK_NAME, os.O_RDONLY | os.O_NOFOLLOW)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return None
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def uid_folders(uid: int, root: str = "/private/var/folders") -> Optional[list[str]]:
    """The per-user temp/cache directories of `uid` (depth two under /private/var/folders, as the teardown removes
    them), or None if they cannot all be inspected. Readable without root: those levels are root-owned 0755."""
    found = []
    try:
        with os.scandir(root) as top:
            for a in top:
                if not a.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(a.path) as sub:
                    for b in sub:
                        if b.stat(follow_symlinks=False).st_uid == uid:
                            found.append(b.path)
    except OSError:
        return None
    return sorted(found)


def show(argv: list[str]) -> str:
    return "sudo " + " ".join(shlex.quote(a) for a in argv)


@dataclass
class Env:
    home: Path = DEFAULT_DIR
    say: Callable[[str], None] = _say
    err: Callable[[str], None] = _err
    ask: Callable[[str], str] = _ask
    sudo: Callable[[list], int] = _sudo
    run: capsmod.Run = capsmod.run_cmd
    install_status: Callable[[], capsmod.InstallStatus] = capsmod.production_install
    sudo_rule_ok: Callable[[object], bool] = lambda policy: capsmod.sudo_rule_ok(policy)
    sudo_rule_file: Callable[[], tuple] = lambda: capsmod.sudo_rule_file()                 # (a) metadata
    sudo_rule_permitted: Callable[[object], Optional[str]] = lambda policy: capsmod.sudo_rule_permitted(policy)
    xcode: Callable[[], Optional[tuple]] = lambda: capsmod.xcode_info()
    xcode_status: Callable[[], capsmod.XcodeStatus] = lambda: capsmod.xcode_status()   # why Xcode is unusable
    screen_granted: Callable[[], bool] = lambda: False
    host: Callable[[], capsmod.Host] = lambda: capsmod.host()
    connect: Callable = AiohttpTransport.connect
    timing: Timing = field(default_factory=Timing)
    executors: Optional[dict] = None
    uid: int = field(default_factory=os.getuid)
    service: Optional[object] = None          # service module stand-in (tests)
    pair_insecure_local: bool = False         # tests only: lets `bh setup` pair with a ws://127.0.0.1 server
    user_home: Path = field(default_factory=Path.home)                  # where the LaunchAgent plist lives
    exists: Callable[[str], Optional[bool]] = lambda p: path_state(p)   # uninstall checks; None = cannot tell
    helper_running: Callable[[Path], Optional[bool]] = lambda home: helper_lock_state(home)   # None: cannot tell
    cache_dirs: Callable[[int], Optional[list]] = lambda uid: uid_folders(uid)       # uninstall's cache check
    stop_wait_s: float = 130.0                # the LaunchAgent's ExitTimeOut (120 s to drain a build) plus margin

    def probes(self, policy) -> capsmod.Probes:
        return capsmod.Probes(install=self.install_status(), sudo_ok=self.sudo_rule_ok(policy), xcode=self.xcode(),
                              screen_granted=self.screen_granted())

    def get_executors(self) -> dict:
        if self.executors is None:
            from .executors import load_executors
            self.executors = load_executors()
        return self.executors

    def svc(self):
        if self.service is None:
            from . import service
            self.service = service
        return self.service
