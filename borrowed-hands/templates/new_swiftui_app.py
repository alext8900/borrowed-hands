#!/usr/bin/env python3
"""Start a new SwiftUI app that Borrowed Hands can build, without Xcode. Python 3 standard library only.

    python3 new_swiftui_app.py --name HelloApp --bundle-id com.example.helloapp \
        [--repo https://github.com/OWNER/REPO.git] DEST

Copies the template next to this file (`swiftui-app/`) into DEST, usually the root of a fresh clone: one
multiplatform SwiftUI app target and a shared scheme, both named after --name, for macOS (arm64) and the iOS
Simulator. No signing team, Swift packages, Run Script phases or asset catalog. The app folder is a
file-system-synchronized group, so a new .swift file in DEST/<name>/ is compiled with no project file edit.

The output depends only on the arguments: same arguments, same bytes. Object IDs in the project file are fixed; they
only need to be unique within one project. Nothing is overwritten: if any file it would write exists, it writes
nothing and exits 1. If anything fails after the run has created something, it stops, deletes nothing, exits 1 and
prints JSON with written (completed files), incomplete (created but not completed) and created_dirs (absolute at or
above DEST, relative inside it). These are a history of what this run created by name at the time, not proof of what
is at those paths now, and must not be used as authority to delete anything. Reports (success or stop) are delivered
when stdout is usable; if it is not, the exit code is still correct (a success that could not be reported exits 1)
and nothing is deleted. DEST and every directory it creates or enters must be real directories, not symlinks: a
symlink in DEST's path is refused (pass the resolved path), after os.path.abspath folds `..` away lexically (so
`link/..` is never checked), and all writes go through directory descriptors opened without following symlinks, so a
path swapped mid-run cannot redirect them. Invalid arguments exit 2.

On success it prints JSON: the project and scheme to build, the files written, and (with --repo) the exact
`bh grant add` command the Mac's owner runs, under a workspace name derived from the repository, with a note for
the owner: `bh grant add` never changes an existing grant, and `--replace` replaces one on purpose. Only the
owner can grant; this script grants nothing. The printed --project is relative to the repository, so the command is
right only when DEST is the repository root.

Known limits (exotic streams): if stderr is a full pipe that nobody reads, the diagnostic blocks after the report has
been delivered; on Python versions whose argparse writes to an absent stream (3.9 here, with fd 2 or fds 1 and 2
closed before start), usage errors and --help exit 1 instead of 2 or 0.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "swiftui-app"
NAME_TOKEN = "__APP_NAME__"
BUNDLE_TOKEN = "__BUNDLE_ID__"
RENAMES = {"gitignore": ".gitignore"}
# Exactly what the template ships. Anything missing, extra or unreadable is refused before DEST is touched.
INVENTORY = frozenset({
    "gitignore",
    "__APP_NAME__.xcodeproj/project.pbxproj",
    "__APP_NAME__.xcodeproj/xcshareddata/xcschemes/__APP_NAME__.xcscheme",
    "__APP_NAME__/__APP_NAME__App.swift",
    "__APP_NAME__/ContentView.swift",
})
PLACEHOLDER = re.compile(r"__[A-Z][A-Z_]*__")
DESTINATIONS = ("platform=macOS,arch=arm64", "generic/platform=iOS Simulator")

# UpperCamelCase ASCII letters and digits: a valid Swift type and module name, Xcode target, scheme and grant name,
# and a file name with nothing a shell or a path could misread.
NAME_RE = re.compile(r"[A-Z][A-Za-z0-9]{1,49}\Z")
# Names that would shadow a module or type the app uses (checked without case: APFS ignores case).
RESERVED = {"app", "contentview", "swift", "swiftui", "foundation", "combine", "observation", "uikit", "appkit",
            "darwin", "dispatch", "corefoundation", "objectivec", "xctest", "testing", "main", "view", "scene"}
LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})"
BUNDLE_RE = re.compile(rf"{LABEL}(?:\.{LABEL})+\Z")
# A public https repository URL with no credentials, port, query or fragment.
REPO_RE = re.compile(r"https://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})(?:/[A-Za-z0-9_.~-]+){2,8}\Z")


HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def public_host(host: str) -> bool:
    """The server's host rule (borrowed-hands/refs.py ascii_host), for the ASCII hosts REPO_RE admits: two or more
    DNS labels, no empty label (so no trailing dot), and a top-level label with no digit unless it is an A-label.
    Refuses IP addresses, single-label names such as localhost, and hosts like a- or github.com."""
    host = host.lower()
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or not all(HOST_LABEL.match(label) for label in labels):
        return False
    if any(c.isdigit() for c in labels[-1]) and not labels[-1].startswith("xn--"):
        return False
    for label in labels:
        if label.startswith("xn--"):
            try:
                label.encode("ascii").decode("idna")
            except (UnicodeError, ValueError):
                return False
    return True


class Refused(ValueError):
    """An argument is not safe or not valid."""


class History:
    """What this run created, by name, at the moment it created it: completed files, files created but not
    completed, and directories (absolute paths at or above DEST, relative paths inside it). A history, not proof of
    what is at those paths now: another actor may have moved or replaced them since."""

    def __init__(self):
        self.written, self.incomplete, self.created_dirs = [], [], []

    def any(self) -> bool:
        return bool(self.written or self.incomplete or self.created_dirs)


class Stopped(Exception):
    """A failure after this run created something. The run stopped, deleted nothing, and reports its History."""

    def __init__(self, at: str, reason: str, history: History):
        super().__init__(at)
        self.at, self.reason, self.history = at, reason, history


def check_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise Refused(f"app name {name!r} must start with an uppercase letter, then 1 to 49 ASCII letters or "
                      "digits (for example HelloApp)")
    if name.lower() in RESERVED:
        raise Refused(f"app name {name!r} would shadow a Swift module or type; pick another")
    return name


def check_bundle_id(bundle: str) -> str:
    if not isinstance(bundle, str) or len(bundle) > 155 or not BUNDLE_RE.match(bundle):
        raise Refused(f"bundle id {bundle!r} must be reverse-DNS: two or more dot-separated parts of ASCII letters, "
                      "digits and hyphens (for example com.example.helloapp)")
    return bundle


def check_repo(repo: str) -> str:
    if (not isinstance(repo, str) or not REPO_RE.match(repo) or ".." in repo
            or not public_host(repo[len("https://"):].partition("/")[0])):
        raise Refused(f"repo {repo!r} must be a public https URL such as https://github.com/OWNER/REPO.git: a DNS "
                      "name (not an IP address or a local name), no credentials, port, query or fragment")
    return repo


def render(name: str, bundle: str) -> list[tuple[str, bytes]]:
    """Every output file as (path relative to DEST, bytes), sorted by path."""
    if TEMPLATE.is_symlink() or not TEMPLATE.is_dir():
        raise RuntimeError(f"template missing: {TEMPLATE}")
    def unreadable(err: OSError):
        raise RuntimeError(f"template unreadable: {err.filename}: {err.strerror}")

    out, found = [], set()
    for dirpath, dirnames, filenames in os.walk(TEMPLATE, onerror=unreadable):
        dirnames.sort()
        for fname in sorted(filenames):
            src = Path(dirpath) / fname
            if src.is_symlink() or not src.is_file():
                raise RuntimeError(f"template entry is not a regular file: {src}")
            rel = src.relative_to(TEMPLATE).as_posix()
            found.add(rel)
            rel = RENAMES.get(rel, rel).replace(NAME_TOKEN, name)
            text = src.read_text(encoding="utf-8").replace(NAME_TOKEN, name).replace(BUNDLE_TOKEN, bundle)
            left = PLACEHOLDER.search(rel) or PLACEHOLDER.search(text)
            if left:
                raise RuntimeError(f"placeholder {left.group(0)} left in {rel}")
            out.append((rel, text.encode("utf-8")))
    if found != INVENTORY:
        raise RuntimeError(f"template incomplete or altered: missing {sorted(INVENTORY - found)}, "
                           f"unexpected {sorted(found - INVENTORY)}")
    return sorted(out)


def workspace_for(repo: str, project: str) -> str:
    """A grant workspace name for this repository and project: `bh grant add` refuses a name that already exists
    (unless the owner passes --replace), so a second app in the same repository must not get the first one's name.
    <owner>-<repo>-<app>, sanitized to the helper's rule ([a-z0-9][a-z0-9_-]{0,63}), plus 6 hex of a hash of the
    normalized URL and the project, so names that sanitize or truncate alike still differ. The URL is normalized
    like the helper's repo fingerprint: host lowercased, path case kept, one trailing / and then .git dropped
    (protocol/bhproto.py normalize_repo_url). This makes accidental reuse unlikely; it does not stop a chosen
    collision; the helper's refusal to replace a grant without --replace is the control."""
    host, _, path = repo[len("https://"):].partition("/")
    path = "/" + path
    path = path[:-1] if path.endswith("/") else path
    path = path[:-4] if path.endswith(".git") else path
    key = f"https://{host.lower()}{path}"
    parts = path.lower().split("/")[1:][-2:] + [project.lower().removesuffix(".xcodeproj")]
    base = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9_-]", "-", "-".join(parts))).strip("-_")[:57].rstrip("-_")
    digest = hashlib.sha256(f"{key}\0{project}".encode()).hexdigest()[:6]
    return f"{base or 'app'}-{digest}"


