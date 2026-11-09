from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

GIT_ENV = {
    "PATH": os.environ["PATH"],
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(repo: Path, *args: str, env_extra: dict[str, str] | None = None) -> str:
    """Test-side git helper (trusted repos only)."""
    env = {**GIT_ENV, **(env_extra or {})}
    out = subprocess.run(["git", "-C", str(repo), *args], env=env, check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()


def commit_files(repo: Path, files: dict[str, str], msg: str = "c") -> str:
    for name, body in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A plain repo on main with one commit; tests add bh/* branches."""
    r = tmp_path / "remote"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_files(r, {"README.md": "hi\n"})
    return r


# ---------------------------------------------------------------- no sudo from the test suite, ever
@pytest.fixture(autouse=True)
def _forbid_sudo(monkeypatch):
    """Tests run on Alex's MacBook: any attempt to exec sudo (even `sudo -n`) fails the test instead of running."""
    import asyncio as _asyncio
    import subprocess as _subprocess

    def _is_sudo(argv):
        a = list(argv) if not isinstance(argv, (str, bytes)) else [argv]
        return bool(a) and os.path.basename(str(a[0])) == "sudo"

    real_run, real_popen_init = _subprocess.run, _subprocess.Popen.__init__
    real_exec = _asyncio.create_subprocess_exec

    def run(argv, *a, **kw):
        if _is_sudo(argv):
            raise AssertionError(f"test tried to run sudo: {argv}")
        return real_run(argv, *a, **kw)

    async def create_subprocess_exec(program, *args, **kw):
        if _is_sudo([program]):
            raise AssertionError(f"test tried to run sudo: {[program, *args]}")
        return await real_exec(program, *args, **kw)
    monkeypatch.setattr(_subprocess, "run", run)
    monkeypatch.setattr(_asyncio, "create_subprocess_exec", create_subprocess_exec)
