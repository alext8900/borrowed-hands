"""The server's Ed25519 signing key in the plugin data dir (SPEC 7): a 32-byte seed, base64url, one line, 0600.

Created under an exclusive lock with a re-check (O_EXCL|O_NOFOLLOW tmp file, fsync, rename), so it is
never replaced. Loaded with O_NOFOLLOW and fstat checks. The key
is never logged; only its key id is.
"""
from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from . import bhproto

KEY_FILE = "server_ed25519.key"
LOCK_FILE = "server_ed25519.lock"


class KeyFileError(RuntimeError):
    pass


def _write_new(path: Path, data: bytes) -> None:
    """Write a complete key to a temp file and rename it into place. Only called under the lock, after
    re-checking that no key exists, so a published key is never replaced."""
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        os.unlink(tmp)  # left by a crashed earlier process with the same pid; we hold the lock
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, path)


@contextmanager
def _locked(data_dir: Path):
    fd = os.open(Path(data_dir) / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600 or st.st_uid != os.getuid() \
                or st.st_nlink != 1:
            raise KeyFileError(f"unsafe permissions on {path.name} (want a regular 0600 file owned by this user)")
        return os.read(fd, 4096)
    finally:
        os.close(fd)


def load_or_create(data_dir: Path):
    path = Path(data_dir) / KEY_FILE
    with _locked(data_dir):  # first-run creation by two processes must agree on one key
        if not path.exists() and not path.is_symlink():
            _write_new(path, (bhproto.b64u(os.urandom(32)) + "\n").encode("ascii"))
        raw = _read(path)
    try:
        seed = bhproto.unb64u(raw.decode("ascii").strip())
    except (UnicodeDecodeError, bhproto.ProtocolError) as exc:
        raise KeyFileError(f"{path.name} is not a base64url Ed25519 seed") from exc
    return bhproto.priv_from_seed(seed)
