from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from borrowed_hands_helper import gitmat
from borrowed_hands_helper.gitmat import (
    CommitNotFound, GitTimeout, InvalidCommit, NotReachableFromAllowedRefs,
    ProtocolNotAllowed, materialize,
)
from conftest import GIT_ENV, commit_files, git


def url(p: Path) -> str:
    return f"file://{p}"


def mat(remote: Path, sha: str, dest: Path):
    return materialize(url(remote), sha, dest, allow_file_protocol=True)


def bh_branch(remote: Path, files: dict[str, str], name: str = "bh/job1") -> str:
    git(remote, "checkout", "-q", "-b", name)
    sha = commit_files(remote, files)
    git(remote, "checkout", "-q", "main")
    return sha


# ---- happy path ---------------------------------------------------------

def test_happy_path(remote: Path, tmp_path: Path):
    sha = bh_branch(remote, {"a.txt": "x\n"})
    res = mat(remote, sha, tmp_path / "wt")
    assert res.commit == sha
    assert res.tree == git(remote, "rev-parse", f"{sha}^{{tree}}")
    assert res.refs == ["refs/heads/bh/job1"]
    assert (tmp_path / "wt" / "a.txt").read_text() == "x\n"
    assert git(tmp_path / "wt", "rev-parse", "HEAD") == sha


def test_commit_on_main_and_bh_reports_bh_only(remote: Path, tmp_path: Path):
    sha = git(remote, "rev-parse", "HEAD")
    git(remote, "branch", "bh/same")
    res = mat(remote, sha, tmp_path / "wt")
    assert res.refs == ["refs/heads/bh/same"]


# ---- rejection ----------------------------------------------------------

def test_commit_only_on_main_rejected(remote: Path, tmp_path: Path):
    sha = commit_files(remote, {"m.txt": "m\n"})
    with pytest.raises(NotReachableFromAllowedRefs):
        mat(remote, sha, tmp_path / "wt")
    assert not (tmp_path / "wt" / "m.txt").exists()


def test_no_bh_refs_at_all(remote: Path, tmp_path: Path):
    sha = git(remote, "rev-parse", "HEAD")
    with pytest.raises(NotReachableFromAllowedRefs):
        mat(remote, sha, tmp_path / "wt")


def test_nonexistent_sha(remote: Path, tmp_path: Path):
    bh_branch(remote, {"a": "1"})
    with pytest.raises(CommitNotFound):
        mat(remote, "a" * 40, tmp_path / "wt")


@pytest.mark.parametrize("bad", ["abc1234", "A" * 40, "g" * 40, "a" * 41, "", "HEAD",
                                 "a" * 39 + "\n", "-" + "a" * 39])
def test_bad_commit_strings(remote: Path, tmp_path: Path, bad: str):
    with pytest.raises(InvalidCommit):
        mat(remote, bad, tmp_path / "wt")


@pytest.mark.parametrize("u", ["http://example.com/r.git", "git://example.com/r.git",
                               "ssh://git@example.com/r.git", "ext::sh -c id", "/tmp/x",
                               "-uhttps://x", "https:///nohost", "file:///tmp/x"])
def test_protocol_enforced(tmp_path: Path, u: str):
    with pytest.raises(ProtocolNotAllowed):
        materialize(u, "a" * 40, tmp_path / "wt")
    assert not (tmp_path / "wt").exists()


def test_file_flag_does_not_open_other_schemes(tmp_path: Path):
    with pytest.raises(ProtocolNotAllowed):
        materialize("http://example.com/r.git", "a" * 40, tmp_path / "wt", allow_file_protocol=True)


def test_https_url_accepted_syntactically(tmp_path: Path, monkeypatch):
    seen: list[list[str]] = []

    def fake(argv, **kw):
        seen.append(argv)
        raise gitmat.CommitNotFound("stop")
    monkeypatch.setattr(gitmat, "_run", fake)
    with pytest.raises(CommitNotFound):
        materialize("https://example.com/r.git", "a" * 40, tmp_path / "wt")
    assert seen  # got past validation


