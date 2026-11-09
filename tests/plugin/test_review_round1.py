"""Fix round 1 (adversarial review): each reviewer probe, inverted into an attack test that must hold."""
import asyncio
import json
import os
import socket
import time
import uuid

import pytest

import bhproto as bp
from fake_helper import CAPS, INFO, FakeHelper, now_ms
from test_listener import Server, paired, srv, wait_for  # noqa: F401  (srv is a fixture)

from borrowed_hands import hooks, keys, schema
from borrowed_hands.store import JobState

ATTACKER_CAPS = {**CAPS, "name": "Attacker", "capabilities": []}


# ---------------------------------------------------------------- HIGH 1: replies bound to helper and session
@pytest.mark.parametrize("reply", ["error", "job.state", "job.result"])
def test_other_helper_cannot_answer_my_request(srv, reply):
    async def go():
        victim = await paired(srv, name="Victim")
        attacker = await paired(srv, name="Attacker")
        v = await asyncio.to_thread(srv.jobs.submit_and_wait, victim.helper_id, "system.info", {}, 0, "none")
        request = await victim.recv()
        if reply == "error":
            await attacker.send("error", {"code": "denied", "message": "foreign refusal"}, re=request["id"])
        elif reply == "job.state":
            await attacker.send("job.accepted", {"job_id": v["job_id"], "state": "queued", "at": now_ms()},
                                re=request["id"])
        else:
            await attacker.send("job.result", {"job_id": v["job_id"], "state": "failed", "result": None,
                                               "error": {"code": "denied", "message": "foreign"}}, re=request["id"])
            await attacker.recv()
        await attacker.send("caps", ATTACKER_CAPS)
        await attacker.recv()  # barrier: the server processed the forged reply
        middle = srv.store.get_job(v["job_id"])
        await victim.answer_system_info(request)
        await victim.close()
        await attacker.close()
        return middle, srv.store.get_job(v["job_id"])
    middle, after = asyncio.run(go())
    assert middle.state is JobState.SUBMITTED and middle.error is None
    assert after.state is JobState.SUCCEEDED and after.result == INFO


def test_reply_for_a_different_job_does_not_consume_the_request(srv):
    async def go():
        h = await paired(srv)
        a = await asyncio.to_thread(srv.jobs.submit_and_wait, h.helper_id, "system.info", {}, 0, "none")
        req_a = await h.recv()
        b = await asyncio.to_thread(srv.jobs.submit_and_wait, h.helper_id, "system.info", {}, 0, "none")
        req_b = await h.recv()
        h.verify_contract(req_a["body"])
        h.verify_contract(req_b["body"])
        # answer B's state but quote A's request id: must not clear A's pending request
        await h.send("job.accepted", {"job_id": b["job_id"], "state": "queued", "at": now_ms()}, re=req_a["id"])
        await h.send("error", {"code": "helper_busy", "message": "full"}, re=req_a["id"])
        await h.send("caps", CAPS)
        await h.recv()
        await h.close()
        return a["job_id"], b["job_id"]
    ja, jb = asyncio.run(go())
    assert srv.store.get_job(ja).error["code"] == "helper_busy"
    assert srv.store.get_job(jb).state is JobState.QUEUED


# ---------------------------------------------------------------- HIGH 2: cancel serialized with dispatch
def test_cancel_while_submit_waits_for_the_send_lock_never_delivers(srv):
    async def go():
        h = await paired(srv)
        m = srv.jobs.mint(h.helper_id, "system.info", {}, "none")
        c = m["contract"]
        srv.store.insert_job(job_id=c["job_id"], helper_id=h.helper_id, action="system.info", contract=m["raw"],
                             sig=m["sig"], contract_hash=m["hash"], nonce=c["nonce"], session_id="r",
                             now=c["issued_at"], expires_at=c["expires_at"])
        entered, release = asyncio.Event(), asyncio.Event()

        async def race():
            s = srv.lst._sessions[h.helper_id]
            await s.lock.acquire()
            t = asyncio.create_task(srv.lst.submit(c["job_id"]))
            await asyncio.sleep(0)
            entered.set()
            await release.wait()
            s.lock.release()
            return await t
        f = asyncio.run_coroutine_threadsafe(race(), srv.lst.loop)
        while not entered.is_set():
            await asyncio.sleep(0.005)
        cancelled = await asyncio.to_thread(srv.jobs.cancel, c["job_id"])
        srv.lst.loop.call_soon_threadsafe(release.set)
        sent = await asyncio.wrap_future(f)
        await h.send("caps", CAPS)
        nxt = await h.recv()
        await h.close()
        return cancelled, sent, nxt
    cancelled, sent, nxt = asyncio.run(go())
    assert cancelled["state"] == "cancelled" and sent is False
    assert nxt["type"] == "ack"          # the next frame is the caps ack, not a job.submit


