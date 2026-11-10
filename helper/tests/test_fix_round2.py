"""Fix round 2 (scoped re-review): failed start-up cleanup blocks builds and keeps running rows unresolved
(HIGH 1); only the dispatcher's cancel_sent / not_running count as a clean cancel (HIGH 2); an orderly stop stops dequeueing
before cancelling (HIGH 3); one bounded stop deadline (MEDIUM 4); refused rows stay as replay tombstones for the
full retention (MEDIUM 5)."""
from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import textwrap
import time

import pytest

import helpers as H
from fake_server import FakeServer
from test_daemon import FAST, FakeBuild, arun, build_contract, deps, pair_home, start, stop, until
from test_fix_round1 import GatedDaemon, RecordingBuild, results_for
from borrowed_hands_helper import checks, config, daemon as daemonmod, service
from borrowed_hands_helper.daemon import Daemon
from borrowed_hands_helper.jobstore import Duplicate, JobStore, Replay, ROW_KEEP_MS


class FlakyKill:
    """Fails the first `fails` start-up cleanups, then succeeds."""

    def __init__(self, fails):
        self.fails, self.calls = fails, []

    async def __call__(self, running):
        self.calls.append(list(running))
        if self.fails > 0:
            self.fails -= 1
            return False
        return True


async def crashed_mid_build(tmp_path, srv, queued_too=False):
    """A home whose previous helper died while job A was building (and B was queued, if asked)."""
    home = await pair_home(tmp_path, srv)
    fb = FakeBuild()
    task = await start(home, srv, deps(fb))
    a, ra, sa = build_contract(srv)
    await srv.submit(ra, sa)
    b = None
    if queued_too:
        b, rb, sb = build_contract(srv)
        await srv.submit(rb, sb)
    await until(lambda: fb.started == [a["job_id"]])
    await stop(task)
    st = JobStore(home / "jobs.sqlite3")
    st._db.execute("UPDATE jobs SET state='building', error_json=NULL WHERE job_id=?", (a["job_id"],))
    st.close()
    while not srv.inbox.empty():
        srv.inbox.get_nowait()
    return home, a, b


def xcode_advertised(caps):
    return any(c["name"] == "xcode.build" for c in caps["body"]["capabilities"])