def test_hardened_argv_and_env(tmp_path: Path, monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    def fake(argv, env, timeout, **kw):
        calls.append((argv, env))
        out = ""
        if "init" in argv or ("worktree" in argv and "add" in argv):
            Path(argv[-1] if "init" in argv else argv[-2]).mkdir()
        if "-t" in argv:
            out = "commit"
        elif "for-each-ref" in argv:
            out = "refs/remotes/origin/bh/x"
        elif "rev-parse" in argv:
            out = "a" * 40
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(gitmat, "_run", fake)
    monkeypatch.setattr(gitmat, "_check_symlinks", lambda *a, **k: None)
    monkeypatch.setenv("GIT_SSH_COMMAND", "evil")
    monkeypatch.setenv("GIT_EXEC_PATH", "/evil")
    materialize("https://example.com/r.git", "a" * 40, tmp_path / "wt")
    assert any("fetch" in a for a, _ in calls)
    argv, env = calls[-1]
    joined = " ".join(" ".join(a) for a, _ in calls)
    for needle in ["core.hooksPath=/dev/null", "core.fsmonitor=false", "protocol.allow=never",
                   "protocol.https.allow=always", "protocol.file.allow=never",
                   "protocol.ext.allow=never", "submodule.recurse=false",
                   "transfer.fsckObjects=true", "--no-recurse-submodules", "--template=",
                   "core.protectHFS=true", "core.protectNTFS=true",
                   "fsck.symlinkPointsToGitDir=error", "filter.lfs.smudge=",
                   "filter.lfs.process=", "filter.lfs.required=false"]:
        assert needle in joined, needle
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_LFS_SKIP_SMUDGE"] == "1"
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert "GIT_SSH_COMMAND" not in env and "GIT_EXEC_PATH" not in env
    assert all(isinstance(a, str) for a in argv)


def test_timeout(remote: Path, tmp_path: Path, monkeypatch):
    bh_branch(remote, {"a": "1"})
    real = subprocess.run

    def slow(argv, **kw):
        kw["timeout"] = 0.0001
        return real(["sleep", "2"], **{k: v for k, v in kw.items() if k != "env"})
    monkeypatch.setattr(gitmat.subprocess, "run", slow)
    with pytest.raises(GitTimeout):
        mat(remote, "a" * 40, tmp_path / "wt")


# ---- malicious repo content --------------------------------------------

def marker_cmd(marker: Path) -> str:
    return f"touch {marker}"


def test_repo_hooks_and_config_do_not_fire(remote: Path, tmp_path: Path):
    """Committed .githooks + remote-side .git/config pointing at it, fsmonitor and filter too."""
    marker = tmp_path / "pwned-hooks"
    hook = f"#!/bin/sh\n{marker_cmd(marker)}\n"
    git(remote, "checkout", "-q", "-b", "bh/evil")
    (remote / ".githooks").mkdir()
    (remote / ".githooks/post-checkout").write_text(hook)
    os.chmod(remote / ".githooks/post-checkout", 0o755)
    sha = commit_files(remote, {".gitattributes": "* filter=evil\n"})
    git(remote, "checkout", "-q", "main")
    # remote-side config an attacker controls (never transferred by fetch, but prove it)
    git(remote, "config", "core.hooksPath", ".githooks")
    git(remote, "config", "core.fsmonitor", marker_cmd(marker))
    git(remote, "config", "filter.evil.smudge", marker_cmd(marker))
    git(remote, "config", "filter.evil.process", marker_cmd(marker))

    # positive control: a plain checkout WITH hooksPath configured fires the payload
    ctl = tmp_path / "ctl"
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(remote), str(ctl)],
                   env=GIT_ENV, check=True)
    git(ctl, "checkout", "-q", sha)
    git(ctl, "config", "core.hooksPath", ".githooks")
    git(ctl, "checkout", "-q", "--detach", sha)  # re-checkout now runs the committed hook
    assert marker.exists(), "control: payload should fire when hooksPath is honoured"
    marker.unlink()

    res = mat(remote, sha, tmp_path / "wt")
    assert res.commit == sha
    assert not marker.exists()