def test_cancel_after_dispatch_began_goes_through_the_helper(srv):
    async def go():
        h = await paired(srv)
        m = srv.jobs.mint(h.helper_id, "system.info", {}, "none")
        c = m["contract"]
        srv.store.insert_job(job_id=c["job_id"], helper_id=h.helper_id, action="system.info", contract=m["raw"],
                             sig=m["sig"], contract_hash=m["hash"], nonce=c["nonce"], session_id="r",
                             now=c["issued_at"], expires_at=c["expires_at"])
        srv.store.mark_dispatching(c["job_id"], now=now_ms())   # dispatch began; delivery not confirmed
        out = {}
        t = asyncio.create_task(asyncio.to_thread(lambda: out.setdefault("c", srv.jobs.cancel(c["job_id"]))))
        req = await h.recv()
        assert req["type"] == "job.cancel"
        await h.send("error", {"code": "invalid_arguments", "message": "unknown job"}, re=req["id"])
        await t
        await h.close()
        return c["job_id"], out["c"]
    jid, r = asyncio.run(go())
    assert r["ok"] is False and srv.store.get_job(jid).state is JobState.SUBMITTED


# ---------------------------------------------------------------- HIGH 3: delivery uncertainty is not terminal
def test_disconnect_suspends_reply_deadlines_and_late_success_lands(srv):
    async def go():
        h = await paired(srv)
        v = await asyncio.to_thread(srv.jobs.submit_and_wait, h.helper_id, "system.info", {}, 0, "none")
        await h.recv()
        await h.close()
        assert wait_for(lambda: not srv.lst.is_online(h.helper_id))

        def expire_pending():
            for p in srv.lst._pending.values():
                p.deadline = time.monotonic() - 1
            srv.lst._tick()
        srv.lst.loop.call_soon_threadsafe(expire_pending)
        await asyncio.sleep(0.1)
        mid = srv.store.get_job(v["job_id"])
        h2 = FakeHelper(srv.url, srv.pub, device_key=h.key, helper_id=h.helper_id)
        await h2.auth()
        await h2.startup(results=[{"job_id": v["job_id"], "state": "succeeded", "result": INFO, "error": None}])
        await h2.close()
        return mid, srv.store.get_job(v["job_id"])
    mid, row = asyncio.run(go())
    assert not mid.state.terminal
    assert row.state is JobState.SUCCEEDED and row.result == INFO


def test_unreported_job_is_lost_only_after_its_contract_expired(srv):
    async def go():
        h = await paired(srv)
        v = await asyncio.to_thread(srv.jobs.submit_and_wait, h.helper_id, "system.info", {}, 0, "none")
        await h.recv()
        await h.close()
        assert wait_for(lambda: not srv.lst.is_online(h.helper_id))
        h2 = FakeHelper(srv.url, srv.pub, device_key=h.key, helper_id=h.helper_id)
        await h2.auth()
        await h2.startup()                                  # does not report the job
        await asyncio.sleep(0.8)                            # past reconcile_after_ms (FAST: 400 ms)
        before = srv.store.get_job(v["job_id"])
        c = srv.store._conn()
        c.execute("UPDATE jobs SET expires_at=? WHERE job_id=?", (now_ms() - 1, v["job_id"]))
        await asyncio.sleep(0.5)
        await h2.close()
        return before, srv.store.get_job(v["job_id"])
    before, after = asyncio.run(go())
    assert not before.state.terminal
    assert after.state is JobState.FAILED and after.error["detail"]["reason"] == "lost"


