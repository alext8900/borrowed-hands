"""Safe materialization of an untrusted git commit.

Port of the hardened materialize script from the feasibility probes. Nothing in the fetched repo
can configure git: no hooks, no repo config, no filters, no submodules, no fsmonitor.
All git calls are argv lists (never a shell) with a scrubbed environment and a timeout.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_SHA_RE = re.compile(r"[0-9a-f]{40}")
# Only branches under refs/heads/bh/ may vouch for a commit; no globs, spaces, colons or "..".
_PREFIX_RE = re.compile(r"refs/heads/bh/(?:[A-Za-z0-9._-]+/)*")
DEFAULT_TIMEOUT = 120.0


class MaterializeError(Exception):
    """Base class for all materialization failures."""


class InvalidCommit(MaterializeError): ...
class ProtocolNotAllowed(MaterializeError): ...
class CommitNotFound(MaterializeError): ...
class NotReachableFromAllowedRefs(MaterializeError): ...
class TreeMismatch(MaterializeError): ...
class GitTimeout(MaterializeError): ...
class GitCommandFailed(MaterializeError): ...
class UnsafeSymlink(MaterializeError): ...
class UnsafeTreeEntry(MaterializeError): ...


@dataclass(frozen=True)
class MaterializeResult:
    commit: str
    tree: str
    refs: list[str]  # allowed refs (as on the remote, refs/heads/bh/...) containing the commit
    dest: Path
    mirror: Path  # bare repo backing the worktree; keep it as long as dest is used


def _validate_url(remote_url: str, allow_file_protocol: bool) -> str:
    """Returns the git protocol name to allow ('https' or 'file')."""
    if not isinstance(remote_url, str) or remote_url != remote_url.strip() or remote_url.startswith("-"):
        raise ProtocolNotAllowed("malformed remote url")
    parts = urlsplit(remote_url)
    if parts.scheme == "https" and parts.netloc:
        return "https"
    if parts.scheme == "file" and allow_file_protocol and parts.path.startswith("/"):
        return "file"
    raise ProtocolNotAllowed(f"remote scheme not allowed: {parts.scheme or 'none'}")


def _env(protocol: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/dev/null",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_ALLOW_PROTOCOL": protocol,
    }
    for k in ("DEVELOPER_DIR", "TMPDIR"):  # needed by the xcode-select git shim
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def _git_prefix(protocol: str) -> list[str]:
    return [
        "git",
        "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "core.symlinks=true",
        "-c", "protocol.allow=never",
        "-c", "protocol.https.allow=always",
        f"-c", f"protocol.file.allow={'always' if protocol == 'file' else 'never'}",
        "-c", "protocol.ext.allow=never",
        "-c", "submodule.recurse=false", "-c", "fetch.recurseSubmodules=false",
        "-c", "core.protectNTFS=true", "-c", "core.protectHFS=true",
        "-c", "fsck.symlinkPointsToGitDir=error", "-c", "transfer.fsckObjects=true",
        "-c", "advice.detachedHead=false",
    ]


def _run(argv: list[str], env: dict[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, env=env, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired as e:
        raise GitTimeout(f"git timed out after {timeout}s: {argv[-3:]}") from e


_ROOT_ALIASES = {"/tmp", "/var", "/etc"}  # macOS root symlinks into /private


def _check_no_symlinks_in_path(p: Path) -> None:
    """lstat every component of an absolute path; only the macOS root aliases may be symlinks."""
    cur = "/"
    for part in Path(p).parts[1:]:
        cur = os.path.join(cur, part)
        try:
            is_link = os.path.islink(cur)
        except OSError as e:
            raise GitCommandFailed(f"cannot inspect path component: {e}") from e
        if is_link and cur not in _ROOT_ALIASES:
            raise GitCommandFailed(f"refusing symlink in path: {cur}")


def _identity(p: Path) -> tuple[int, int]:
    try:
        st = os.lstat(p)
    except OSError as e:
        raise GitCommandFailed(f"cannot stat {p.name}: {e}") from e
    return (st.st_dev, st.st_ino)


def _dotgit_like(name: str) -> bool:
    """True if name is .git after NFKC, dropping marks (Mn/Me), control/format chars and
    whitespace, and casefolding. Homoglyphs from other scripts are an accepted gap."""
    kept = "".join(
        c for c in unicodedata.normalize("NFKC", name)
        if unicodedata.category(c) not in ("Mn", "Me", "Cc", "Cf", "Zl", "Zp") and not c.isspace()
    )
    return kept.casefold() == ".git"


def _check_symlinks(root: Path) -> None:
    """Fail if any symlink in the checkout resolves outside root or into .git.

    Uses lstat/readlink/realpath only; link targets are never opened.
    """
    real_root = os.path.realpath(root)
    git_dir = os.path.join(real_root, ".git")
    stack = [real_root]
    while stack:
        d = stack.pop()
        with os.scandir(d) as it:
            for e in it:
                if _dotgit_like(e.name) and not (d == real_root and e.name == ".git"):
                    raise UnsafeTreeEntry("tree contains an entry that looks like .git")
                if e.is_symlink():
                    target = os.path.realpath(e.path)
                    inside = target == real_root or target.startswith(real_root + os.sep)
                    in_git = target == git_dir or target.startswith(git_dir + os.sep)
                    if not inside or in_git:
                        rel = os.path.relpath(e.path, real_root)
                        raise UnsafeSymlink(f"symlink {rel} points outside the checkout or into .git")
                elif e.is_dir(follow_symlinks=False) and not (d == real_root and e.name == ".git"):
                    stack.append(e.path)


def materialize(
    remote_url: str,
    commit: str,
    dest: Path,
    allowed_ref_prefix: str = "refs/heads/bh/",
    allow_file_protocol: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> MaterializeResult:
    if not isinstance(commit, str) or not _SHA_RE.fullmatch(commit):
        raise InvalidCommit("commit must be 40 lowercase hex characters")
    protocol = _validate_url(remote_url, allow_file_protocol)
    if not _PREFIX_RE.fullmatch(allowed_ref_prefix) or ".." in allowed_ref_prefix:
        raise ValueError("allowed_ref_prefix must look like refs/heads/bh/<name>/")

    dest = Path(os.path.abspath(dest))  # absolute: a relative "-x" must never reach git as an option
    mirror = dest.with_name(dest.name + ".mirror.git")
    for p in (dest, mirror):
        _check_no_symlinks_in_path(p)
    if os.path.lexists(dest):
        raise GitCommandFailed("dest already exists; materialize only writes into a path it creates")
    mirror_existed = os.path.lexists(mirror)
    created: dict[Path, tuple[int, int]] = {}

    def track(p: Path) -> None:  # remember identity of a dir we created, so cleanup removes only that
        created[p] = _identity(p)
    local_prefix = "refs/remotes/origin/" + allowed_ref_prefix[len("refs/heads/"):]
    env, g = _env(protocol), _git_prefix(protocol)

    def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
        argv = g + (["-C", str(cwd)] if cwd else []) + list(args)
        r = _run(argv, env=env, timeout=timeout)
        if check and r.returncode != 0:
            raise GitCommandFailed(f"git {args[0]} failed: {r.stderr.strip()[:300]}")
        return r.stdout.strip() if r.returncode == 0 else ""

    try:
        if not mirror_existed:
            # --template= : no hooks or info/exclude from any template dir
            git("init", "-q", "--bare", "--template=", str(mirror))
            track(mirror)
        # A reused mirror may hold refs the remote has since deleted: start from none, so
        # reachability is decided only by refs this fetch produced.
        for stale in git("for-each-ref", "--format=%(refname)", local_prefix, cwd=mirror).splitlines():
            git("update-ref", "-d", stale, cwd=mirror)
        base_fetch = ["fetch", "-q", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head"]
        git(*base_fetch, remote_url, f"+{allowed_ref_prefix}*:{local_prefix}*", cwd=mirror)
        have = _run(g + ["-C", str(mirror), "cat-file", "-e", f"{commit}^{{commit}}"], env, timeout)
        if have.returncode != 0:
            fetched = _run(g + ["-C", str(mirror)] + base_fetch + [remote_url, commit], env, timeout)
            if fetched.returncode != 0:
                raise CommitNotFound(f"commit {commit} not found on remote")
        if git("cat-file", "-t", commit, cwd=mirror) != "commit":
            raise CommitNotFound(f"{commit} is not a commit")

        contains = git("for-each-ref", "--contains", commit, "--format=%(refname)", local_prefix, cwd=mirror)
        refs = sorted(f"refs/heads/{r[len('refs/remotes/origin/'):]}" for r in contains.splitlines() if r)
        if not refs:
            raise NotReachableFromAllowedRefs(f"{commit} is not reachable from {allowed_ref_prefix}*")

        git("worktree", "prune", cwd=mirror)  # drop registrations whose dir was deleted
        git("worktree", "add", "--detach", "--no-checkout", str(dest), commit, cwd=mirror)
        track(dest)
        git("-c", "filter.lfs.smudge=", "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false",
            "checkout", "-q", "--detach", commit, cwd=dest)

        head = git("rev-parse", "HEAD", cwd=dest)
        if head != commit:
            raise TreeMismatch(f"HEAD {head} != {commit}")
        tree = git("rev-parse", "HEAD^{tree}", cwd=dest)
        if tree != git("rev-parse", f"{commit}^{{tree}}", cwd=mirror):
            raise TreeMismatch("worktree tree differs from commit tree")
        dirty = git("status", "--porcelain", "--ignore-submodules=all", cwd=dest)
        if dirty:
            raise TreeMismatch(f"worktree dirty after checkout: {dirty[:200]}")
        _check_symlinks(dest)
        return MaterializeResult(commit=commit, tree=tree, refs=refs, dest=dest, mirror=mirror)
    except BaseException:
        for p, ident in created.items():
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if ident == (st.st_dev, st.st_ino):  # still the dir we made, not a replacement
                shutil.rmtree(p, ignore_errors=True)
        raise
