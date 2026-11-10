"""Fix round 1 (adversarial review of the helper): each probe reproduced as a test.

HIGH 1 grant edits vs queued jobs; HIGH 2 revoke between dequeue and executor start; HIGH 3 kill path on start
and executor cleanup on stop; HIGH 4 contract size and rejected-row bounds; MEDIUM 5 one confirm dialog at a time,
killed on cancel; MEDIUM 6 LaunchAgent interpreter path; LOW 7 transports always closed; plus the dequeue
allowlist mutation survivor and the sudoers --sweep-only / --cancel lines."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import helpers as H
from fake_server import FakeServer
from test_daemon import (FAST, FakeBuild, arun, bhproto_now, build_contract, deps, pair_home, start, stop, until)
from borrowed_hands_helper import checks, config, connection, daemon as daemonmod, service
from borrowed_hands_helper._shared import bhproto
from borrowed_hands_helper.daemon import Daemon
from borrowed_hands_helper.jobstore import ROW_KEEP_MS, JobStore

REPO = Path(__file__).resolve().parents[2]


def edit_grant(home, **changes):
    cfg = config.load(home)
    ws = dataclasses.replace(cfg.workspace("demo"), **changes)
    config.save(home, cfg.with_workspace(ws, replace_existing=True))
    os.utime(home / config.CONFIG_NAME, ns=(time.time_ns(), time.time_ns() + 10**9))


async def two_builds(srv, home, fb):
    """Job a runs (blocked in the fake executor), job b waits in the queue."""
    a, ra, sa = build_contract(srv)
    b, rb, sb = build_contract(srv)
    await srv.submit(ra, sa)
    await srv.submit(rb, sb)
    await until(lambda: fb.started == [a["job_id"]])
    return a, b


async def results_for(srv, ids, n_max=20):
    out = {}
    for _ in range(n_max):
        r = (await srv.next("job.result"))["body"]
        if r["job_id"] in ids:
            out[r["job_id"]] = r
        if len(out) == len(ids):
            return out
    raise AssertionError(f"missing results: {set(ids) - set(out)}")


class RecordingBuild(FakeBuild):
    def __init__(self):
        super().__init__()
        self.jobs = {}

    async def run(self, job, emit):
        self.jobs[job.job_id] = job
        return await super().run(job, emit)


# ------------------------------------------------------------------ HIGH 1 + mutation survivor
@pytest.mark.parametrize("changes,reason", [
    (dict(project="Replacement.xcodeproj"), "grant_changed"),
    (dict(confirm=True), "grant_changed"),
    (dict(schemes=("Other",)), "grant_revoked"),
    (dict(configurations=("Release",)), "grant_revoked"),
    (dict(destinations=("generic/platform=iOS Simulator",)), "grant_revoked"),
    (dict(remote_url="https://github.com/someone/else.git"), "grant_revoked"),
    (dict(remote_url="https://GitHub.com/alext8900/bh-demo/"), "grant_changed"),   # same repo, other fetch URL
])
def test_queued_job_is_cancelled_when_its_grant_changes(tmp_path, changes, reason):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = RecordingBuild()
            task = await start(home, srv, deps(fb))
            a, b = await two_builds(srv, home, fb)
            edit_grant(home, **changes)
            await asyncio.sleep(0.2)
            fb.release(a["job_id"])
            res = await results_for(srv, {b["job_id"]})
            assert res[b["job_id"]]["state"] == "cancelled"
            assert res[b["job_id"]]["error"]["detail"]["reason"] == reason
            assert b["job_id"] not in fb.jobs                      # the executor never saw it
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_queued_job_runs_with_the_current_grant_after_an_unrelated_edit(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = RecordingBuild()
            task = await start(home, srv, deps(fb))
            a, b = await two_builds(srv, home, fb)
            edit_grant(home, schemes=("Demo", "Extra"), confirm=False)       # widening, nothing newly required
            await asyncio.sleep(0.2)
            fb.release(a["job_id"])
            await until(lambda: b["job_id"] in fb.jobs)
            assert fb.jobs[b["job_id"]].workspace.schemes == ("Demo", "Extra")
            fb.release(b["job_id"])
            res = await results_for(srv, {b["job_id"]})
            assert res[b["job_id"]]["state"] == "succeeded"
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_confirm_turned_off_does_not_cancel(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            edit_grant(home, confirm=True)
            fb = RecordingBuild()

            async def allow(summary):
                return True
            task = await start(home, srv, deps(fb, confirm=allow))
            a, b = await two_builds(srv, home, fb)
            edit_grant(home, confirm=False)
            await asyncio.sleep(0.2)
            fb.release(a["job_id"])
            await until(lambda: b["job_id"] in fb.jobs)
            assert fb.jobs[b["job_id"]].workspace.confirm is False
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


# ------------------------------------------------------------------ HIGH 2
class GatedDaemon(Daemon):
    """Blocks inside the await between dequeue authorization and the executor start."""
    gate: asyncio.Event
    entered: asyncio.Event

    async def _transition(self, job_id, state):
        await super()._transition(job_id, state)
        if state == "building":
            self.entered.set()
            await self.gate.wait()


def test_revoke_between_dequeue_and_executor_start_never_starts_it(tmp_path):
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
            config.save(home, config.load(home).without_workspace("demo"))
            os.utime(home / config.CONFIG_NAME, ns=(time.time_ns(), time.time_ns() + 10**9))
            await asyncio.sleep(0.3)                               # the watcher cancels while we are gated
            d.gate.set()
            res = await results_for(srv, {a["job_id"]})
            assert res[a["job_id"]]["state"] == "cancelled"
            assert res[a["job_id"]]["error"]["detail"]["reason"] == "grant_revoked"
            assert fb.jobs == {}                                   # ex.run never started
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_bh_cancel_between_dequeue_and_executor_start(tmp_path):
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
            await srv.send("job.cancel", {"job_id": a["job_id"]})
            await srv.next("ack")
            d.gate.set()
            res = await results_for(srv, {a["job_id"]})
            assert res[a["job_id"]]["state"] == "cancelled" and fb.jobs == {}
            assert res[a["job_id"]]["error"]["detail"]["reason"] == "cancelled_by_agent"
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_persisted_cancel_flag_alone_stops_the_start(tmp_path):
    """`bh cancel` writes the flag; even before the watcher routes it, the final gate honours it."""
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = RecordingBuild()
            d = GatedDaemon(home, deps(fb, config_poll_s=60))
            d.gate, d.entered = asyncio.Event(), asyncio.Event()
            task = asyncio.create_task(d.run())
            await srv.next("caps")
            a, ra, sa = build_contract(srv)
            await srv.submit(ra, sa)
            await asyncio.wait_for(d.entered.wait(), 5)
            assert JobStore(home / "jobs.sqlite3").request_cancel(a["job_id"])
            d.gate.set()
            res = await results_for(srv, {a["job_id"]})
            assert res[a["job_id"]]["state"] == "cancelled" and fb.jobs == {}
            assert res[a["job_id"]]["error"]["detail"]["reason"] == "cancelled_on_mac"
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


# ------------------------------------------------------------------ HIGH 3
def test_start_runs_the_kill_path_before_recovery_and_before_connecting(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = FakeBuild()
            task = await start(home, srv, deps(fb))
            a, ra, sa = build_contract(srv)
            await srv.submit(ra, sa)
            await until(lambda: fb.started == [a["job_id"]])
            task.cancel()                                         # simulate a crash: no orderly cleanup
            d_ = task
            try:
                await d_
            except asyncio.CancelledError:
                pass
            # make the row look like a crash left it running
            st = JobStore(home / "jobs.sqlite3")
            st._db.execute("UPDATE jobs SET state='building' WHERE job_id=?", (a["job_id"],))
            calls = []

            async def kill_path(running):
                calls.append(("kill", list(running), srv.auths, JobStore(home / "jobs.sqlite3").get(a["job_id"]).state))
                return True
            auths_before = srv.auths
            task = asyncio.create_task(Daemon(home, deps(FakeBuild(), kill_path=kill_path)).run())
            await until(lambda: srv.auths > auths_before)
            assert calls == [("kill", [a["job_id"]], auths_before, "building")]
            row = JobStore(home / "jobs.sqlite3").get(a["job_id"])
            assert row.state == "failed" and row.error["detail"]["reason"] == "helper_restarted"
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_orderly_stop_awaits_executor_cancel(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = FakeBuild()
            task = await start(home, srv, deps(fb))
            a, ra, sa = build_contract(srv)
            await srv.submit(ra, sa)
            await until(lambda: fb.started == [a["job_id"]])
            await stop(task)
            assert fb.cancelled == [a["job_id"]]
            row = JobStore(home / "jobs.sqlite3").get(a["job_id"])
            assert row.state == "cancelled" and row.error["detail"]["reason"] == "helper_stopping"
        finally:
            await srv.stop()
    arun(main())


def test_default_kill_path_argv(monkeypatch):
    calls = []

    async def fake_exec(argv, stdin_bytes, timeout):
        calls.append((argv, stdin_bytes))
        if "--sweep-only" in argv:
            return 0, '{"v": 1, "status": "swept", "sweep_ok": true}\n'
        return 0, '{"status": "cancel_sent"}\n'
    monkeypatch.setattr(daemonmod, "_exec_capture", fake_exec)
    jid = "0b9f3c2e-1d4a-4c5b-8e6f-7a8b9c0d1e2f"
    assert asyncio.run(daemonmod.dispatcher_kill_path([jid])) is True
    assert calls == [(["/usr/bin/sudo", "-n", "/opt/borrowed-hands/bin/bh-dispatch", "--cancel"], (jid + "\n").encode()),
                     (["/usr/bin/sudo", "-n", "/opt/borrowed-hands/bin/bh-dispatch", "--sweep-only"], b"")]


def test_default_kill_path_reports_a_failed_sweep(monkeypatch):
    async def fake_exec(argv, stdin_bytes, timeout):
        return 1, '{"status": "error"}\n'
    monkeypatch.setattr(daemonmod, "_exec_capture", fake_exec)
    assert asyncio.run(daemonmod.dispatcher_kill_path([])) is False


def test_sudoers_has_exact_sweep_and_cancel_lines():
    from borrowed_hands_helper import setup
    text = setup.sudoers_text(501, config.Policy())
    D = "#501 ALL=(root) NOPASSWD: /opt/borrowed-hands/bin/bh-dispatch"
    assert f"{D} --sweep-only" in text.splitlines() and f"{D} --cancel" in text.splitlines()
    assert len(text.splitlines()) == 6


# ------------------------------------------------------------------ HIGH 4
def test_oversized_contract_is_refused_before_anything_is_stored(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            task = await start(home, srv, deps())
            c, raw, sig = srv.contract(srv.helper_id, "system.info", approval_ref="none")
            big = dict(c)
            big["params"] = {}
            pad = bhproto.canonical(big)
            # a validly signed contract padded far past the cap (unknown key: would fail schema anyway)
            padded = pad[:-1] + b',"zz":"' + b"x" * 3_700_000 + b'"}'
            await srv.submit(padded, bhproto.sign(srv.priv, bhproto.CTX_JOB, padded))
            err = (await srv.next("error"))["body"]
            assert err["code"] == "invalid_arguments" and err["detail"]["reason"] == "too_large"
            assert JobStore(home / "jobs.sqlite3").recent(100) == []
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_oversized_frame_is_refused_before_decoding_or_checking(tmp_path, monkeypatch):
    called = []
    real = checks.verify_contract

    async def spy(*a, **kw):
        called.append(1)
        return await real(*a, **kw)
    monkeypatch.setattr(checks, "verify_contract", spy)

    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            task = await start(home, srv, deps())
            await srv.send("job.submit", {"contract": "A" * 40000, "sig": "A" * 86})
            err = (await srv.next("error"))["body"]
            assert err["detail"]["reason"] == "too_large" and called == []
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_contract_cap_in_checks_after_signature(tmp_path):
    from test_checks import Env
    e = Env(tmp_path)
    c = H.contract(action="system.info")
    raw = bhproto.canonical(c)
    raw = raw[:-1] + b',"zz":"' + b"x" * 20000 + b'"}'
    out = e.run(raw, bhproto.sign(H.SERVER_PRIV, bhproto.CTX_JOB, raw))
    assert out.code == "invalid_arguments" and out.detail["reason"] == "too_large"
    bad = e.run(raw, bhproto.sign(H.OTHER_PRIV, bhproto.CTX_JOB, raw))
    assert bad.code == "denied"                                      # signature still checked first
    assert e.store.recent(10) == []


def test_rejected_rows_keep_no_contract_bytes_and_stay_as_tombstones(tmp_path):
    from test_checks import Env
    e = Env(tmp_path)
    e.enabled = frozenset()
    c = H.contract(action="system.info")
    raw, sig = H.sign_contract(c)
    assert e.run(raw, sig).code == "helper_unavailable"
    row = e.store.get(c["job_id"])
    assert row.state == "failed" and row.contract == b""
    assert isinstance(e.run(raw, sig), checks.Duplicate)              # still blocks a replay while unexpired
    e.store.prune(now=c["expires_at"] + 60_001)
    assert e.store.get(c["job_id"]) is not None                       # tombstone kept (fix round 2, MEDIUM 5)


def test_unaccepted_rows_are_bounded(tmp_path):
    from test_checks import Env
    e = Env(tmp_path)
    e.enabled = frozenset()
    e.store.max_recent_rejected = 5
    codes = []
    for _ in range(8):
        raw, sig = H.sign_contract(H.contract(action="system.info"))
        codes.append(e.run(raw, sig).code)
    assert codes[:5] == ["helper_unavailable"] * 5
    assert codes[5:] == ["helper_busy"] * 3
    assert len(e.store.recent(100)) == 5


def test_daemon_prunes_periodically(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            task = await start(home, srv, deps(prune_every_s=0.2))          # the start-up prune has run
            st = JobStore(home / "jobs.sqlite3")
            old = bhproto_now() - ROW_KEEP_MS - 60_000                      # past the full retention
            st.claim(job_id="22222222-2222-4222-8222-222222222222", nonce="n", contract_hash="h", contract=b"",
                     action="system.info", workspace_id=None, expires_at=old + 300_000, now=old)
            st.finish_rejected("22222222-2222-4222-8222-222222222222", {"code": "denied", "message": "x"}, old)
            await until(lambda: st.get("22222222-2222-4222-8222-222222222222") is None, timeout=5)
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


# ------------------------------------------------------------------ MEDIUM 5
def test_one_confirm_dialog_at_a_time_and_cancel_kills_it(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            edit_grant(home, confirm=True)
            shown, killed = [], []

            async def dialog(summary):
                shown.append(summary["job_id"])
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    killed.append(summary["job_id"])
                    raise
            fb = RecordingBuild()
            task = await start(home, srv, deps(fb, confirm=dialog))
            a, ra, sa = build_contract(srv)
            await srv.submit(ra, sa)
            await until(lambda: shown == [a["job_id"]])
            for _ in range(5):
                await srv.submit(*build_contract(srv)[1:])
                err = (await srv.next("error"))["body"]
                assert err["code"] == "helper_busy" and err["detail"]["reason"] == "confirm_pending"
            assert shown == [a["job_id"]]
            await srv.send("job.cancel", {"job_id": a["job_id"]})
            err = (await srv.next("error"))["body"]
            assert err["code"] == "cancelled"
            assert killed == [a["job_id"]] and fb.jobs == {}
            assert JobStore(home / "jobs.sqlite3").get(a["job_id"]).state == "cancelled"
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_osascript_process_is_killed_and_reaped_on_cancel(tmp_path):
    fake = tmp_path / "osascript"
    fake.write_text("#!/bin/sh\nexec sleep 60\n")
    fake.chmod(0o755)
    procs = []

    async def main():
        t = asyncio.create_task(daemonmod.osascript_confirm(
            {"action": "xcode.build", "workspace": "demo", "scheme": "Demo", "configuration": "Debug",
             "destination": "platform=macOS,arch=arm64", "commit": "a" * 40, "approval_ref": "none", "job_id": "x"},
            osascript=str(fake), on_spawn=procs.append))
        await until(lambda: procs)
        t0 = time.monotonic()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert procs[0].returncode is not None and procs[0].returncode < 0      # killed by a signal, and reaped
        assert time.monotonic() - t0 < 5
    asyncio.run(main())


# ------------------------------------------------------------------ MEDIUM 6
def test_service_program_stays_inside_the_venv():
    prog = service.program()
    assert prog[0].startswith(sys.prefix) and prog[1:] == ["-I", "-m", "borrowed_hands_helper", "run"]
    assert Path(prog[0]).exists()


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_service_program_from_a_real_uv_tool_install(tmp_path):
    env = dict(os.environ, UV_TOOL_DIR=str(tmp_path / "tools"), UV_TOOL_BIN_DIR=str(tmp_path / "bin"))
    r = subprocess.run(["uv", "tool", "install", "--quiet", str(REPO / "helper")], env=env, capture_output=True,
                       text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    py = tmp_path / "tools" / "borrowed-hands-helper" / "bin" / "python"
    prog = subprocess.run([str(py), "-I", "-c", "import json; from borrowed_hands_helper import service; print(json.dumps(service.program()))"],
                          capture_output=True, text=True, timeout=60)
    argv = json.loads(prog.stdout)
    assert argv[0].startswith(str(tmp_path / "tools")), argv
    out = subprocess.run(argv[:-1] + ["--version"], capture_output=True, text=True, timeout=60,
                         env={"PATH": "/usr/bin:/bin"})
    assert out.returncode == 0 and out.stdout.strip() == "bh 0.2.1", out.stderr


# ------------------------------------------------------------------ LOW 7
class CountingTransport:
    def __init__(self, inner):
        self.inner, self.closed = inner, 0

    async def send(self, t):
        await self.inner.send(t)

    async def recv(self):
        return await self.inner.recv()

    async def close(self, code=1000):
        self.closed += 1
        await self.inner.close(code)


def test_run_forever_closes_every_transport(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            cfg = config.load(home)
            from borrowed_hands_helper import keyfile
            priv = keyfile.load_device_key(home)
            made = []

            async def connect(url, timing):
                t = CountingTransport(await connection.AiohttpTransport.connect(url, timing))
                made.append(t)
                return t
            n = {"sessions": 0}

            async def on_session(session, t):
                n["sessions"] += 1
                if n["sessions"] == 1:
                    return                                         # a normal end (e.g. after server.rotate)
                raise connection.Stop(1, "done")
            stop_ = await connection.run_forever(lambda: cfg.server, lambda: priv, on_session, timing=FAST,
                                                 connect=connect)
            assert stop_.message == "done" and len(made) == 2 and all(t.closed >= 1 for t in made)
        finally:
            await srv.stop()
    arun(main())


def test_the_suite_cannot_run_sudo():
    with pytest.raises(AssertionError, match="tried to run sudo"):
        asyncio.run(daemonmod.dispatcher_kill_path([]))
    with pytest.raises(AssertionError, match="tried to run sudo"):
        subprocess.run(["/usr/bin/sudo", "-n", "true"])