# ---------------------------------------------------------------- HIGH 4: pre-auth limits
def _loop_gap_while(srv, coro_factory):
    gaps = []

    async def monitor():
        prev = time.monotonic()
        until = prev + 1.0
        while time.monotonic() < until:
            await asyncio.sleep(0.005)
            t = time.monotonic()
            gaps.append(t - prev)
            prev = t
    f = asyncio.run_coroutine_threadsafe(monitor(), srv.lst.loop)
    res = asyncio.run(coro_factory())
    f.result(5)
    return max(gaps), res


def test_large_unauthenticated_frame_is_refused_before_decoding(srv):
    async def go():
        h = FakeHelper(srv.url, srv.pub)
        await h.open()
        raw = b'{"junk":[' + b"{}," * 999999 + b"{}]}"
        await asyncio.sleep(0.03)
        await h.send_raw(json.dumps({"v": 1, "p": bp.b64u(raw)}, separators=(",", ":")))
        code = await h.expect_close()
        await h.close()
        return code
    gap, code = _loop_gap_while(srv, go)
    assert code == 1009
    assert gap < 0.1, gap


def test_pre_auth_frame_over_16k_closes_1009_and_small_one_is_fine(srv):
    async def go():
        h = FakeHelper(srv.url, srv.pub)
        await h.open()
        await h.send_raw(h.handshake_frame("auth", {"helper_id": str(uuid.uuid4()), "client_nonce": "x" * 17000,
                                                    "device_sig": "A" * 86}))
        code = await h.expect_close()
        await h.close()
        return code
    assert asyncio.run(go()) == 1009


def test_authenticated_frames_may_exceed_the_pre_auth_limit(srv):
    async def go():
        h = await paired(srv)
        results = [{"job_id": str(uuid.uuid4()), "state": "failed", "result": None,
                    "error": {"code": "internal_error", "message": "x" * 900}} for _ in range(40)]
        await h.send("caps", CAPS)
        await h.recv()
        p = await h.send("jobs.sync", {"active": [], "results": results})
        ack = await h.recv()
        await h.close()
        return len(bp.wrap(p, h.key)), ack
    size, ack = asyncio.run(go())
    assert size > 32 * 1024 and ack["type"] == "job.ack"


def test_deeply_nested_pre_auth_frame_is_refused(srv):
    async def go():
        h = FakeHelper(srv.url, srv.pub)
        await h.open()
        raw = b"[" * 5000 + b"]" * 5000
        await h.send_raw(json.dumps({"v": 1, "p": bp.b64u(raw)}))
        code = await h.expect_close()
        await h.close()
        return code
    assert asyncio.run(go()) == 4400


def test_unauthenticated_connections_are_capped_per_ip(srv):
    async def go():
        held = []
        for _ in range(srv.lst.MAX_UNAUTH_PER_IP):
            h = FakeHelper(srv.url, srv.pub)
            await h.open()
            held.append(h)
        extra = FakeHelper(srv.url, srv.pub)
        try:
            await extra.open()
            code = await extra.expect_close(timeout=3)
        except Exception as exc:  # refused at the HTTP level is fine too
            code = repr(exc)
        await extra.close()
        for h in held:
            await h.close()
        # once those are gone, a new one is accepted
        ok = FakeHelper(srv.url, srv.pub)
        hello = await ok.open()
        await ok.close()
        return code, hello
    code, hello = asyncio.run(go())
    assert code in (4429, 1013) or "503" in str(code) or "429" in str(code)
    assert hello["type"] == "hello"


def test_incomplete_http_request_is_closed_by_the_handshake_deadline(srv):
    s = socket.create_connection(("127.0.0.1", srv.port))
    s.sendall(b"GET /bh/v1/ws HTTP/1.1\r\nHost: x\r\n")      # never finishes the headers
    s.settimeout(srv.lst.t.handshake_s + 3)
    t0 = time.time()
    try:
        data = s.recv(100)
    except (socket.timeout, ConnectionResetError):
        data = b"timeout"
    s.close()
    assert data != b"timeout", "connection still open past the handshake deadline"
    assert time.time() - t0 <= srv.lst.t.handshake_s + 3