def test_filter_in_gitattributes_with_configured_filter_is_control(remote: Path, tmp_path: Path):
    marker = tmp_path / "pwned-filter"
    git(remote, "checkout", "-q", "-b", "bh/f")
    sha = commit_files(remote, {".gitattributes": "*.txt filter=evil\n", "a.txt": "x\n"})
    git(remote, "checkout", "-q", "main")
    # control: clone with the smudge filter configured fires it
    ctl = tmp_path / "ctl"
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(remote), str(ctl)],
                   env=GIT_ENV, check=True)
    git(ctl, "config", "filter.evil.smudge", f"sh -c 'touch {marker}; cat'")
    git(ctl, "checkout", "-q", sha)
    assert marker.exists()
    marker.unlink()
    res = mat(remote, sha, tmp_path / "wt")
    assert not marker.exists()
    assert (tmp_path / "wt" / "a.txt").read_text() == "x\n"
    assert res.commit == sha


def test_lfs_filter_attributes_are_passthrough(remote: Path, tmp_path: Path):
    git(remote, "checkout", "-q", "-b", "bh/lfs")
    sha = commit_files(remote, {".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n",
                                "a.bin": "not really lfs\n"})
    git(remote, "checkout", "-q", "main")
    mat(remote, sha, tmp_path / "wt")
    assert (tmp_path / "wt" / "a.bin").read_text() == "not really lfs\n"


@pytest.mark.parametrize("sub_url", [
    "ext::sh -c 'touch {marker}'",
    "file://{marker_dir}/nonexistent-submodule",
    "../../../../etc",
])
def test_submodules_not_fetched_or_updated(remote: Path, tmp_path: Path, sub_url: str):
    marker = tmp_path / "pwned-sub"
    sub_url = sub_url.format(marker=marker, marker_dir=tmp_path)
    git(remote, "checkout", "-q", "-b", "bh/sub")
    (remote / ".gitmodules").write_text(f'[submodule "s"]\n\tpath = s\n\turl = {sub_url}\n')
    git(remote, "add", ".gitmodules")
    git(remote, "update-index", "--add", "--cacheinfo", f"160000,{'b' * 40},s")
    git(remote, "commit", "-q", "-m", "sub")
    sha = git(remote, "rev-parse", "HEAD")
    git(remote, "checkout", "-q", "main")
    try:
        res = mat(remote, sha, tmp_path / "wt")
        assert res.commit == sha
    except gitmat.MaterializeError:
        pass  # fsck rejecting a hostile .gitmodules is also fine
    assert not marker.exists()


def test_template_hook_in_env_is_ignored(remote: Path, tmp_path: Path, monkeypatch):
    marker = tmp_path / "pwned-template"
    tpl = tmp_path / "tpl"
    (tpl / "hooks").mkdir(parents=True)
    hook = tpl / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\n{marker_cmd(marker)}\n")
    hook.chmod(0o755)
    sha = bh_branch(remote, {"a": "1"})
    # control: init with the template, then a checkout fires the hook
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    env = {"GIT_TEMPLATE_DIR": str(tpl)}
    git(ctl, "init", "-q", env_extra=env)
    git(ctl, "fetch", "-q", url(remote), "refs/heads/bh/job1", env_extra={"GIT_ALLOW_PROTOCOL": "file"})
    git(ctl, "checkout", "-q", "FETCH_HEAD")
    assert marker.exists(), "control: template hook should fire"
    marker.unlink()

    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tpl))
    mat(remote, sha, tmp_path / "wt")
    assert not marker.exists()


def test_dirty_or_existing_dest_is_error(remote: Path, tmp_path: Path):
    sha = bh_branch(remote, {"a": "1"})
    dest = tmp_path / "wt"
    dest.mkdir()
    (dest / "junk").write_text("x")
    with pytest.raises(gitmat.MaterializeError):
        mat(remote, sha, dest)
    assert (dest / "junk").read_text() == "x"  # pre-existing dest is never deleted


# ---- review fixes -------------------------------------------------------

def test_reused_mirror_drops_stale_refs(remote: Path, tmp_path: Path):
    sha = bh_branch(remote, {"a": "1"}, name="bh/evil")
    dest = tmp_path / "wt"
    res = mat(remote, sha, dest)
    shutil.rmtree(dest)
    git(res.mirror, "worktree", "prune")
    git(remote, "branch", "-q", "-D", "bh/evil")
    with pytest.raises(NotReachableFromAllowedRefs):
        mat(remote, sha, dest)  # same mirror, branch gone upstream