def grant_note(workspace: str | None) -> str:
    if workspace is None:
        return ("To get the owner's bh grant add command, run again with the same --name and --bundle-id plus "
                "--repo <public https URL> in a new empty directory; nothing in this one needs to change.")
    return (f"For the Mac's owner: bh grant add never changes an existing grant. If {workspace} is already "
            "listed (bh grant list), the command changes nothing and prints what that grant allows; add --replace "
            "only to replace it on purpose (it prints what changes). Another --workspace name adds a second, "
            "separate grant with only the settings in the command: pass --confirm and --expires if that one should "
            "have them too. --project is relative to the repository, so the command is right only when DEST is the "
            "repository root. Only the owner can grant.")


# Tests set this to observe or interleave with the writer: _hook(stage, rel) at "checked" (all checks done,
# nothing written yet), "before", "created" and "wrote" (around and just after creating each file), "mkdir" (just
# after creating a directory) and "open" (a directory passed the symlink check and is about to be opened).
_hook = None
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _step(stage: str, rel: str = "") -> None:
    if _hook is not None:
        _hook(stage, rel)


def _lstat(name: str, dir_fd: int):
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_dir(name: str, dir_fd: int, shown: str, key: str) -> int:
    """Open an existing directory entry without following a symlink, even one swapped in after a check."""
    st = _lstat(name, dir_fd)
    if st is not None and stat.S_ISLNK(st.st_mode):
        raise Refused(f"{shown} is a symlink; use a real directory")
    _step("open", key)
    try:
        return os.open(name, DIR_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        raise Refused(f"{shown} is not a directory (or is a symlink): {e.strerror}") from None


def _close_all(fds) -> OSError | None:
    """Close each descriptor once (a failed close is never retried) and return the first error, if any."""
    first = None
    for fd in fds:
        try:
            os.close(fd)
        except OSError as e:
            first = first or e
    return first


def _open_dest(dest: str, history: History) -> int:
    """DEST as a directory descriptor, walked from / one component at a time: a symlink in the abspath-normalized
    path is refused (pass the resolved path; `..` is folded away lexically first), and missing trailing components
    are created. Everything later is created relative to this descriptor, so renaming or relinking DEST afterwards
    cannot redirect a write."""
    path = os.path.abspath(dest)
    fd = os.open("/", DIR_FLAGS)
    shown = ""
    try:
        for part in [c for c in path.split("/") if c]:
            shown += "/" + part
            if _lstat(part, fd) is None:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)              # umask applies; never chmodded later
                    history.created_dirs.append(shown)
                    _step("mkdir", shown)
                except FileExistsError:
                    pass                                          # raced; the no-follow open below decides
            nxt = _open_dir(part, fd, f"destination path component {shown!r}", shown)
            prev, fd = fd, nxt
            err = _close_all([prev])
            if err is not None:
                raise err
        return fd
    except BaseException:
        _close_all([fd])
        raise