def test_idle_tcp_connection_is_closed_by_the_handshake_deadline(srv):
    s = socket.create_connection(("127.0.0.1", srv.port))
    s.settimeout(srv.lst.t.handshake_s + 3)
    try:
        data = s.recv(100)
    except (socket.timeout, ConnectionResetError):
        data = b"timeout"
    s.close()
    assert data != b"timeout"


# ---------------------------------------------------------------- LOW 9: one pairing-failure path
def test_bad_pair_signatures_also_trip_code_invalidation(srv):
    from borrowed_hands import pairing
    s = srv.pair_string()
    for i in range(pairing.GLOBAL_FAIL_LIMIT):
        srv.lst._rl.failed(now_ms())
    async def go():
        h = FakeHelper.from_pairing(s)
        await h.open()
        dev_pub = bp.b64u(bp.pub_raw(h.key))
        await h.send_raw(h.handshake_frame("pair", {
            "code": bp.b64u(h.code), "device_pubkey": dev_pub, "device_name": "x", "client_nonce": "A" * 43,
            "helper_version": "0.1.0", "os": "macos", "os_version": "26.2", "arch": "arm64", "pair_sig": "A" * 86}))
        code = await h.expect_close()
        await h.close()
        return code
    srv.lst._rl = type(srv.lst._rl)()          # fresh limiter: only this attempt's failures count
    for i in range(pairing.GLOBAL_FAIL_LIMIT - 1):
        srv.lst._rl.failed(now_ms())
    asyncio.run(go())
    code_hash = bp.code_hash(FakeHelper.from_pairing(s).code)
    assert srv.store.consume_code(code_hash, now=now_ms()) is False   # invalidated by the tripping failure


# ---------------------------------------------------------------- LOW 10: v must be the integer 1
def test_boolean_version_is_rejected():
    raw = bp.canonical({"type": "hello", "id": str(uuid.uuid4()), "ts": 1, "body": {}})
    with pytest.raises(bp.ProtocolError):
        bp.unwrap(json.dumps({"v": True, "p": bp.b64u(raw)}), None)
    assert schema.errors("envelope.json", {"v": True, "p": "e30"}) != []


# ---------------------------------------------------------------- MEDIUM 5: no delimiter collisions
def test_rule_key_has_no_delimiter_collisions():
    base = {"helper_id": "6a1e2b3c-4d5e-4f60-8172-839405a6b7c8", "workspace_id": "demo",
            "destination": "platform=macOS,arch=arm64"}
    a = hooks.rule_key("hands_xcode_build", {**base, "scheme": "A:B", "configuration": "C"})
    b = hooks.rule_key("hands_xcode_build", {**base, "scheme": "A", "configuration": "B:C"})
    assert a != b and a.startswith("hands_xcode_build:") and b.startswith("hands_xcode_build:")
    c1 = hooks.rule_key("hands_screen_capture", {"helper_id": "h:1", "display": 2})
    c2 = hooks.rule_key("hands_screen_capture", {"helper_id": "h", "display": "1:2"})
    assert c1 != c2


# ---------------------------------------------------------------- MEDIUM 6: tickets keyed by invocation
def test_denied_ticket_is_dropped_even_if_helper_resolution_changes():
    active = [True]
    hid = "6a1e2b3c-4d5e-4f60-8172-839405a6b7c8"

    def resolve(tool, args):
        return (hid, None, {**args, "helper_id": hid}) if active[0] else (None, None, args)
    hooks.TICKETS.clear()
    hooks.configure(resolve)
    try:
        args = {"display": 1}
        hooks.pre_tool_call(tool_name="hands_screen_capture", args=args, session_id="s", tool_call_id="call-1")
        active[0] = False
        hooks.post_tool_call(tool_name="hands_screen_capture", args=args, session_id="s", status="blocked",
                             tool_call_id="call-1")
        active[0] = True
        assert hooks.consume_ticket("s", "hands_screen_capture", args, helper_id=hid) is None
    finally:
        hooks.TICKETS.clear()


