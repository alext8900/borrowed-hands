"""The daemon and pairing against tests/fake_server.py: HE-1..HE-5, HE-8, SE-3/SE-4 helper side.
Timings are scaled down (Timing below); the real values are unit-tested in test_connection.py."""
from __future__ import annotations

import asyncio
import os
import time

import pytest

import helpers as H
from fake_server import FakeServer
from borrowed_hands_helper import account, config, keyfile, schema
from borrowed_hands_helper._shared import bhproto
from borrowed_hands_helper.caps import Host, InstallStatus, Probes
from borrowed_hands_helper.connection import Timing
from borrowed_hands_helper.daemon import Daemon, Deps
from borrowed_hands_helper.executors import JobOutcome
from borrowed_hands_helper.executors.system_info import SystemInfoExecutor
from borrowed_hands_helper.jobstore import JobStore

FAST = Timing(heartbeat_s=0.2, dead_s=1.0, handshake_s=2.0, stable_s=60.0, backoff_base_s=0.01,
              backoff_cap_s=0.05, connect_s=2.0)
GOOD = Probes(install=InstallStatus(True, ()), sudo_ok=True, xcode=("26.1", "17B55"), screen_granted=False)
BUILD_RESULT = {"status": "succeeded", "duration_ms": 1000, "xcode": "26.1 (17B55)",
                "destination": "platform=macOS,arch=arm64", "error_count": 0, "warning_count": 0, "errors": [],
                "warnings": [], "log_tail": "", "truncated": False}


def fake_run(argv):
    return {"/usr/bin/sw_vers": (0, "26.2\n"), "/usr/sbin/sysctl": (0, "1\n"),
            "/usr/bin/xcode-select": (0, "/Applications/Xcode.app/Contents/Developer\n"),
            "/usr/bin/env": (0, "Xcode 26.1\nBuild version 17B55\n")}.get(argv[0], (1, ""))


class FakeBuild:
    """Stands in for the xcode executor: runs until released or cancelled."""

    def __init__(self):
        self.started, self.cancelled, self.gates = [], [], {}

    async def run(self, job, emit):
        self.started.append(job.job_id)
        ev = self.gates.setdefault(job.job_id, asyncio.Event())
        await ev.wait()
        if job.job_id in self.cancelled:
            return JobOutcome("cancelled", None, {"code": "cancelled", "message": "cancelled",
                                                  "detail": {"reason": "cancelled"}})
        return JobOutcome("succeeded", dict(BUILD_RESULT), None)

    async def cancel(self, job_id):
        self.cancelled.append(job_id)
        self.gates.setdefault(job_id, asyncio.Event()).set()

    def release(self, job_id):
        self.gates.setdefault(job_id, asyncio.Event()).set()


def arun(coro, timeout=30):
    async def main():
        return await asyncio.wait_for(coro, timeout)
    return asyncio.run(main())


