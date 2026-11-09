"""HE-6: device key storage rules (PROTOCOL 3.4). Each rule has a test that breaks it."""
from __future__ import annotations

import os
import stat

import pytest

from borrowed_hands_helper import keyfile
from borrowed_hands_helper._shared import bhproto
from borrowed_hands_helper.keyfile import UnsafeKeyStorage


@pytest.fixture
def d(tmp_path):
    p = tmp_path / "BorrowedHands"
    keyfile.ensure_private_dir(p)
    return p


def test_dir_created_0700_and_key_roundtrip(d):
    assert stat.S_IMODE(os.lstat(d).st_mode) == 0o700
    priv = keyfile.create_device_key(d)
    kp = d / keyfile.KEY_NAME
    st = os.lstat(kp)
    assert stat.S_IMODE(st.st_mode) == 0o600 and st.st_nlink == 1
    assert bhproto.pub_raw(keyfile.load_device_key(d)) == bhproto.pub_raw(priv)
    assert not (d / (keyfile.KEY_NAME + ".tmp")).exists()


def test_existing_dir_with_wrong_mode_refused(tmp_path):
    p = tmp_path / "BH"
    p.mkdir(mode=0o755)
    os.chmod(p, 0o755)
    with pytest.raises(UnsafeKeyStorage, match="unsafe permissions"):
        keyfile.ensure_private_dir(p)


def test_symlinked_dir_refused(tmp_path):
    real = tmp_path / "real"
    keyfile.ensure_private_dir(real)
    keyfile.create_device_key(real)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(UnsafeKeyStorage):
        keyfile.load_device_key(link)


def test_symlinked_key_file_refused(d, tmp_path):
    other = tmp_path / "elsewhere"
    keyfile.ensure_private_dir(other)
    keyfile.create_device_key(other)
    (d / keyfile.KEY_NAME).symlink_to(other / keyfile.KEY_NAME)
    with pytest.raises(UnsafeKeyStorage):
        keyfile.load_device_key(d)


def test_key_mode_0644_refused(d):
    keyfile.create_device_key(d)
    os.chmod(d / keyfile.KEY_NAME, 0o644)
    with pytest.raises(UnsafeKeyStorage, match="0600"):
        keyfile.load_device_key(d)


def test_dir_mode_changed_later_refused(d):
    keyfile.create_device_key(d)
    os.chmod(d, 0o750)
    with pytest.raises(UnsafeKeyStorage):
        keyfile.load_device_key(d)


def test_preexisting_tmp_is_an_error_not_overwritten(d):
    tmp = d / (keyfile.KEY_NAME + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, b"planted")
    os.close(fd)
    with pytest.raises(UnsafeKeyStorage, match="tmp"):
        keyfile.create_device_key(d)
    assert tmp.read_bytes() == b"planted"
    assert not (d / keyfile.KEY_NAME).exists()


def test_hard_link_nlink_2_refused(d):
    keyfile.create_device_key(d)
    os.link(d / keyfile.KEY_NAME, d / "second-name")
    with pytest.raises(UnsafeKeyStorage, match="link"):
        keyfile.load_device_key(d)


def test_other_owner_refused(d, monkeypatch):
    keyfile.create_device_key(d)
    real = os.getuid()
    monkeypatch.setattr(keyfile.os, "getuid", lambda: real + 1)
    with pytest.raises(UnsafeKeyStorage, match="owner|owned"):
        keyfile.load_device_key(d)


def test_create_refuses_existing_key(d):
    keyfile.create_device_key(d)
    with pytest.raises(UnsafeKeyStorage):
        keyfile.create_device_key(d)


def test_rotation_replaces_atomically_and_keeps_no_old_copy(d):
    old = keyfile.create_device_key(d)
    new = bhproto.new_private_key()
    keyfile.replace_device_key(d, new)
    assert bhproto.pub_raw(keyfile.load_device_key(d)) == bhproto.pub_raw(new) != bhproto.pub_raw(old)
    assert sorted(p.name for p in d.iterdir()) == [keyfile.KEY_NAME]


def test_garbage_key_content_refused(d):
    keyfile.write_private_file(d / keyfile.KEY_NAME, b"not base64 at all!!\n")
    with pytest.raises(UnsafeKeyStorage):
        keyfile.load_device_key(d)


def test_delete_key(d):
    keyfile.create_device_key(d)
    keyfile.delete_device_key(d)
    assert not (d / keyfile.KEY_NAME).exists()
    keyfile.delete_device_key(d)  # idempotent


def test_key_file_owned_by_another_user_refused_even_in_our_dir(d, monkeypatch):
    keyfile.create_device_key(d)
    real_fstat = keyfile.os.fstat

    def fstat(fd):
        st = real_fstat(fd)
        fields = list(st)
        fields[stat.ST_UID] = st.st_uid + 1
        return os.stat_result(fields)
    monkeypatch.setattr(keyfile.os, "fstat", fstat)
    with pytest.raises(UnsafeKeyStorage, match="owned by another user"):
        keyfile.load_device_key(d)
