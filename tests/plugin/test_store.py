"""store.py: SQLite schema and the state rules the rest of the plugin relies on (SPEC 7, PROTOCOL 3.2, 4.5)."""
import os
import sqlite3
import stat
import threading

import pytest

from borrowed_hands import store as st
from borrowed_hands.store import JobState

H1 = "6a1e2b3c-4d5e-4f60-8172-839405a6b7c8"
J1 = "3f2b8c1e-9a4d-4e6f-8b2a-1c3d5e7f9a0b"
J2 = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def s(data_dir):
    return st.Store(data_dir / "bh.sqlite3")


def _job(s, job_id=J1, nonce="n1", helper_id=H1, expires_at=10_000):
    s.insert_job(job_id=job_id, helper_id=helper_id, action="system.info", contract=b"{}", sig="sig",
                 contract_hash="h" + job_id, nonce=nonce, session_id="sess", now=1, expires_at=expires_at)


def test_file_mode_and_reopen_is_idempotent(s, data_dir):
    p = data_dir / "bh.sqlite3"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    s.add_helper(H1, b"k" * 32, "Mac", now=1, paired_by="telegram:1")
    s2 = st.Store(p)
    assert s2.get_helper(H1).name == "Mac"
    assert s2.schema_version() == st.SCHEMA_VERSION


def test_pair_code_single_use_ttl_superseded(s):
    s.create_code("a" * 64, now=0, ttl_ms=600_000, issued_to="telegram:1")
    assert s.consume_code("a" * 64, now=600_000) is False          # expires_at > now is required
    s.create_code("b" * 64, now=0, ttl_ms=600_000, issued_to="telegram:1")
    s.create_code("c" * 64, now=1, ttl_ms=600_000, issued_to="telegram:1")
    assert s.consume_code("b" * 64, now=2) is False                 # superseded
    assert s.consume_code("c" * 64, now=2) is True
    assert s.consume_code("c" * 64, now=3) is False                 # single use
    assert s.consume_code("d" * 64, now=3) is False


def test_pair_code_consume_is_atomic_under_threads(s):
    s.create_code("e" * 64, now=0, ttl_ms=600_000, issued_to="x:1")
    wins = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        wins.append(s.consume_code("e" * 64, now=1))
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert wins.count(True) == 1


def test_invalidate_codes(s):
    s.create_code("f" * 64, now=0, ttl_ms=600_000, issued_to="x:1")
    s.invalidate_codes(now=1)
    assert s.consume_code("f" * 64, now=2) is False


def test_helpers_revoke_and_lookup(s):
    s.add_helper(H1, b"k" * 32, "Mac mini", now=1, paired_by="telegram:1")
    assert s.find_helper("Mac mini").helper_id == H1
    assert s.find_helper(H1[:8]) is None                            # exact id or exact name only
    s.revoke_helper(H1, now=5)
    assert s.get_helper(H1).revoked_at == 5
    assert s.find_helper("Mac mini").revoked_at == 5


def test_terminal_is_absorbing_first_result_wins(s):
    _job(s)
    assert s.get_job(J1).state is JobState.PENDING_SEND
    assert s.update_state(J1, JobState.QUEUED, now=2) is True
    assert s.finish(J1, JobState.FAILED, result={"a": 1}, error=None, now=3) is True
    assert s.finish(J1, JobState.SUCCEEDED, result={"a": 2}, error=None, now=4) is False
    assert s.update_state(J1, JobState.BUILDING, now=5) is False
    j = s.get_job(J1)
    assert j.state is JobState.FAILED and j.result == {"a": 1}


def test_finish_rejects_non_terminal_and_update_rejects_terminal(s):
    _job(s)
    with pytest.raises(ValueError):
        s.finish(J1, JobState.QUEUED, result=None, error=None, now=2)
    with pytest.raises(ValueError):
        s.update_state(J1, JobState.SUCCEEDED, now=2)


def test_nonce_and_job_id_unique(s):
    _job(s)
    with pytest.raises(sqlite3.IntegrityError):
        _job(s, job_id=J2, nonce="n1")


def test_stale_marking_and_clearing(s):
    _job(s)
    _job(s, job_id=J2, nonce="n2")
    s.finish(J2, JobState.SUCCEEDED, result=None, error=None, now=2)
    assert s.mark_nonterminal_stale() == 1
    assert s.get_job(J1).stale and not s.get_job(J2).stale
    s.update_state(J1, JobState.BUILDING, now=3)
    assert not s.get_job(J1).stale


def test_detached_overlay(s):
    _job(s)
    s.set_detached(H1, 7)
    assert s.get_job(J1).detached_since == 7
    s.set_detached(H1, None)
    assert s.get_job(J1).detached_since is None


def test_notices(s):
    s.add_notice("Paired: Mac", now=1)
    s.add_notice("Unpaired: Mac", now=2)
    assert s.take_notices(now=3) == ["Paired: Mac", "Unpaired: Mac"]
    assert s.take_notices(now=4) == []


def test_audit_rows(s):
    s.audit("pair", helper_id=H1, now=1)
    assert s.audit_events() == ["pair"]
