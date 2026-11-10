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
    """Run one admin command interactively (sudo may ask for the password). The caller has already shown it.
    On Ctrl-C, wait for sudo instead of killing it (subprocess.run would, after 0.25 s): a sudo running its command
    is root, so the kill fails with PermissionError and hides the KeyboardInterrupt from the caller's handler. sudo
    gets the same SIGINT from the terminal and ends on its own; then the interrupt is raised again."""
    p = subprocess.Popen(["/usr/bin/sudo", *argv])
    try:
        return p.wait()
    except KeyboardInterrupt:
        p.wait()
        raise


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


SF_NOUNLINK = 0x00100000      # <sys/stat.h>; macOS sets it (SIP-enforced) on its per-user folder skeleton
# Inside a per-user folder: TCC Full Disk Access protected, no sunlnk; nobody without FDA can list them, root included
TEMP_ITEMS = ("T/TemporaryItems", "T/com.apple.trustd/TemporaryItems")


class Unverified(str):
    """A path removable_entries could not prove empty and that must never be offered to rm: a TemporaryItems folder
    at its exact place that is not provably empty (the user restarts or uses Full Disk Access instead)."""


class Uninspected(str):
    """A folder of the per-user tree this process may not list (T and C are 0700 uid 470): not inspected, so never
    "nothing removable". Only the root teardown, verified in the same run, can vouch for it."""


def on_apfs(path: str, run: Optional[Callable] = None) -> bool:
    """True if the file system holding `path` is APFS: df statfs(2)s the path and, with -T, prints it only if its type
    is in the list (else nothing and exit 1); -Y adds the type column, checked too. Same check as the teardown."""
    rc, out = (run or capsmod.run_cmd)(["/bin/df", "-P", "-Y", "-T", "apfs", path])
    rows = out.splitlines()
    return rc == 0 and len(rows) == 2 and rows[1].split()[1:2] == ["apfs"]


def temporary_items_empty(p: str, st, uid: int, listdir: Callable, on_apfs: Callable[[str], bool]) -> bool:
    """The teardown's emptiness proof for a TemporaryItems folder nobody can list (see teardown-bh-builder.sh): on
    APFS a directory's st_size is 32 * (entries + 2) and st_nlink is entries + 2. Observed APFS behavior, not a
    documented API, so only the exact shape macOS creates counts: a 0700 directory of uid:uid (the installer's group
    has the same number), no flags, size 64, link count 2, a listing refused with EPERM, on APFS."""
    import errno
    import stat
    if not (st.st_mode == stat.S_IFDIR | 0o700 and st.st_uid == uid and st.st_gid == uid and st.st_flags == 0
            and st.st_size == 64 and st.st_nlink == 2):
        return False
    try:
        listdir(p)
    except OSError as e:
        return e.errno == errno.EPERM and on_apfs(p)
    return False


def removable_entries(roots: list, uid: int, lstat: Callable = os.lstat, listdir: Callable = os.listdir,
                      on_apfs: Callable[[str], bool] = on_apfs) -> Optional[list[str]]:
    """The top-most paths under `roots` (uid's per-user folders) that are not macOS's protected skeleton, which no
    one can remove: directories owned by `uid` with SF_NOUNLINK, data vaults inside them that the kernel will not
    let anyone stat (EPERM), and a TemporaryItems at exactly its place that is provably empty. A TemporaryItems there
    that is not provably empty is returned as Unverified. [] if only that skeleton is left; None if something could
    not be inspected. lstat only: a symlink is reported, never followed. Run as the owner, folders only uid and root
    can read (T, C: 0700) cannot be looked into and are returned as Uninspected: the checklist accepts them only when
    the root teardown verified them in the same run."""
    import errno
    import stat
    found: list[str] = []

    def walk(p: str, top: bool, root: str) -> bool:
        try:
            st = lstat(p)
        except FileNotFoundError:
            return True
        except OSError as e:
            return e.errno == errno.EPERM and not top
        if not (stat.S_ISDIR(st.st_mode) and st.st_uid == uid and st.st_flags & SF_NOUNLINK):
            if p in {f"{root}/{t}" for t in TEMP_ITEMS}:
                if not temporary_items_empty(p, st, uid, listdir, on_apfs):
                    found.append(Unverified(p))
            else:
                found.append(p)
            return True
        try:
            names = listdir(p)
        except OSError as e:
            if e.errno != errno.EACCES:
                return False
            found.append(Uninspected(p))
            return True
        return all(walk(os.path.join(p, n), False, root) for n in sorted(names))
    return found if all(walk(r, True, r) for r in roots) else None


def _unsafe_to_paste(word: str) -> bool:
    """Control, format (bidi overrides, zero-width) and line/paragraph separator characters: in a pasted command a
    newline runs a second command, and the others can make a command look like something it is not."""
    import unicodedata
    return any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in word)


def paste_command(argv: list[str], sudo: bool = False) -> Optional[str]:
    """A command line for the user to copy and run, every word shell-quoted; None if any word cannot be shown
    safely (see _unsafe_to_paste). Paths in it can come from build code (uid 470's file names), never trust them."""
    if any(_unsafe_to_paste(a) for a in argv):
        return None
    return " ".join(shlex.quote(a) for a in (["sudo", *argv] if sudo else argv))


def describe_unsafe(argv: list[str]) -> str:
    """What to say instead of a command paste_command refused: the offending words escaped, never raw."""
    bad = [a for a in argv if _unsafe_to_paste(a)]
    return ("no command shown: a path in it contains control characters (" + ", ".join(ascii(a) for a in bad)
            + "); inspect it with ls -b and remove it by hand")


def shown_path(p: str) -> str:
    """A path for a note (not a command): shell-quoted so it reads as one word, escaped if it holds control
    characters. Names under a per-user folder are chosen by build code."""
    return ascii(p) if _unsafe_to_paste(p) else shlex.quote(p)


def show(argv: list[str]) -> str:
    return paste_command(argv, sudo=True) or describe_unsafe(argv)


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
    cache_removable: Callable[[list, int], Optional[list]] = lambda roots, uid: removable_entries(roots, uid)
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