def test_stale_registered_worktree_is_pruned(remote: Path, tmp_path: Path):
    sha = bh_branch(remote, {"a": "1"})
    dest = tmp_path / "wt"
    mat(remote, sha, dest)
    shutil.rmtree(dest)  # worktree registration left behind, no prune
    assert mat(remote, sha, dest).commit == sha


def _evil_link(remote: Path, link_target: str, name: str = "bh/link") -> str:
    git(remote, "checkout", "-q", "-b", name)
    (remote / "ln").symlink_to(link_target)
    sha = commit_files(remote, {"x": "1"})
    git(remote, "checkout", "-q", "main")
    return sha


@pytest.mark.parametrize("target", ["/etc/passwd", "../../../.ssh/id_rsa", "../outside",
                                    ".git/hooks/post-checkout", ".git", "sub/../.git/config"])
def test_escaping_symlinks_rejected_and_cleaned(remote: Path, tmp_path: Path, target: str):
    sha = _evil_link(remote, target)
    dest = tmp_path / "wt"
    with pytest.raises(gitmat.UnsafeSymlink):
        mat(remote, sha, dest)
    assert not dest.exists() and not (tmp_path / "wt.mirror.git").exists()


def test_symlink_chain_escape_rejected(remote: Path, tmp_path: Path):
    git(remote, "checkout", "-q", "-b", "bh/chain")
    (remote / "a").symlink_to("/etc")
    (remote / "b").symlink_to("a/passwd")  # lexically inside, really outside
    sha = commit_files(remote, {"x": "1"})
    git(remote, "checkout", "-q", "main")
    with pytest.raises(gitmat.UnsafeSymlink):
        mat(remote, sha, tmp_path / "wt")


def test_internal_symlink_ok(remote: Path, tmp_path: Path):
    git(remote, "checkout", "-q", "-b", "bh/ok")
    (remote / "d").mkdir()
    (remote / "d" / "t.txt").write_text("t")
    (remote / "ln").symlink_to("d/t.txt")
    (remote / "d" / "up").symlink_to("../d/t.txt")
    (remote / "dangling").symlink_to("nothing-here")
    sha = commit_files(remote, {"x": "1"})
    git(remote, "checkout", "-q", "main")
    res = mat(remote, sha, tmp_path / "wt")
    assert res.commit == sha and (tmp_path / "wt" / "ln").is_symlink()


@pytest.mark.parametrize("prefix", ["refs/heads/", "refs/heads/bh", "refs/heads/bh/*", "refs/heads/bh/a b/",
                                    "refs/heads/bh/a:b/", "refs/heads/bh/a\n/", "refs/heads/other/",
                                    "refs/heads/bh/../x/", "refs/tags/bh/"])
def test_bad_ref_prefix(remote: Path, tmp_path: Path, prefix: str):
    with pytest.raises(ValueError):
        materialize(url(remote), "a" * 40, tmp_path / "wt", allowed_ref_prefix=prefix,
                    allow_file_protocol=True)


def test_relative_dest_with_dash_is_not_an_option(remote: Path, tmp_path: Path, monkeypatch):
    sha = bh_branch(remote, {"a": "1"})
    monkeypatch.chdir(tmp_path)
    res = materialize(url(remote), sha, Path("-wt"), allow_file_protocol=True)
    assert res.dest.is_absolute() and (tmp_path / "-wt" / "a").exists()


def test_symlink_dest_refused(remote: Path, tmp_path: Path):
    sha = bh_branch(remote, {"a": "1"})
    target = tmp_path / "real"
    target.mkdir()
    (tmp_path / "wt").symlink_to(target)
    with pytest.raises(gitmat.MaterializeError):
        mat(remote, sha, tmp_path / "wt")
    assert list(target.iterdir()) == []


def test_symlink_mirror_refused(remote: Path, tmp_path: Path):
    sha = bh_branch(remote, {"a": "1"})
    target = tmp_path / "real"
    target.mkdir()
    (tmp_path / "wt.mirror.git").symlink_to(target)
    with pytest.raises(gitmat.MaterializeError):
        mat(remote, sha, tmp_path / "wt")
    assert list(target.iterdir()) == []