def _preflight(root: int, files: list[tuple[str, bytes]]) -> None:
    """Refuse before writing anything: an existing target, or an output directory that is a symlink or a file."""
    for rel, _ in files:
        *dirs, fname = rel.split("/")
        fd, opened = root, []
        try:
            for i, d in enumerate(dirs):
                st = _lstat(d, fd)
                if st is None:
                    fd = None
                    break
                fd = _open_dir(d, fd, "/".join(dirs[:i + 1]), "/".join(dirs[:i + 1]))
                opened.append(fd)
            if fd is not None and _lstat(fname, fd) is not None:
                raise FileExistsError(f"{rel} already exists in the destination; nothing was written")
        except BaseException:
            _close_all(opened)
            raise
        err = _close_all(opened)
        if err is not None:
            raise err


def write(dest: Path, files: list[tuple[str, bytes]], history: History) -> None:
    """Create every file, then release every directory descriptor once, and return or raise exactly one outcome.
    Once this run has created anything, any Exception (including a descriptor close failure after otherwise
    successful writing) becomes one Stopped carrying the history; a close failure never replaces an earlier
    failure. Before anything is created, a failure propagates unchanged. Nothing is ever deleted."""
    dirs = {}                                                     # relative dir -> held descriptor
    at = str(dest)

    def dir_fd(rel_dir: str) -> int:
        if rel_dir not in dirs:
            parent, _, name = rel_dir.rpartition("/")
            pfd = dir_fd(parent)
            try:
                # umask applies. Never chmod it afterwards: the name may hold someone else's directory by then.
                os.mkdir(name, 0o755, dir_fd=pfd)
                history.created_dirs.append(rel_dir)
                _step("mkdir", rel_dir)
            except FileExistsError:
                pass
            dirs[rel_dir] = _open_dir(name, pfd, rel_dir, rel_dir)
        return dirs[rel_dir]

    try:
        dirs[""] = _open_dest(str(dest), history)
        _preflight(dirs[""], files)
        _step("checked")
        for rel, data in files:
            at = rel
            parent, _, fname = rel.rpartition("/")
            pfd = dir_fd(parent)
            _step("before", rel)
            fd = os.open(fname, FILE_FLAGS, 0o644, dir_fd=pfd)
            # This descriptor is ours from here until its single close. A failure in between closes it once and
            # stays the reported failure; a close error after a complete write is a failure of its own.
            try:
                history.incomplete.append(rel)
                _step("created", rel)
                os.fchmod(fd, 0o644)                              # on this descriptor: no umask, no name lookup
                view = memoryview(data)
                while view:
                    n = os.write(fd, view)
                    if n <= 0:
                        raise OSError("write made no progress")
                    view = view[n:]
            except BaseException:
                _close_all([fd])
                raise
            err = _close_all([fd])
            if err is not None:
                raise err
            history.incomplete.remove(rel)
            history.written.append(rel)
            _step("wrote", rel)
    except Exception as e:
        failure = e
    else:
        failure = None
    finally:
        released = _close_all(dirs.values())                     # also runs while a BaseException unwinds
    if failure is None and released is not None:
        failure, at = released, "closing the output directories"
    if failure is None:
        return
    if not history.any():
        raise failure
    raise Stopped(at, _reason(failure), history) from None