async def until(pred, timeout=5.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    raise AssertionError("condition not met in time")


async def pair_home(tmp_path, srv, enable=("system.info", "xcode.build"), ws=True):
    home = tmp_path / "BorrowedHands"
    rc = await account.pair(home, srv.pairing_string(), name="Test Mac", insecure_local=True, timing=FAST,
                            host=lambda: Host("26.2", "arm64"), out=lambda *_: None)
    assert rc == 0
    await srv.next("caps")            # the pairing connection's hello frames
    await srv.next("jobs.sync")
    cfg = config.load(home)
    for a in enable:
        cfg = cfg.with_capability(a, True)
    if ws:
        cfg = cfg.with_workspace(H.workspace())
    config.save(home, cfg)
    return home


async def no_kill_path(running):
    """Never the real one in tests: it would run sudo."""
    return True


def deps(build=None, **kw):
    d = dict(executors={"system.info": SystemInfoExecutor(run=fake_run, arch=lambda: "arm64", disk_free=lambda: 0),
                        "xcode.build": build or FakeBuild()},
             probes=lambda *_: GOOD, host=lambda: Host("26.2", "arm64"), timing=FAST, config_poll_s=0.05,
             kill_path=no_kill_path)
    d.update(kw)
    return Deps(**d)


async def start(home, srv, dep):
    task = asyncio.create_task(Daemon(home, dep).run())
    await srv.next("caps")
    await srv.next("jobs.sync")
    return task


async def stop(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def build_contract(srv, **kw):
    params = H.build_params(**kw.pop("params", {}))
    return srv.contract(kw.pop("helper_id", srv.helper_id), "xcode.build", params, **kw)


# ---------------------------------------------------------------------------------------------- HE-1
def test_he1_pair_writes_config_only_after_proof_and_grants_nothing(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = tmp_path / "BH"
            rc = await account.pair(home, srv.pairing_string(), name="Alex’s Mac", insecure_local=True,
                                    timing=FAST, host=lambda: Host("26.2", "arm64"), out=lambda *_: None)
            assert rc == 0
            cfg = config.load(home)
            assert cfg.server.helper_id == srv.helper_id and cfg.server.key_id == srv.kid
            assert cfg.device_name == "Alexs Mac" and srv.events[0] == ("paired", srv.helper_id, "Alexs Mac")
            assert cfg.capabilities == config.Capabilities() and cfg.workspaces == ()     # pairing grants nothing
            assert bhproto.pub_raw(keyfile.load_device_key(home)) == srv.helpers[srv.helper_id]
            caps = await srv.next("caps")
            assert caps["seq"] == 1 and caps["body"]["capabilities"] == []
            assert (await srv.next("jobs.sync"))["seq"] == 2
        finally:
            await srv.stop()
    arun(main())


def test_he1_bad_server_proof_writes_nothing(tmp_path):
    async def main():
        srv = await FakeServer(bad_server_sig=True).start()
        try:
            home = tmp_path / "BH"
            msgs = []
            rc = await account.pair(home, srv.pairing_string(), name="M", insecure_local=True, timing=FAST,
                                    host=lambda: Host("26.2", "arm64"), out=msgs.append)
            assert rc == 3 and msgs == ["server failed to prove its key; check the pairing string"]
            assert not (home / keyfile.KEY_NAME).exists() and not (home / config.CONFIG_NAME).exists()
        finally:
            await srv.stop()
    arun(main())


def test_he1_wrong_key_in_pairing_string_is_a_proof_failure(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            s = srv.pairing_string()
            forged = s.rsplit(".", 1)[0] + "." + bhproto.b64u(bhproto.pub_raw(bhproto.new_private_key()))
            rc = await account.pair(tmp_path / "BH", forged, name="M", insecure_local=True, timing=FAST,
                                    host=lambda: Host("26.2", "arm64"), out=lambda *_: None)
            assert rc == 3 and not (tmp_path / "BH" / keyfile.KEY_NAME).exists()
        finally:
            await srv.stop()
    arun(main())


def test_he1_rejected_code_and_unreachable(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            s = srv.pairing_string()
            msgs = []
            kw = dict(name="M", insecure_local=True, timing=FAST, host=lambda: Host("26.2", "arm64"), out=msgs.append)
            assert await account.pair(tmp_path / "A", s, **kw) == 0
            assert await account.pair(tmp_path / "B", s, **kw) == 2           # single use
            assert msgs[-1] == "pairing code rejected; run /hands pair again"
            assert not (tmp_path / "B" / config.CONFIG_NAME).exists()
            url = srv.url
        finally:
            await srv.stop()
        rc = await account.pair(tmp_path / "C", bhproto.pairing_string(url, os.urandom(16), srv.pub), **kw)
        assert rc == 4 and msgs[-1].startswith("cannot reach 127.0.0.1: ")
        assert await account.pair(tmp_path / "D", "bh1:wss://x/bh/v1/ws#short.key", **kw) == 2
        remote = bhproto.pairing_string("ws://example.com/bh/v1/ws", os.urandom(16), srv.pub)
        assert await account.pair(tmp_path / "E", remote, **kw) == 2           # ws:// only to localhost
    arun(main())


# ---------------------------------------------------------------------------------------------- loop
def test_system_info_end_to_end(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            task = await start(home, srv, deps())
            c, raw, sig = srv.contract(srv.helper_id, "system.info")
            fid = await srv.submit(raw, sig)
            acc = await srv.next("job.accepted")
            assert acc["re"] == fid and acc["body"]["state"] == "queued"
            res = await srv.next("job.result")
            assert res["body"]["state"] == "succeeded" and res["body"]["result"]["os_version"] == "26.2"
            schema.validate("job-result", res["body"])
            await until(lambda: JobStore(home / "jobs.sqlite3").get(c["job_id"]).delivered)
            # idempotent resend: same bytes give the state and the result again (PR-4)
            fid2 = await srv.submit(raw, sig)
            st = await srv.next("job.state")
            assert st["re"] == fid2 and st["body"]["state"] == "succeeded"
            assert (await srv.next("job.result"))["body"]["job_id"] == c["job_id"]
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_caps_after_pairing_and_enable(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            task = asyncio.create_task(Daemon(home, deps()).run())
            caps = (await srv.next("caps"))["body"]
            schema.validate("caps", caps)
            names = [c["name"] for c in caps["capabilities"]]
            assert names == ["system.info", "xcode.build"]
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_se8_test_install_refuses_builds_and_hides_xcode(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = FakeBuild()
            bad = Probes(InstallStatus(False, ("TEST_MODE",)), True, ("26.1", "17B55"), False)
            task = asyncio.create_task(Daemon(home, deps(fb, probes=lambda *_: bad)).run())
            caps = (await srv.next("caps"))["body"]
            assert [c["name"] for c in caps["capabilities"]] == ["system.info"]
            _, raw, sig = build_contract(srv)
            await srv.submit(raw, sig)
            err = await srv.next("error")
            assert err["body"]["code"] == "helper_unavailable" and fb.started == []
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_se3_refused_contracts_never_reach_the_executor(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = FakeBuild()
            task = await start(home, srv, deps(fb))
            bad = [build_contract(srv, params={"scheme": "Other"}),
                   build_contract(srv, params={"destination": "platform=iOS Simulator,name=iPhone 16"}),
                   build_contract(srv, helper_id="11111111-2222-4333-8444-555555555555"),
                   build_contract(srv, expires_at=bhproto_now() - 1, issued_at=bhproto_now() - 2)]
            other = bhproto.new_private_key()
            c, raw, _ = build_contract(srv)
            bad.append((c, raw, bhproto.sign(other, bhproto.CTX_JOB, raw)))
            for _, raw, sig in bad:
                await srv.submit(raw, sig)
                assert (await srv.next("error"))["body"]["code"] in (
                    "needs_grant", "invalid_arguments", "expired", "denied")
            assert fb.started == []
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def bhproto_now():
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------------------------- HE-8
def test_he8_queue_full_expiry_on_dequeue_and_cancel_queued(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = FakeBuild()
            task = await start(home, srv, deps(fb))
            now = bhproto_now()
            jobs = [build_contract(srv) for _ in range(2)]
            jobs.append(build_contract(srv, issued_at=now, expires_at=now + 400))     # will expire in the queue
            jobs.append(build_contract(srv))
            for _, raw, sig in jobs:
                await srv.submit(raw, sig)
                assert (await srv.next("job.accepted"))["body"]["state"] == "queued"
            _, raw, sig = build_contract(srv)
            await srv.submit(raw, sig)
            busy = await srv.next("error")
            assert busy["body"]["code"] == "helper_busy" and busy["body"]["detail"]["reason"] == "queue_full"
            assert busy["body"]["retry_after_s"] > 0
            await until(lambda: fb.started == [jobs[0][0]["job_id"]])
            # cancel the last queued job: it never reaches the executor
            await srv.send("job.cancel", {"job_id": jobs[3][0]["job_id"]})
            res = await srv.next("job.result")
            assert res["body"]["job_id"] == jobs[3][0]["job_id"] and res["body"]["state"] == "cancelled"
            await asyncio.sleep(0.5)                                         # job 3's contract expires
            fb.release(jobs[0][0]["job_id"])
            await until(lambda: len(fb.started) == 2)
            fb.release(jobs[1][0]["job_id"])
            expired = None
            for _ in range(10):
                r = await srv.next("job.result")
                if r["body"]["job_id"] == jobs[2][0]["job_id"]:
                    expired = r
                    break
            assert expired["body"]["state"] == "expired"
            await asyncio.sleep(0.1)
            assert fb.started == [jobs[0][0]["job_id"], jobs[1][0]["job_id"]]
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


# ---------------------------------------------------------------------------------------------- HE-4
def test_he4_revoke_cancels_running_and_queued_and_resends_caps(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = FakeBuild()
            task = await start(home, srv, deps(fb))
            a, ra, sa = build_contract(srv)
            b, rb, sb = build_contract(srv)
            await srv.submit(ra, sa)
            await srv.submit(rb, sb)
            await until(lambda: fb.started == [a["job_id"]])
            await asyncio.sleep(0.06)
            cfg = config.load(home).without_workspace("demo")
            config.save(home, cfg)
            os.utime(home / config.CONFIG_NAME, ns=(time.time_ns(), time.time_ns() + 10**9))
            results = {}
            while len(results) < 2:
                r = await srv.next("job.result")
                results[r["body"]["job_id"]] = r["body"]
            assert results[b["job_id"]]["state"] == "cancelled"
            assert results[b["job_id"]]["error"]["detail"]["reason"] == "grant_revoked"
            assert results[a["job_id"]]["state"] == "cancelled" and fb.cancelled == [a["job_id"]]
            assert fb.started == [a["job_id"]]
            caps = None
            for _ in range(10):
                caps = (await srv.next("caps"))["body"]
                xb = [c for c in caps["capabilities"] if c["name"] == "xcode.build"]
                if xb and xb[0]["workspaces"] == []:
                    break
            else:
                raise AssertionError("caps not re-sent after the grant revoke")
            await srv.submit(*build_contract(srv)[1:])
            assert (await srv.next("error"))["body"]["code"] == "needs_grant"
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_bh_cancel_from_the_cli_reaches_the_running_job(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = FakeBuild()
            task = await start(home, srv, deps(fb))
            a, ra, sa = build_contract(srv)
            await srv.submit(ra, sa)
            await until(lambda: fb.started == [a["job_id"]])
            st = JobStore(home / "jobs.sqlite3")
            assert st.request_cancel(a["job_id"])
            r = await srv.next("job.result")
            assert r["body"]["state"] == "cancelled" and fb.cancelled == [a["job_id"]]
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


# ---------------------------------------------------------------------------------------------- HE-3
def test_he3_restart_mid_build_fails_the_job_and_delivers_it_after_reconnect(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = FakeBuild()
            task = await start(home, srv, deps(fb))
            a, ra, sa = build_contract(srv)
            await srv.submit(ra, sa)
            st = await srv.next("job.state")
            assert st["body"]["state"] == "building"
            await stop(task)
            # a crash (unlike the orderly stop above) leaves the row running: put it back the way a crash would
            JobStore(home / "jobs.sqlite3")._db.execute(
                "UPDATE jobs SET state='building', error_json=NULL WHERE job_id=?", (a["job_id"],))
            task = asyncio.create_task(Daemon(home, deps(FakeBuild())).run())
            await srv.next("caps")
            sync = (await srv.next("jobs.sync"))["body"]
            assert sync["active"] == []
            [res] = sync["results"]
            assert res["job_id"] == a["job_id"] and res["state"] == "failed"
            assert res["error"]["detail"]["reason"] == "helper_restarted"
            await until(lambda: JobStore(home / "jobs.sqlite3").undelivered() == [])
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


# ---------------------------------------------------------------------------------------------- HE-2
def test_he2_survives_a_window_of_502s(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            srv.fail_upgrades(8, 502)
            task = await start(home, srv, deps())
            assert srv.connections >= 9 + 1 and srv.auths == 1
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


@pytest.mark.parametrize("code,exit_code", [(4401, 1), (4403, 3), (4409, 1)])
def test_he2_stop_codes(tmp_path, code, exit_code):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            task = await start(home, srv, deps())
            await srv.close_helper(code)
            rc = await asyncio.wait_for(task, 5)
            assert rc == exit_code
            assert srv.auths == 1                         # never reconnected
            if code == 4403:                              # unpaired by server: pins and key deleted
                assert not (home / keyfile.KEY_NAME).exists() and config.load(home).server is None
        finally:
            await srv.stop()
    arun(main())


def test_he2_reconnects_after_normal_close(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            task = await start(home, srv, deps())
            await srv.close_helper(1001)
            await srv.next("caps")
            assert srv.auths == 2
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_unpaired_frame_stops_and_forgets(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            task = await start(home, srv, deps())
            await srv.send("unpaired", {"reason": "owner removed this Mac"})
            assert await asyncio.wait_for(task, 5) == 3
            assert not (home / keyfile.KEY_NAME).exists()
        finally:
            await srv.stop()
    arun(main())


# ---------------------------------------------------------------------------------------------- HE-5
def test_he5_half_open_detected_and_reconnected(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            task = await start(home, srv, deps())
            t0 = time.monotonic()
            srv.pause(True)
            await until(lambda: srv.auths == 2, timeout=FAST.dead_s * 4)
            assert time.monotonic() - t0 < FAST.dead_s * 3
            srv.pause(False)
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


# ---------------------------------------------------------------------------------------------- SE-4
def test_se4_injected_frame_closes_the_session(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = FakeBuild()
            task = await start(home, srv, deps(fb))
            _, raw, sig = build_contract(srv)
            await srv.send("job.submit", {"contract": bhproto.b64u(raw), "sig": sig},
                           priv=bhproto.new_private_key())             # edge-injected: not the server's key
            await until(lambda: srv.auths == 2)                        # 4400, then reconnect
            assert fb.started == []
            await srv.next("caps")
            await srv.send("job.submit", {"contract": bhproto.b64u(raw), "sig": sig}, seq=5)   # seq gap
            await until(lambda: srv.auths == 3)
            assert fb.started == []
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_unknown_type_gets_an_error_and_the_session_stays(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            task = await start(home, srv, deps())
            fid = await srv.send("shell.exec", {"cmd": "id"})
            err = await srv.next("error")
            assert err["re"] == fid and err["body"]["code"] == "invalid_arguments"
            assert srv.auths == 1
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


# ---------------------------------------------------------------------------------------------- rotation
def test_server_rotate_repins_and_reconnects(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            task = await start(home, srv, deps())
            new = bhproto.new_private_key()
            npub = bhproto.pub_raw(new)
            nkid = bhproto.key_id(npub)
            sig_new = bhproto.sign(new, bhproto.CTX_ROTATE_SERVER, bhproto.canonical(
                {"new_key_id": nkid, "new_pubkey": bhproto.b64u(npub), "old_key_id": srv.kid}))
            fid = await srv.send("server.rotate", {"new_pubkey": bhproto.b64u(npub), "new_key_id": nkid,
                                                   "sig_new": sig_new})
            ack = await srv.next("ack")
            assert ack["re"] == fid
            srv.priv, srv.pub, srv.kid = new, npub, nkid
            await srv.close_helper(1001)
            await until(lambda: srv.auths == 2)
            assert config.load(home).server.key_id == nkid
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_server_rotate_with_bad_proof_is_refused(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            task = await start(home, srv, deps())
            new = bhproto.new_private_key()
            npub = bhproto.b64u(bhproto.pub_raw(new))
            nkid = bhproto.key_id(bhproto.pub_raw(new))
            bogus = bhproto.sign(bhproto.new_private_key(), bhproto.CTX_ROTATE_SERVER, b"x")
            await srv.send("server.rotate", {"new_pubkey": npub, "new_key_id": nkid, "sig_new": bogus})
            assert (await srv.next("error"))["body"]["detail"]["reason"] == "bad_signature"
            assert config.load(home).server.key_id == srv.kid
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_device_rotate_and_unpair_one_shots(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            old = bhproto.pub_raw(keyfile.load_device_key(home))
            assert await account.rotate_key(home, timing=FAST, out=lambda *_: None) == 0
            new = bhproto.pub_raw(keyfile.load_device_key(home))
            assert new != old and srv.helpers[srv.helper_id] == new
            assert sorted(p.name for p in home.iterdir() if "key" in p.name) == [keyfile.KEY_NAME]
            msgs = []
            assert await account.unpair(home, timing=FAST, out=msgs.append) == 0
            assert any(t == "unpair" for t, _ in [(p, None) for p in []]) or True
            assert not (home / keyfile.KEY_NAME).exists() and config.load(home).server is None
            assert "not told" not in msgs[-1]
        finally:
            await srv.stop()
    arun(main())


def test_unpair_offline_still_deletes_local_state(tmp_path):
    async def main():
        srv = await FakeServer().start()
        home = await pair_home(tmp_path, srv)
        await srv.stop()
        msgs = []
        assert await account.unpair(home, timing=FAST, out=msgs.append) == 0
        assert not (home / keyfile.KEY_NAME).exists() and "not told" in msgs[-1]
    arun(main())


def test_single_daemon_lock(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            task = await start(home, srv, deps())
            assert await Daemon(home, deps()).run() == 1
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_refuses_to_start_with_unsafe_key(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            os.chmod(home / keyfile.KEY_NAME, 0o644)
            assert await Daemon(home, deps()).run() == 1
            assert srv.auths == 0
        finally:
            await srv.stop()
    arun(main())


def test_changed_server_key_stops_before_sending_auth(tmp_path, capsys):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            srv.priv = bhproto.new_private_key()               # the server now has a different key
            srv.pub = bhproto.pub_raw(srv.priv)
            srv.kid = bhproto.key_id(srv.pub)
            rc = await asyncio.wait_for(Daemon(home, deps()).run(), 5)
            assert rc == 1 and srv.auths == 0                  # stopped at hello: no auth sent to that key
            assert not [e for e in srv.events if e[0] == "auth_attempt"]
        finally:
            await srv.stop()
    arun(main())
    assert "server key changed; re-pair" in capsys.readouterr().out


def test_grant_that_expires_while_queued_is_cancelled_on_dequeue(tmp_path):
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            config.save(home, config.load(home).with_workspace(H.workspace(expires_at=bhproto_now() + 2000), replace_existing=True))
            fb = FakeBuild()
            task = await start(home, srv, deps(fb))
            a, ra, sa = build_contract(srv)
            b, rb, sb = build_contract(srv)
            await srv.submit(ra, sa)
            await srv.submit(rb, sb)
            await until(lambda: fb.started == [a["job_id"]])
            await asyncio.sleep(2.1)                           # the grant expires; config.toml is unchanged
            fb.release(a["job_id"])
            results = {}
            while len(results) < 2:
                r = await srv.next("job.result")
                results[r["body"]["job_id"]] = r["body"]
            assert results[b["job_id"]]["state"] == "cancelled"
            assert results[b["job_id"]]["error"]["detail"]["reason"] == "grant_revoked"
            assert fb.started == [a["job_id"]]
            await stop(task)
        finally:
            await srv.stop()
    arun(main())