# ---- review round 2 -----------------------------------------------------

def test_parent_symlink_in_dest_path_refused(remote: Path, tmp_path: Path):
    sha = bh_branch(remote, {"a": "1"})
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(gitmat.MaterializeError):
        mat(remote, sha, tmp_path / "link" / "wt")
    assert list(real.iterdir()) == []


def test_macos_root_alias_is_allowed(remote: Path, tmp_path: Path):
    p = str(tmp_path)
    if not p.startswith("/private/var/"):
        pytest.skip("no /var alias here")
    sha = bh_branch(remote, {"a": "1"})
    assert mat(remote, sha, Path(p[len("/private"):]) / "wt").commit == sha


def test_precreated_empty_dest_refused_and_left_alone(remote: Path, tmp_path: Path):
    sha = _evil_link(remote, "/etc/passwd")
    dest = tmp_path / "wt"
    dest.mkdir()
    with pytest.raises(gitmat.MaterializeError):
        mat(remote, sha, dest)
    assert list(dest.iterdir()) == []  # nothing was written into it


def test_precreated_empty_dest_refused_before_any_git(tmp_path: Path, monkeypatch):
    dest = tmp_path / "wt"
    dest.mkdir()
    monkeypatch.setattr(gitmat, "_run", lambda *a, **k: pytest.fail("git must not run"))
    with pytest.raises(gitmat.MaterializeError):
        materialize("https://example.com/r.git", "a" * 40, dest)


@pytest.mark.parametrize("name", [".git\n", ".git\u200b"])
def test_dotgit_lookalike_entries_rejected(remote: Path, tmp_path: Path, name: str):
    git(remote, "checkout", "-q", "-b", "bh/dg")
    (remote / "t").write_text("t")
    (remote / name).symlink_to("t")
    sha = commit_files(remote, {"x": "1"})
    git(remote, "checkout", "-q", "main")
    with pytest.raises(gitmat.MaterializeError) as ei:
        mat(remote, sha, tmp_path / "wt")
    assert isinstance(ei.value, gitmat.UnsafeTreeEntry)
    assert not (tmp_path / "wt").exists()


def test_cleanup_only_removes_the_dir_we_created(remote: Path, tmp_path: Path, monkeypatch):
    sha = _evil_link(remote, "/etc/passwd")
    dest = tmp_path / "wt"
    real_check = gitmat._check_symlinks

    def swap_then_check(root):
        dest.rename(tmp_path / "wt.old")      # someone swaps our dir for theirs
        dest.mkdir()
        (dest / "theirs").write_text("keep")
        real_check(tmp_path / "wt.old")

    monkeypatch.setattr(gitmat, "_check_symlinks", swap_then_check)
    with pytest.raises(gitmat.UnsafeSymlink):
        mat(remote, sha, dest)
    assert (dest / "theirs").read_text() == "keep"


def test_identity_failure_is_explicit(tmp_path: Path):
    with pytest.raises(gitmat.MaterializeError):
        gitmat._identity(tmp_path / "missing")


@pytest.mark.parametrize("name", [".git", ".GIT", ".git\n", ".git\u200b", "．git", ".ｇit",
                                  ".git️", ".gi̇t", ".g\u200bit "])
def test_dotgit_predicate_catches_lookalikes(name: str):
    assert gitmat._dotgit_like(name)


@pytest.mark.parametrize("name", [".gitignore", "git", ".github", "a.git", ".gitmodules"])
def test_dotgit_predicate_spares_normal_names(name: str):
    assert not gitmat._dotgit_like(name)


@pytest.mark.parametrize("name", ["．git", ".ｇit", ".git️"])
def test_dotgit_unicode_lookalike_entries_rejected(remote: Path, tmp_path: Path, name: str):
    git(remote, "checkout", "-q", "-b", "bh/ul")
    (remote / "t").write_text("t")
    (remote / name).symlink_to("t")
    sha = commit_files(remote, {"x": "1"})
    git(remote, "checkout", "-q", "main")
    with pytest.raises(gitmat.UnsafeTreeEntry):
        mat(remote, sha, tmp_path / "wt")