def test_approved_ticket_is_still_consumable_when_resolution_is_stable():
    hid = "6a1e2b3c-4d5e-4f60-8172-839405a6b7c8"
    hooks.TICKETS.clear()
    hooks.configure(lambda tool, a: (hid, None, {**a, "helper_id": hid}))
    try:
        hooks.pre_tool_call(tool_name="hands_screen_capture", args={"display": 1}, session_id="s",
                            tool_call_id="call-2")
        hooks.post_tool_call(tool_name="hands_screen_capture", args={"display": 1}, session_id="s",
                             status="ok", tool_call_id="call-2")
        assert hooks.consume_ticket("s", "hands_screen_capture", {"display": 1}, helper_id=hid,
                                    tool_call_id="call-2")
    finally:
        hooks.TICKETS.clear()


# ---------------------------------------------------------------- MEDIUM 7: concurrent first-run key creation
_KEY_CHILD = r"""
import importlib.util, os, sys, time
plugin_dir, d, go = sys.argv[1], sys.argv[2], sys.argv[3]
spec = importlib.util.spec_from_file_location("bh_child", os.path.join(plugin_dir, "keys.py"),
                                              submodule_search_locations=[])
pkg = importlib.util.spec_from_file_location("bh_pkg", os.path.join(plugin_dir, "bhproto.py"))
bhproto = importlib.util.module_from_spec(pkg); pkg.loader.exec_module(bhproto)
import types
parent = types.ModuleType("bh_parent"); parent.__path__ = [plugin_dir]; parent.bhproto = bhproto
sys.modules["bh_parent"] = parent; sys.modules["bh_parent.bhproto"] = bhproto
ks = importlib.util.spec_from_file_location("bh_parent.keys", os.path.join(plugin_dir, "keys.py"))
keys = importlib.util.module_from_spec(ks); sys.modules["bh_parent.keys"] = keys; ks.loader.exec_module(keys)
while not os.path.exists(go):
    time.sleep(0.001)
print(bhproto.key_id(bhproto.pub_raw(keys.load_or_create(d))))
"""