def _reason(e: Exception) -> str:
    if isinstance(e, FileExistsError):
        return "appeared during the run"
    if isinstance(e, OSError):
        return e.strerror or str(e)
    if isinstance(e, Refused):
        return str(e)
    return f"{type(e).__name__}: {e}"


def _silence(stream, fd: int) -> None:
    """After a failed write or flush, point the fd at /dev/null so the interpreter's exit flush cannot fail
    (no 'Exception ignored', no exit status 120)."""
    try:
        null = os.open(os.devnull, os.O_WRONLY)
        if null != fd:                                            # a closed fd is reused by the open itself
            try:
                os.dup2(null, fd)
            finally:
                os.close(null)
    except OSError:
        pass
    try:
        stream.flush()
    except (OSError, ValueError):
        pass


def _emit(obj: dict) -> bool:
    """The report, to stdout, flushed. False if stdout cannot take it, or is absent (None when fd 1 was closed
    before Python started; its fd number may now be one of ours, so it is left alone)."""
    if sys.stdout is None:
        return False
    try:
        sys.stdout.write(json.dumps(obj, indent=2) + "\n")
        sys.stdout.flush()
        return True
    except (OSError, ValueError):
        _silence(sys.stdout, 1)
        return False


def _say(msg: str) -> None:
    """A diagnostic on stderr, best effort: it never replaces the report. Absent stderr: nothing to do."""
    if sys.stderr is None:
        return
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        _silence(sys.stderr, 2)


