"""Private file storage for the device key and the config (PROTOCOL 3.4).

Rules, each tested (HE-6):
- the directory is a real directory (lstat, not a symlink), owned by the user, mode exactly 0700;
- files are written atomically: `<name>.tmp` opened O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW mode 0600, fsync,
  rename over the final name; an existing tmp file is an error, never overwritten;
- files are read with O_RDONLY|O_NOFOLLOW then fstat: a regular file, owned by the user, mode 0600, nlink 1;
- rotation writes the new key through the same atomic path; the old key is never kept.
Residual risk (PROTOCOL 8): any process running as the same user can read these files.
"""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from ._shared import bhproto

KEY_NAME = "device_ed25519.key"
MAX_FILE = 1 << 20


class UnsafeKeyStorage(Exception):
    """Refuse to start: the message says what is wrong (never the key itself)."""


def check_private_dir(d: Path) -> None:
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        raise UnsafeKeyStorage(f"{d} does not exist; run 'bh setup'") from None
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) != 0o700):
        raise UnsafeKeyStorage(f"unsafe permissions on {d}: it must be a real directory owned by you with "
                               f"mode 0700 (fix: chmod 700 '{d}')")


def ensure_private_dir(d: Path) -> None:
    d = Path(d)
    d.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(d, 0o700)
        os.chmod(d, 0o700)          # umask may have narrowed it; never widen beyond 0700
    except FileExistsError:
        pass
    check_private_dir(d)


def write_private_file(path: Path, data: bytes) -> None:
    path = Path(path)
    check_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise UnsafeKeyStorage(f"{tmp.name} already exists (an interrupted write or tampering); inspect "
                               f"and remove it, then retry") from None
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(tmp)
        raise
    os.close(fd)
    os.rename(tmp, path)


def read_private_file(path: Path) -> bytes:
    path = Path(path)
    check_private_dir(path.parent)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise UnsafeKeyStorage(f"{path.name} is a symlink; refusing") from None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeKeyStorage(f"{path.name} is not a regular file; refusing")
        if st.st_uid != os.getuid():
            raise UnsafeKeyStorage(f"{path.name} is owned by another user; refusing")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise UnsafeKeyStorage(f"{path.name} must have mode 0600 (fix: chmod 600 '{path}')")
        if st.st_nlink != 1:
            raise UnsafeKeyStorage(f"{path.name} has {st.st_nlink} hard links; refusing")
        if st.st_size > MAX_FILE:
            raise UnsafeKeyStorage(f"{path.name} is too large")
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _encode(priv) -> bytes:
    return (bhproto.b64u(bhproto.seed_of(priv)) + "\n").encode("ascii")


def create_device_key(d: Path):
    """A fresh key, refusing to replace an existing one (rotation uses replace_device_key)."""
    p = Path(d) / KEY_NAME
    if os.path.lexists(p):
        raise UnsafeKeyStorage(f"{KEY_NAME} already exists; refusing to overwrite it")
    priv = bhproto.new_private_key()
    write_private_file(p, _encode(priv))
    return priv


def replace_device_key(d: Path, priv) -> None:
    write_private_file(Path(d) / KEY_NAME, _encode(priv))


def load_device_key(d: Path):
    raw = read_private_file(Path(d) / KEY_NAME)
    try:
        text = raw.decode("ascii")
        if not text.endswith("\n") or "\n" in text[:-1]:
            raise ValueError
        return bhproto.priv_from_seed(bhproto.unb64u(text[:-1]))
    except (ValueError, UnicodeDecodeError):
        raise UnsafeKeyStorage(f"{KEY_NAME} is not a valid key file; run 'bh pair' again") from None


def has_device_key(d: Path) -> bool:
    return os.path.lexists(Path(d) / KEY_NAME)


def delete_device_key(d: Path) -> None:
    for name in (KEY_NAME, KEY_NAME + ".tmp"):
        try:
            os.unlink(Path(d) / name)
        except FileNotFoundError:
            pass