def test_concurrent_first_run_key_creation_agrees(tmp_path):
    """Six processes create the key at the same moment: all must end up with the one key on disk."""
    import subprocess
    import sys
    from pathlib import Path
    plugin_dir = str(Path(keys.__file__).parent)
    for round_ in range(5):
        d = tmp_path / f"r{round_}"
        d.mkdir(mode=0o700)
        go = tmp_path / f"go{round_}"
        ps = [subprocess.Popen([sys.executable, "-I", "-B", "-c", _KEY_CHILD, plugin_dir, str(d), str(go)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(6)]
        time.sleep(0.5)
        go.write_text("go")
        outs = [p.communicate(timeout=30) for p in ps]
        assert all(p.returncode == 0 for p in ps), [o[1][-300:] for o in outs]
        ids = {o[0].strip() for o in outs}
        final = bp.key_id(bp.pub_raw(keys.load_or_create(d)))
        assert ids == {final}, (round_, ids, final)
        assert sorted(p.name for p in d.iterdir()) == sorted([keys.KEY_FILE, keys.LOCK_FILE])


# ---------------------------------------------------------------- mutation survivors from the review
def test_key_file_with_loose_mode_is_refused(tmp_path):
    d = tmp_path / "k"
    d.mkdir(mode=0o700)
    keys.load_or_create(d)
    os.chmod(d / keys.KEY_FILE, 0o644)
    with pytest.raises(keys.KeyFileError):
        keys.load_or_create(d)


def test_key_file_symlink_is_refused(tmp_path):
    d = tmp_path / "k"
    d.mkdir(mode=0o700)
    target = tmp_path / "elsewhere"                     # a perfectly valid key file, but not ours
    target.write_text(bp.b64u(os.urandom(32)) + "\n")
    os.chmod(target, 0o600)
    os.symlink(target, d / keys.KEY_FILE)
    with pytest.raises((keys.KeyFileError, OSError)):
        keys.load_or_create(d)


def test_unsupported_schema_keyword_is_refused():
    with pytest.raises(ValueError):
        schema.check({"type": "object", "minProperties": 1}, {})


def test_plugin_readme_carries_the_rule_13_disclosures():
    from pathlib import Path
    text = (Path(keys.__file__).parent / "README.md").read_text()
    for needle in ["listener", "8790", "background thread", "server_ed25519.key", "bh.sqlite3", "plugin-data",
                   "bh-builder", "sudoers", "LaunchDaemon", "Screen Recording", "No telemetry", "pre_tool_call"]:
        assert needle in text, needle


def test_reply_of_the_wrong_kind_does_not_consume_the_request(srv):
    async def go():
        h = await paired(srv)
        v = await asyncio.to_thread(srv.jobs.submit_and_wait, h.helper_id, "system.info", {}, 0, "none")
        req = await h.recv()
        await h.send("ack", {}, re=req["id"])                       # an ack cannot answer a job.submit
        await h.send("error", {"code": "helper_busy", "message": "full"}, re=req["id"])
        await h.send("caps", CAPS)
        await h.recv()
        await h.close()
        return v["job_id"]
    jid = asyncio.run(go())
    assert srv.store.get_job(jid).error["code"] == "helper_busy"


def test_cancel_during_the_submit_write_goes_through_the_helper(srv):
    """The job.submit write is in progress (blocked): a cancel must not finish the job locally."""
    async def go():
        h = await paired(srv)
        m = srv.jobs.mint(h.helper_id, "system.info", {}, "none")
        c = m["contract"]
        srv.store.insert_job(job_id=c["job_id"], helper_id=h.helper_id, action="system.info", contract=m["raw"],
                             sig=m["sig"], contract_hash=m["hash"], nonce=c["nonce"], session_id="r",
                             now=c["issued_at"], expires_at=c["expires_at"])
        sess = srv.lst._sessions[h.helper_id]
        real = sess.ws.send_str
        gate_open = asyncio.Event()
        writing = asyncio.Event()

        async def slow_send(text, *a, **kw):
            if '"job.submit"' in bp.unb64u(json.loads(text)["p"]).decode():
                writing.set()
                await gate_open.wait()
            return await real(text, *a, **kw)

        async def setup():
            sess.ws.send_str = slow_send
            return asyncio.get_running_loop().create_task(srv.lst.submit(c["job_id"]))
        task = asyncio.run_coroutine_threadsafe(setup(), srv.lst.loop).result(5)
        await asyncio.to_thread(lambda: asyncio.run_coroutine_threadsafe(writing.wait(), srv.lst.loop).result(5))
        outcome = await asyncio.to_thread(srv.lst.call, srv.lst.cancel_unsent(c["job_id"]), 5)
        srv.lst.loop.call_soon_threadsafe(gate_open.set)
        sub = await h.recv()
        await h.close()
        return outcome, sub, srv.store.get_job(c["job_id"])
    outcome, sub, job = asyncio.run(go())
    assert outcome == "dispatched" and sub["type"] == "job.submit"
    assert job.state is JobState.SUBMITTED


def test_authenticated_connections_do_not_count_against_the_pre_auth_cap(srv):
    async def go():
        helpers = [await paired(srv, name=f"Mac {i}") for i in range(srv.lst.MAX_UNAUTH_PER_IP)]
        extra = FakeHelper(srv.url, srv.pub)
        hello = await extra.open()
        await extra.close()
        for h in helpers:
            await h.close()
        return hello
    assert asyncio.run(go())["type"] == "hello"


def test_blocked_ticket_is_dropped_by_invocation_even_if_args_changed():
    """post_tool_call can see different (middleware-modified) args; the invocation id still finds the ticket."""
    hooks.TICKETS.clear()
    hooks.configure(lambda tool, a: (a.get("helper_id"), None, dict(a)))
    try:
        args = {"helper_id": "h1", "display": 1}
        hooks.pre_tool_call(tool_name="hands_screen_capture", args=args, session_id="s", tool_call_id="call-9")
        hooks.post_tool_call(tool_name="hands_screen_capture", args={"helper_id": "h1", "display": 2},
                             session_id="s", status="blocked", tool_call_id="call-9")
        assert hooks.consume_ticket("s", "hands_screen_capture", args) is None
    finally:
        hooks.TICKETS.clear()