# ------------------------------------------------------------------ HIGH 1
def test_failed_startup_cleanup_blocks_builds_and_keeps_rows_unresolved(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home, a, b = await crashed_mid_build(tmp_path, srv, queued_too=True)
            kill = FlakyKill(fails=10**6)
            fb = RecordingBuild()
            task = asyncio.create_task(Daemon(home, deps(fb, kill_path=kill, sweep_retry_s=0.05)).run())
            caps = await srv.next("caps")
            assert not xcode_advertised(caps)                                   # builds unavailable
            assert JobStore(home / "jobs.sqlite3").get(a["job_id"]).state == "building"   # not resolved
            await srv.submit(*build_contract(srv)[1:])
            err = (await srv.next("error"))["body"]
            assert err["code"] == "helper_unavailable" and err["detail"]["reason"] == "build_cleanup_pending"
            await srv.submit(*srv.contract(srv.helper_id, "system.info")[1:])
            assert (await srv.next("job.result"))["body"]["state"] == "succeeded"   # system.info still works
            await until(lambda: len(kill.calls) >= 3)                         # retried with backoff
            assert all(c == [a["job_id"]] for c in kill.calls)
            assert fb.jobs == {}                                               # the queued build B never ran
            assert JobStore(home / "jobs.sqlite3").get(b["job_id"]).state == "queued"
            from borrowed_hands_helper.daemon import read_status
            assert "build_cleanup_pending" in read_status(home).get("detail", "") + str(read_status(home))
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_builds_resume_only_after_a_successful_sweep(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home, a, b = await crashed_mid_build(tmp_path, srv, queued_too=True)
            kill = FlakyKill(fails=2)
            fb = RecordingBuild()
            task = asyncio.create_task(Daemon(home, deps(fb, kill_path=kill, sweep_retry_s=0.05)).run())
            await until(lambda: b["job_id"] in fb.jobs, timeout=10)
            assert len(kill.calls) == 3                                        # ran only after the 3rd (good) sweep
            row = JobStore(home / "jobs.sqlite3").get(a["job_id"])
            assert row.state == "failed" and row.error["detail"]["reason"] == "helper_restarted"
            caps = None
            for _ in range(10):
                caps = await srv.next("caps")
                if xcode_advertised(caps):
                    break
            assert xcode_advertised(caps)
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


# ------------------------------------------------------------------ Scoped re-review: cleanup vs authority
@pytest.mark.parametrize("change,reason", [
    ("rename", None), ("disable", "capability_disabled"), ("revoke", "grant_revoked"),
])
def test_config_reload_during_cleanup_preserves_only_authorized_builds(tmp_path, change, reason):
    async def main():
        srv = await FakeServer().start()
        task = None
        try:
            home, _, b = await crashed_mid_build(tmp_path, srv, queued_too=True)
            recovered = asyncio.Event()

            async def cleanup(_running):
                return recovered.is_set()

            fb = RecordingBuild()
            d = Daemon(home, deps(fb, kill_path=cleanup, sweep_retry_s=0.05))
            task = asyncio.create_task(d.run())
            caps = await srv.next("caps")
            assert not xcode_advertised(caps)
            cfg = dataclasses.replace(config.load(home), device_name="Renamed Mac")
            if change == "disable":
                cfg = cfg.with_capability("xcode.build", False)
            elif change == "revoke":
                cfg = cfg.with_workspace(dataclasses.replace(cfg.workspace("demo"), schemes=("Other",)), replace_existing=True)
            config.save(home, cfg)
            # The watcher's caps update follows its cancellation checks, so no polling delay can mask them.
            await until(lambda: d._last_caps is not None and d._last_caps.get("name") == "Renamed Mac")
            row = d.store.get(b["job_id"])
            assert row.state == ("queued" if reason is None else "cancelled")
            if reason is not None:
                assert row.error["detail"]["reason"] == reason
            assert not fb.started                       # cleanup still blocks every execution
            recovered.set()
            await until(lambda: d._build_block is None)
            if reason is None:
                await until(lambda: fb.started == [b["job_id"]])
                fb.release(b["job_id"])
                await until(lambda: d.store.get(b["job_id"]).terminal)
                assert d.store.get(b["job_id"]).state == "succeeded"
                assert fb.started == [b["job_id"]]      # preserved job executes exactly once
            else:
                assert not fb.started
        finally:
            if task is not None:
                await stop(task)
            await srv.stop()
    arun(main())


# ------------------------------------------------------------------ HIGH 2
STUB = textwrap.dedent('''\
    import json, os, sys, time
    mode = open(sys.argv[0] + ".mode").read().strip()      # the kill path runs it with a clean environment
    args = sys.argv[1:]
    if args == ["--sweep-only"]:
        print(json.dumps({"v": 1, "status": "swept" if mode != "sweep_fails" else "error"}))
        sys.exit(1 if mode in ("sweep_fails", "sweep_nonzero") else 0)
    if args == ["--cancel"]:
        jid = sys.stdin.readline().strip()
        if mode == "old_dispatcher":
            print(json.dumps({"v": 1, "status": "usage", "message": "usage: bh-dispatch ..."})); sys.exit(1)
        if mode == "hang":
            time.sleep(30)
        if mode == "exit_nonzero":
            print(json.dumps({"status": "cancel_sent"})); sys.exit(2)
        if mode == "garbage":
            print("not json"); sys.exit(0)
        print(json.dumps({"v": 1, "status": "cancel_sent" if mode == "running" else "not_running", "job_id": jid}))
        sys.exit(0)
    print(json.dumps({"v": 1, "status": "usage"})); sys.exit(1)
''')


@pytest.mark.parametrize("mode,ok", [
    ("running", True), ("not_running", True),
    ("old_dispatcher", False), ("exit_nonzero", False), ("garbage", False), ("hang", False), ("sweep_fails", False),
    ("sweep_nonzero", False),
])
def test_kill_path_against_a_stub_of_l3s_dispatcher(tmp_path, monkeypatch, mode, ok):
    stub = tmp_path / "bh-dispatch-stub"
    stub.write_text(STUB)
    (tmp_path / "bh-dispatch-stub.mode").write_text(mode)
    monkeypatch.setattr(daemonmod, "CLEANUP_TIMEOUT_S", 1.0)
    jid = "0b9f3c2e-1d4a-4c5b-8e6f-7a8b9c0d1e2f"
    got = asyncio.run(daemonmod.dispatcher_kill_path([jid], prefix=[sys.executable, str(stub)]))
    assert got is ok


def test_kill_path_with_nothing_running_needs_only_the_sweep(tmp_path, monkeypatch):
    stub = tmp_path / "bh-dispatch-stub"
    stub.write_text(STUB)
    (tmp_path / "bh-dispatch-stub.mode").write_text("old_dispatcher")   # --cancel unsupported; nothing to cancel
    assert asyncio.run(daemonmod.dispatcher_kill_path([], prefix=[sys.executable, str(stub)])) is True


# ------------------------------------------------------------------ HIGH 3
def test_orderly_stop_never_starts_a_queued_job(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = RecordingBuild()

            class SlowCancel(RecordingBuild):
                async def cancel(self, job_id):
                    await asyncio.sleep(0.2)                    # the window in which B used to start
                    await super().cancel(job_id)
            fb = SlowCancel()
            task = await start(home, srv, deps(fb))
            a, ra, sa = build_contract(srv)
            b, rb, sb = build_contract(srv)
            await srv.submit(ra, sa)
            await srv.submit(rb, sb)
            await until(lambda: fb.started == [a["job_id"]])
            await stop(task)
            assert fb.started == [a["job_id"]] and b["job_id"] not in fb.jobs
            st = JobStore(home / "jobs.sqlite3")
            assert st.get(b["job_id"]).state == "queued"
            assert st.get(a["job_id"]).state == "cancelled"
        finally:
            await srv.stop()
    arun(main())


def test_stop_while_a_job_is_at_the_final_gate_does_not_start_it(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = RecordingBuild()
            d = GatedDaemon(home, deps(fb))
            d.gate, d.entered = asyncio.Event(), asyncio.Event()
            task = asyncio.create_task(d.run())
            await srv.next("caps")
            a, ra, sa = build_contract(srv)
            await srv.submit(ra, sa)
            await asyncio.wait_for(d.entered.wait(), 5)
            task.cancel()
            await asyncio.sleep(0.1)
            d.gate.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert fb.jobs == {}
            assert JobStore(home / "jobs.sqlite3").get(a["job_id"]).state == "queued"   # resumes next start
        finally:
            await srv.stop()
    arun(main())


# ------------------------------------------------------------------ MEDIUM 4
def test_stop_has_one_bounded_deadline_even_if_cancel_hangs(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)

            class Hang(FakeBuild):
                async def cancel(self, job_id):
                    await asyncio.sleep(3600)
            fb = Hang()
            task = await start(home, srv, deps(fb, stop_wait_s=0.5))
            a, ra, sa = build_contract(srv)
            await srv.submit(ra, sa)
            await until(lambda: fb.started == [a["job_id"]])
            t0 = time.monotonic()
            await stop(task)
            assert time.monotonic() - t0 < 2.0
        finally:
            await srv.stop()
    arun(main())


def test_stop_deadline_fits_inside_the_launchagent_exit_timeout():
    import plistlib
    exit_timeout = plistlib.loads(service.plist_bytes(os.getcwd() and __import__("pathlib").Path("/x"),
                                                      ["/x/python"]))["ExitTimeOut"]
    assert daemonmod.Deps(executors={}, probes=None, host=None).stop_wait_s <= exit_timeout - 20


# ------------------------------------------------------------------ MEDIUM 5
def test_refused_request_cannot_be_replayed_after_pruning(tmp_path):
    from test_checks import Env
    e = Env(tmp_path)
    e.enabled = frozenset()
    c = H.contract(action="system.info")
    raw, sig = H.sign_contract(c)
    assert e.run(raw, sig).code == "helper_unavailable"
    e.store.prune(now=c["expires_at"] + 120_000)                       # bytes may go; the tombstone stays
    row = e.store.get(c["job_id"])
    assert row is not None and row.contract == b"" and row.nonce == c["nonce"]
    claim = e.store.claim(job_id=c["job_id"], nonce=c["nonce"], contract_hash=H.chash(raw), contract=raw,
                          action="system.info", workspace_id=None, expires_at=c["expires_at"], now=H.NOW)
    assert isinstance(claim, Duplicate)
    other = e.store.claim(job_id="99999999-9999-4999-8999-999999999999", nonce=c["nonce"], contract_hash="x",
                          contract=b"", action="system.info", workspace_id=None, expires_at=1, now=H.NOW)
    assert isinstance(other, Replay)
    e.store.prune(now=row.updated_at + ROW_KEEP_MS + 1)                 # only after the full retention
    assert e.store.get(c["job_id"]) is None


def test_tombstones_are_bounded_by_refusing_not_by_forgetting(tmp_path):
    from test_checks import Env
    e = Env(tmp_path)
    e.enabled = frozenset()
    e.store.max_tombstones = 4
    e.store.max_recent_rejected = 10**6
    codes = [e.run(*H.sign_contract(H.contract(action="system.info"))).code for _ in range(6)]
    assert codes == ["helper_unavailable"] * 4 + ["helper_busy"] * 2
    assert len(e.store.recent(100)) == 4


def test_stop_does_not_even_dequeue_the_queued_job(tmp_path):
    """Holding the lanes is the first line: B is never dequeued (never moved to building), not merely reverted."""
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            moves = []

            class Recording(Daemon):
                async def _transition(self, job_id, state):
                    moves.append((job_id, state))
                    await super()._transition(job_id, state)

            class SlowCancel(RecordingBuild):
                async def cancel(self, job_id):
                    await asyncio.sleep(0.2)
                    await super().cancel(job_id)
            fb = SlowCancel()
            task = asyncio.create_task(Recording(home, deps(fb)).run())
            await srv.next("caps")
            a, ra, sa = build_contract(srv)
            b, rb, sb = build_contract(srv)
            await srv.submit(ra, sa)
            await srv.submit(rb, sb)
            await until(lambda: fb.started == [a["job_id"]])
            await stop(task)
            assert (b["job_id"], "building") not in moves
        finally:
            await srv.stop()
    arun(main())


def test_builds_blocked_while_a_job_is_at_the_final_gate_put_it_back(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = RecordingBuild()
            d = GatedDaemon(home, deps(fb))
            d.gate, d.entered = asyncio.Event(), asyncio.Event()
            task = asyncio.create_task(d.run())
            await srv.next("caps")
            a, ra, sa = build_contract(srv)
            await srv.submit(ra, sa)
            await asyncio.wait_for(d.entered.wait(), 5)
            d._block_builds("build_cleanup_pending")
            d.gate.set()
            await asyncio.sleep(0.3)
            assert fb.jobs == {}
            assert JobStore(home / "jobs.sqlite3").get(a["job_id"]).state == "queued"
            assert d.build.q.queued_ids() == [a["job_id"]]           # back at the head, resumes after the cleanup
            await stop(task)
        finally:
            await srv.stop()
    arun(main())