class _NoColor(argparse.HelpFormatter):
    """Python 3.14's formatter probes stdout for colour while the parser is built, and a closed stdout makes that
    raise. Never colour; older Pythons never call this."""

    def _set_color(self, color, *args, **kwargs):                # private; its signature varies by version
        super()._set_color(False, *args, **kwargs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create a new SwiftUI app that Borrowed Hands can build.",
                                 formatter_class=_NoColor)
    ap.add_argument("--name", required=True, help="app, target and scheme name, UpperCamelCase (HelloApp)")
    ap.add_argument("--bundle-id", required=True, help="reverse-DNS bundle identifier (com.example.helloapp)")
    ap.add_argument("--repo", help="public https URL of the repository, to print the owner's grant command")
    ap.add_argument("dest", help="directory to create the project in, usually the repository root")
    args = ap.parse_args(argv)
    history = History()
    try:
        name, bundle = check_name(args.name), check_bundle_id(args.bundle_id)
        repo = check_repo(args.repo) if args.repo is not None else None
        files = render(name, bundle)
        write(Path(args.dest), files, history)
        workspace = workspace_for(repo, f"{name}.xcodeproj") if repo is not None else None
        result = {"project": f"{name}.xcodeproj", "scheme": name, "configuration": "Debug",
                  "destinations": list(DESTINATIONS), "workspace": workspace, "grant_note": grant_note(workspace),
                  "files": [rel for rel, _ in files]}
        if repo is not None:
            result["grant_command"] = (
                f"bh grant add xcode.build --workspace {workspace} --repo {repo} --project {name}.xcodeproj "
                f"--scheme {name} --configuration Debug "
                + " ".join(f"--destination '{d}'" for d in DESTINATIONS))
    except Stopped as e:
        return _stop(e.at, e.reason, history)
    except Exception as e:
        if history.any():                 # once this run created anything, every failure is a stop with a report
            return _stop("", _reason(e), history)
        if isinstance(e, Refused):
            _say(f"refused: {e}")
            return 2
        _say(f"refused: {e}" if isinstance(e, FileExistsError) else f"error: {_reason(e)}")
        return 1
    # success goes through the same delivery: a report that never arrived is not a success
    return 0 if _emit(result) else 1


def _stop(at: str, reason: str, history: History) -> int:
    """Report first (stdout, flushed), then the diagnostic; exit 1 either way."""
    _emit({"error": "stopped", "conflict": at, "reason": reason, "written": history.written,
           "incomplete": history.incomplete, "created_dirs": history.created_dirs})
    _say(f"stopped at {at}: {reason}; nothing was deleted. What this run created is listed on stdout (a history, "
         "not proof of what is at those paths now; never delete by it).")
    return 1

if __name__ == "__main__":
    sys.exit(main())
