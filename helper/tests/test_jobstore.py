"""Durable job table (PROTOCOL 4.5): claim/replay, transitions, recovery on start, delivery, pruning."""
from __future__ import annotations

import os
import stat

import pytest

from borrowed_hands_helper.jobstore import Duplicate, Inserted, JobStore, Replay

J1 = "11111111-1111-4111-8111-111111111111"
J2 = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.sqlite3")
    yield s
    s.close()


def claim(store, job_id=J1, nonce="n1", h="h1", now=1000, exp=5000, action="xcode.build"):
    return store.claim(job_id=job_id, nonce=nonce, contract_hash=h, contract=b"{}", action=action,
                       workspace_id="demo", expires_at=exp, now=now)


def test_file_mode_and_pragmas(tmp_path):
    s = JobStore(tmp_path / "jobs.sqlite3")
    assert stat.S_IMODE(os.stat(tmp_path / "jobs.sqlite3").st_mode) == 0o600
    assert s.pragma("journal_mode") == "wal" and s.pragma("synchronous") == 2   # FULL
    s.close()


def test_claim_insert_duplicate_replay(store):
    assert isinstance(claim(store), Inserted)
    assert store.get(J1).state == "received"
    dup = claim(store)
    assert isinstance(dup, Duplicate) and dup.row.job_id == J1
    assert isinstance(claim(store, h="other"), Replay)                  # same job_id, different hash
    assert isinstance(claim(store, job_id=J2, h="h2"), Replay)          # reused nonce
    assert isinstance(claim(store, job_id=J2, nonce="n2", h="h2"), Inserted)


def test_transitions_and_results(store):
    claim(store)
    store.set_state(J1, "queued", now=1100)
    store.set_state(J1, "building", now=1200)
    store.finish(J1, "failed", result=None, error={"code": "timeout", "message": "x"}, now=1300)
    r = store.get(J1)
    assert r.state == "failed" and r.error["code"] == "timeout" and r.delivered is False
    assert [x.job_id for x in store.undelivered()] == [J1]
    assert store.result_body(r) == {"job_id": J1, "state": "failed", "result": None,
                                    "error": {"code": "timeout", "message": "x"}}
    store.mark_delivered([J1])
    assert store.undelivered() == []
    with pytest.raises(ValueError):
        store.set_state(J1, "building", now=1400)   # terminal rows never move again


def test_recover_on_start(store):
    claim(store, job_id=J1, nonce="a")
    store.set_state(J1, "building", now=1)
    claim(store, job_id=J2, nonce="b", exp=2000)
    store.set_state(J2, "queued", now=1)
    j3 = "33333333-3333-4333-8333-333333333333"
    claim(store, job_id=j3, nonce="c", exp=9000)
    store.set_state(j3, "queued", now=1)
    j4 = "44444444-4444-4444-8444-444444444444"
    claim(store, job_id=j4, nonce="d")      # stuck in received (helper died during a confirm dialog)
    still_queued = store.recover_on_start(now=3000)
    assert store.get(J1).state == "failed" and store.get(J1).error["detail"]["reason"] == "helper_restarted"
    assert store.get(J2).state == "expired"
    assert store.get(j4).state == "failed"
    assert [r.job_id for r in still_queued] == [j3]


def test_cancel_requests(store):
    claim(store)
    store.set_state(J1, "queued", now=1)
    assert store.request_cancel(J1) is True
    assert store.cancel_requests() == [J1]
    store.finish(J1, "cancelled", None, {"code": "cancelled", "message": "c"}, now=2)
    assert store.cancel_requests() == []
    assert store.request_cancel(J1) is False
    assert store.request_cancel(J2) is False


def test_prune(store):
    claim(store)
    store.finish(J1, "succeeded", {"x": 1}, None, now=1000)
    store.prune(now=1000 + 24 * 3600_000 + 1)
    r = store.get(J1)
    assert r is not None and r.result is None               # result dropped after 24 h
    assert isinstance(claim(store), (Duplicate, Replay))     # id still blocks replays
    store.prune(now=1000 + 7 * 86400_000 + 1)
    assert store.get(J1) is None
