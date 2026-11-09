"""Server side of PROTOCOL 1 to 6 over a real aiohttp listener on localhost, driven by fake_helper.

PR-2 / SE-4 frame checks, pairing (PL-5), auth (SE-5), jobs end to end, helper_busy passthrough, offline resend,
restart reconcile (PL-7), cancel, unpair both ways, rate limits, and no secrets in logs (SE-6).
"""
import asyncio
import json
import logging
import socket
import threading
import time
import urllib.request
import uuid

import pytest

import bhproto as bp
from fake_helper import CAPS, INFO, FakeHelper, now_ms

from borrowed_hands import config, jobs, keys, listener, pairing
from borrowed_hands.store import JobState, Store

FAST = listener.Timings(handshake_s=1.0, reconcile_after_ms=400, tick_s=0.1, submit_reply_s=150, cancel_reply_s=3)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    def __init__(self, data_dir, port=None, timings=FAST, owners=("telegram:1",), trusted_proxy=None,
                 max_frame_bytes=config.MAX_FRAME):
        self.port = port or free_port()
        self.url = f"ws://127.0.0.1:{self.port}/bh/v1/ws"
        self.settings = config.Settings(listen_enabled=True, host="127.0.0.1", port=self.port, public_url=self.url,
                                        owners=tuple(owners), trusted_proxy=trusted_proxy,
                                        max_frame_bytes=max_frame_bytes)
        self.data_dir = data_dir
        self.store = Store(data_dir / "bh.sqlite3")
        self.key = keys.load_or_create(data_dir)
        self.pub = bp.pub_raw(self.key)
        self.notifier = pairing.Notifier(self.store)
        self.lst = listener.Listener(self.settings, self.store, self.key, self.notifier, timings=timings)
        self.jobs = jobs.JobService(self.store, self.lst)
        self.lst.jobs = self.jobs

    def start(self):
        self.lst.start()
        assert self.lst.error is None, self.lst.error
        return self

    def stop(self):
        self.lst.stop()

    def pair_string(self, issued_to="telegram:1"):
        return pairing.new_code(self.store, self.settings, self.pub, issued_to, now_ms())[0]


@pytest.fixture
def srv(data_dir):
    s = Server(data_dir).start()
    yield s
    s.stop()


async def paired(srv, name="Fake Mac", startup=True):
    h = FakeHelper.from_pairing(srv.pair_string(), name=name)
    r = await h.pair()
    assert r["type"] == "pair_ok", r
    if startup:
        await h.startup()
    return h


def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- transport basics
def test_healthz_and_no_other_routes(srv):
    with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/bh/v1/healthz") as r:
        assert r.status == 200 and json.loads(r.read()) == {"ok": True, "v": 1}
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/healthz")
    assert ei.value.code == 404


def test_hello_shape_and_subprotocol(srv):
    async def go():
        h = FakeHelper(srv.url, srv.pub)
        hello = await h.open()
        assert h.ws.protocol == "bh.v1"
        assert hello["type"] == "hello"
        b = hello["body"]
        assert b["server_key_id"] == bp.key_id(srv.pub) and b["v"] == 1 and b["max_frame"] == bp.MAX_FRAME
        assert len(bp.unb64u(b["server_nonce"])) == 32
        await h.close()
    asyncio.run(go())


def test_missing_subprotocol_is_refused(srv):
    async def go():
        h = FakeHelper(srv.url, srv.pub)
        try:
            await h.open(protocols=())
        except Exception:
            await h.close()
            return
        code = await h.expect_close()
        await h.close()
        assert code == 4400
    asyncio.run(go())


def test_handshake_timeout_4408(srv):
    async def go():
        h = FakeHelper(srv.url, srv.pub)
        await h.open()
        code = await h.expect_close()
        await h.close()
        return code
    assert asyncio.run(go()) == 4408


def test_binary_frame_1003(srv):
    async def go():
        h = await paired(srv)
        await h.ws.send_bytes(b"\x00\x01")
        code = await h.expect_close()
        await h.close()
        return code
    assert asyncio.run(go()) == 1003


# ---------------------------------------------------------------- pairing (PROTOCOL 3, PL-5)
def test_pair_happy_path_grants_nothing(srv):
    async def go():
        h = await paired(srv, startup=False)
        await h.startup()
        await h.close()
        return h
    h = asyncio.run(go())
    row = srv.store.get_helper(h.helper_id)
    assert row.name == "Fake Mac" and row.paired_by == "telegram:1" and row.revoked_at is None
    assert row.device_pubkey == bp.pub_raw(h.key)
    assert row.caps["capabilities"] == CAPS["capabilities"]


def test_pair_code_single_use_4410(srv):
    s = srv.pair_string()

    async def go():
        h1 = FakeHelper.from_pairing(s)
        assert (await h1.pair())["type"] == "pair_ok"
        await h1.close()
        h2 = FakeHelper.from_pairing(s)
        r = await h2.pair()
        code = await h2.expect_close()
        await h2.close()
        return r, code
    r, code = asyncio.run(go())
    assert r["type"] == "error" and r["body"]["code"] == "expired" and code == 4410


def test_pair_superseded_code_4410(srv):
    old = srv.pair_string()
    srv.pair_string()

    async def go():
        h = FakeHelper.from_pairing(old)
        r = await h.pair()
        code = await h.expect_close()
        await h.close()
        return r, code
    r, code = asyncio.run(go())
    assert r["body"]["code"] == "expired" and code == 4410


def test_pair_bad_pair_sig_rejected_before_consuming_code(srv):
    s = srv.pair_string()

    async def go():
        h = FakeHelper.from_pairing(s)
        await h.open()
        other = bp.new_private_key()
        dev_pub = bp.b64u(bp.pub_raw(h.key))
        nonce = bp.b64u(bytes(32))
        sig = bp.sign(other, bp.CTX_PAIR, bp.canonical({
            "client_nonce": nonce, "code_hash": bp.code_hash(h.code), "device_pubkey": dev_pub,
            "endpoint": h.endpoint, "server_nonce": h.hello["body"]["server_nonce"]}))
        await h.send_raw(h.handshake_frame("pair", {
            "code": bp.b64u(h.code), "device_pubkey": dev_pub, "device_name": "x", "client_nonce": nonce,
            "helper_version": "0.1.0", "os": "macos", "os_version": "26.2", "arch": "arm64", "pair_sig": sig}))
        code = await h.expect_close()
        await h.close()
        h2 = FakeHelper.from_pairing(s)
        r = await h2.pair()
        await h2.close()
        return code, r
    code, r = asyncio.run(go())
    assert code == 4400
    assert r["type"] == "pair_ok"


def test_pair_rate_limit_per_ip_4429(srv):
    async def one(code):
        h = FakeHelper.from_pairing(srv.pair_string())
        r = await h.pair(code=code)
        c = await h.expect_close()
        await h.close()
        return r, c

    async def go():
        out = []
        for _ in range(11):
            out.append(await one(bytes(16)))
        return out
    out = asyncio.run(go())
    assert all(r["body"]["code"] == "expired" and c == 4410 for r, c in out[:10])
    r, c = out[10]
    assert c == 4429 and r["body"]["code"] == "helper_unavailable"
    assert r["body"]["detail"]["reason"] == "rate_limited" and r["body"]["retry_after_s"] >= 1


def test_global_failure_limit_invalidates_outstanding_code(data_dir):
    rl = pairing.RateLimiter()
    t = 0
    for i in range(30):
        assert rl.allow(f"10.0.0.{i}", t) is None
        rl.failed(t)
    assert rl.allow("10.0.1.1", t) is not None
    assert rl.global_tripped(t)


def test_pair_code_never_stored_or_logged_plaintext(srv, caplog):
    caplog.set_level(logging.DEBUG)
    s = srv.pair_string()
    code_b64 = s.split("#")[1].split(".")[0]

    async def go():
        h = FakeHelper.from_pairing(s)
        await h.pair()
        await h.startup()
        await h.close()
    asyncio.run(go())
    raw = (srv.data_dir / "bh.sqlite3").read_bytes()
    for side in ("bh.sqlite3-wal",):
        p = srv.data_dir / side
        if p.exists():
            raw += p.read_bytes()
    assert code_b64.encode() not in raw and bp.unb64u(code_b64) not in raw
    assert code_b64 not in caplog.text


# ---------------------------------------------------------------- auth (PROTOCOL 1.3, SE-5)
def test_auth_happy_path_and_supersede_4409(srv):
    async def go():
        h = await paired(srv)
        h2 = FakeHelper(srv.url, srv.pub, device_key=h.key, helper_id=h.helper_id)
        await h2.auth()
        code = await h.expect_close()
        await h2.startup()
        await h.close()
        await h2.close()
        return code
    assert asyncio.run(go()) == 4409


@pytest.mark.parametrize("case", ["unknown", "badsig", "endpoint", "revoked"])
def test_auth_failures_4401(srv, case):
    async def go():
        h = await paired(srv)
        await h.close()
        h2 = FakeHelper(srv.url, srv.pub, device_key=h.key, helper_id=h.helper_id)
        kw = {}
        if case == "unknown":
            h2.helper_id = str(uuid.uuid4())
        elif case == "badsig":
            kw["key"] = bp.new_private_key()
        elif case == "endpoint":
            kw["endpoint"] = "wss://evil.example/bh/v1/ws"
        else:
            srv.store.revoke_helper(h.helper_id, now=now_ms())
        try:
            await h2.auth(**kw)
        except AssertionError:
            pass
        except Exception:
            pass
        code = await h2.expect_close() if not h2.ws.closed else h2.ws.close_code
        await h2.close()
        return code
    assert asyncio.run(go()) == 4401


# ---------------------------------------------------------------- signed frames (PR-2, SE-4)
@pytest.mark.parametrize("attack", ["unsigned", "flipped", "seq_gap", "seq_repeat", "dup_id", "wrong_sid",
                                    "expired", "window", "wrong_key", "replay_old", "bad_schema", "version",
                                    "missing_seq", "extra_field"])
def test_frame_attacks_close_4400(srv, attack):
    async def go():
        h = await paired(srv)
        if attack == "unsigned":
            await h.send_raw(bp.wrap(h.frame("ack", {}), None))
        elif attack == "flipped":
            env = json.loads(bp.wrap(h.frame("ack", {}), h.key))
            raw = bytearray(bp.unb64u(env["p"]))
            raw[5] ^= 1
            env["p"] = bp.b64u(bytes(raw))
            await h.send_raw(json.dumps(env))
        elif attack == "seq_gap":
            await h.send("ack", {}, seq=h.seq_out + 2)
        elif attack == "seq_repeat":
            await h.send("ack", {}, seq=h.seq_out)
        elif attack == "dup_id":
            p = await h.send("caps", CAPS)
            await h.recv()
            await h.send("caps", CAPS, id=p["id"])
        elif attack == "wrong_sid":
            await h.send("ack", {}, sid=str(uuid.uuid4()))
        elif attack == "expired":
            ts = now_ms() - 200_000
            await h.send("ack", {}, ts=ts, exp=ts + 100_000)
        elif attack == "window":
            ts = now_ms()
            await h.send("ack", {}, ts=ts, exp=ts + 120_001)
        elif attack == "wrong_key":
            await h.send_raw(bp.wrap(h.frame("ack", {}), bp.new_private_key()))
        elif attack == "replay_old":
            p = h.frame("caps", CAPS)
            text = bp.wrap(p, h.key)
            await h.send_raw(text)
            await h.recv()
            await h.send_raw(text)
        elif attack == "bad_schema":
            await h.send("caps", {**CAPS, "evil": 1})
        elif attack in ("missing_seq", "extra_field"):
            p = h.frame("ack", {})
            if attack == "missing_seq":
                del p["seq"]
            else:
                p["x"] = 1
            await h.send_raw(bp.wrap(p, h.key))
        elif attack == "version":
            await h.send_raw(json.dumps({"v": 2, "p": "e30", "s": "A" * 86}))
        code = await h.expect_close()
        await h.close()
        return code
    assert asyncio.run(go()) == 4400


def test_unknown_type_is_error_not_close(srv):
    async def go():
        h = await paired(srv)
        p = await h.send("job.submit", {"contract": "e30", "sig": "A" * 86})   # S->H only
        r = await h.recv()
        p2 = await h.send("caps", CAPS)
        r2 = await h.recv()
        await h.close()
        return p, r, p2, r2
    p, r, p2, r2 = asyncio.run(go())
    assert r["type"] == "error" and r["re"] == p["id"] and r["body"]["code"] == "invalid_arguments"
    assert r2["type"] == "ack" and r2["re"] == p2["id"]


# ---------------------------------------------------------------- jobs end to end
def _submit_in_thread(srv, helper_id, wait_s=10, action="system.info", params=None):
    out = {}

    def run():
        out["r"] = srv.jobs.submit_and_wait(helper_id, action, params or {}, wait_s, "none", session_id="s1")
    t = threading.Thread(target=run)
    t.start()
    return t, out


def test_system_info_round_trip(srv):
    async def go():
        h = await paired(srv)
        t, out = _submit_in_thread(srv, h.helper_id)
        sub = await h.recv()
        assert sub["type"] == "job.submit"
        c = await h.answer_system_info(sub)
        await asyncio.to_thread(t.join, 10)
        await h.close()
        return c, out["r"]
    c, r = asyncio.run(go())
    assert c["action"] == "system.info" and c["params"] == {} and c["approval_ref"] == "none"
    assert r["ok"] is True and r["state"] == "succeeded" and r["result"] == INFO and r["job_id"] == c["job_id"]
    j = srv.store.get_job(c["job_id"])
    assert j.state is JobState.SUCCEEDED and j.contract_hash == __import__("hashlib").sha256(j.contract).hexdigest()


def test_helper_busy_passthrough(srv):
    async def go():
        h = await paired(srv)
        t, out = _submit_in_thread(srv, h.helper_id)
        sub = await h.recv()
        h.verify_contract(sub["body"])
        await h.send("error", {"code": "helper_busy", "message": "queue full", "detail": {"reason": "queue_full"},
                               "retry_after_s": 42}, re=sub["id"])
        await asyncio.to_thread(t.join, 10)
        await h.close()
        return out["r"]
    r = asyncio.run(go())
    assert r["ok"] is False and r["error"]["code"] == "helper_busy"
    assert r["error"]["detail"] == {"reason": "queue_full"} and r["error"]["retry_after_s"] == 42


def test_offline_submit_returns_job_id_then_resends_identical_bytes(srv):
    async def go():
        h = await paired(srv)
        await h.close()
        assert wait_for(lambda: not srv.lst.is_online(h.helper_id))
        r = await asyncio.to_thread(srv.jobs.submit_and_wait, h.helper_id, "system.info", {}, 0, "none", "s1")
        assert r["ok"] is True and r["state"] == "pending_send" and r["helper_online"] is False
        stored = srv.store.get_job(r["job_id"]).contract
        h2 = FakeHelper(srv.url, srv.pub, device_key=h.key, helper_id=h.helper_id)
        await h2.auth()
        await h2.startup()
        sub = await h2.recv()
        assert bp.unb64u(sub["body"]["contract"]) == stored
        await h2.answer_system_info(sub)
        await h2.close()
        return r["job_id"]
    job_id = asyncio.run(go())
    assert srv.jobs.get(job_id)["state"] == "succeeded"


def test_restart_mid_job_reconciles_and_dedupes_results(data_dir):
    """PL-7: plugin dies mid-job (no on_unload), comes back, the job is stale, jobs.sync reconciles it."""
    s1 = Server(data_dir).start()
    port = s1.port

    async def phase1():
        h = await paired(s1)
        t, out = _submit_in_thread(s1, h.helper_id, wait_s=1)
        sub = await h.recv()
        c = h.verify_contract(sub["body"])
        await h.send("job.accepted", {"job_id": c["job_id"], "state": "queued", "at": now_ms(), "position": 0},
                     re=sub["id"])
        await asyncio.to_thread(t.join, 10)
        await h.close()
        return h, c, out["r"]
    h, c, r = asyncio.run(phase1())
    assert r["state"] == "queued"
    s1.stop()
    s2 = Server(data_dir, port=port)
    s2.jobs.on_startup()
    assert s2.store.get_job(c["job_id"]).stale is True
    s2.start()
    try:
        result = {"job_id": c["job_id"], "state": "succeeded", "result": INFO, "error": None}

        async def phase2():
            h2 = FakeHelper(s2.url, s2.pub, device_key=h.key, helper_id=h.helper_id)
            await h2.auth()
            await h2.send("caps", CAPS)
            await h2.recv()
            await h2.send("jobs.sync", {"active": [], "results": [result]})
            ack = await h2.recv()
            await h2.send("job.result", {**result, "result": {**INFO, "cpu": "different"}})
            ack2 = await h2.recv()
            await h2.close()
            return ack, ack2
        ack, ack2 = asyncio.run(phase2())
        assert ack["body"]["job_ids"] == [c["job_id"]] and ack2["body"]["job_ids"] == [c["job_id"]]
        j = s2.store.get_job(c["job_id"])
        assert j.state is JobState.SUCCEEDED and j.stale is False and j.result == INFO
    finally:
        s2.stop()


def test_unreported_expired_stale_job_becomes_lost(srv):
    async def go():
        h = await paired(srv)
        await h.close()
        assert wait_for(lambda: not srv.lst.is_online(h.helper_id))
        return h
    h = asyncio.run(go())
    jid = str(uuid.uuid4())
    srv.store.insert_job(job_id=jid, helper_id=h.helper_id, action="system.info", contract=b"{}", sig="x",
                         contract_hash="y", nonce="z", session_id="s", now=1, expires_at=now_ms() - 1)
    srv.store.mark_sent(jid, now=2)
    srv.store.mark_nonterminal_stale()

    async def back():
        h2 = FakeHelper(srv.url, srv.pub, device_key=h.key, helper_id=h.helper_id)
        await h2.auth()
        await h2.startup()
        await asyncio.sleep(0.8)
        await h2.close()
    asyncio.run(back())
    j = srv.store.get_job(jid)
    assert j.state is JobState.FAILED and j.error["code"] == "internal_error" and j.error["detail"]["reason"] == "lost"


def test_cancel_queued_job(srv):
    async def go():
        h = await paired(srv)
        t, out = _submit_in_thread(srv, h.helper_id, wait_s=1)
        sub = await h.recv()
        c = h.verify_contract(sub["body"])
        await h.send("job.accepted", {"job_id": c["job_id"], "state": "queued", "at": now_ms(), "position": 0},
                     re=sub["id"])
        await asyncio.to_thread(t.join, 10)
        ct = threading.Thread(target=lambda: out.__setitem__("c", srv.jobs.cancel(c["job_id"])))
        ct.start()
        req = await h.recv()
        assert req["type"] == "job.cancel" and req["body"] == {"job_id": c["job_id"]}
        await h.send("ack", {}, re=req["id"])
        await h.send("job.result", {"job_id": c["job_id"], "state": "cancelled", "result": None,
                                    "error": {"code": "cancelled", "message": "cancelled by the agent"}})
        await h.recv()
        await asyncio.to_thread(ct.join, 10)
        await h.close()
        return c["job_id"], out["c"]
    jid, r = asyncio.run(go())
    assert r["ok"] is True
    assert srv.jobs.get(jid)["state"] == "cancelled"


def test_cancel_pending_send_is_local(srv):
    async def go():
        h = await paired(srv)
        await h.close()
        assert wait_for(lambda: not srv.lst.is_online(h.helper_id))
        return h
    h = asyncio.run(go())
    r = srv.jobs.submit_and_wait(h.helper_id, "system.info", {}, 0, "none", "s1")
    c = srv.jobs.cancel(r["job_id"])
    assert c["ok"] is True and c["state"] == "cancelled"


def test_unpair_from_server_4403_and_cancels(srv):
    async def go():
        h = await paired(srv)
        t, out = _submit_in_thread(srv, h.helper_id, wait_s=0)
        sub = await h.recv()
        await asyncio.to_thread(t.join, 10)
        res = await asyncio.to_thread(srv.lst.call, srv.lst.unpair(h.helper_id, "unpaired by owner"), 5)
        msg = await h.recv()
        code = await h.expect_close()
        await h.close()
        return h, out["r"]["job_id"], msg, code, res
    h, jid, msg, code, res = asyncio.run(go())
    assert res is True and msg["type"] == "unpaired" and code == 4403
    assert srv.store.get_helper(h.helper_id).revoked_at is not None
    j = srv.store.get_job(jid)
    assert j.state is JobState.CANCELLED and j.error["detail"]["reason"] == "revoked"


def test_unpair_from_helper(srv):
    async def go():
        h = await paired(srv)
        p = await h.send("unpair", {})
        a = await h.recv()
        await h.close()
        return h, p, a
    h, p, a = asyncio.run(go())
    assert a["type"] == "ack" and a["re"] == p["id"]
    assert srv.store.get_helper(h.helper_id).revoked_at is not None
    assert any("Unpaired" in n for n in srv.store.pending_notices())


def test_device_rotate(srv):
    async def go():
        h = await paired(srv)
        new = bp.new_private_key()
        new_pub = bp.b64u(bp.pub_raw(new))
        sig_new = bp.sign(new, bp.CTX_ROTATE_DEVICE, bp.canonical({"helper_id": h.helper_id, "new_pubkey": new_pub,
                                                                    "sid": h.sid}))
        p = await h.send("device.rotate", {"new_pubkey": new_pub, "sig_new": sig_new})
        a = await h.recv()
        await h.close()
        h2 = FakeHelper(srv.url, srv.pub, device_key=new, helper_id=h.helper_id)
        await h2.auth()
        await h2.close()
        return a, p
    a, p = asyncio.run(go())
    assert a["type"] == "ack" and a["re"] == p["id"]


def test_device_rotate_bad_new_sig_is_denied(srv):
    async def go():
        h = await paired(srv)
        new = bp.new_private_key()
        new_pub = bp.b64u(bp.pub_raw(new))
        sig_new = bp.sign(bp.new_private_key(), bp.CTX_ROTATE_DEVICE,
                          bp.canonical({"helper_id": h.helper_id, "new_pubkey": new_pub, "sid": h.sid}))
        await h.send("device.rotate", {"new_pubkey": new_pub, "sig_new": sig_new})
        a = await h.recv()
        await h.close()
        return h, a
    h, a = asyncio.run(go())
    assert a["type"] == "error" and a["body"]["code"] == "denied"
    assert srv.store.get_helper(h.helper_id).device_pubkey == bp.pub_raw(h.key)


def test_no_secrets_in_logs(srv, caplog):
    caplog.set_level(logging.DEBUG)
    s = srv.pair_string()
    code_b64 = s.split("#")[1].split(".")[0]
    seen_sigs = []

    async def go():
        h = FakeHelper.from_pairing(s)
        await h.pair()
        await h.startup()
        t, out = _submit_in_thread(srv, h.helper_id)
        sub = await h.recv()
        seen_sigs.append(sub["body"]["sig"])
        seen_sigs.append(sub["body"]["contract"])
        await h.answer_system_info(sub)
        await asyncio.to_thread(t.join, 10)
        await h.close()
    asyncio.run(go())
    text = caplog.text
    assert "borrowed_hands" in text  # the plugin did log
    seed = bp.seed_of(srv.key)
    for secret in [code_b64, bp.b64u(seed), seed.hex(), *seen_sigs]:
        assert secret not in text


def test_helper_cannot_report_another_helpers_job(srv):
    async def go():
        a = await paired(srv, name="Mac A")
        b = await paired(srv, name="Mac B")
        t, out = _submit_in_thread(srv, a.helper_id, wait_s=0)
        sub = await a.recv()
        c = a.verify_contract(sub["body"])
        await asyncio.to_thread(t.join, 10)
        await b.send("job.state", {"job_id": c["job_id"], "state": "building", "at": now_ms()})
        await b.send("job.result", {"job_id": c["job_id"], "state": "succeeded", "result": INFO, "error": None})
        await b.recv()
        await a.close()
        await b.close()
        return c["job_id"]
    jid = asyncio.run(go())
    j = srv.store.get_job(jid)
    assert j.state is JobState.SUBMITTED and j.result is None


def test_result_of_the_wrong_kind_is_rejected(srv):
    build = {"status": "succeeded", "duration_ms": 1, "xcode": "26.1", "destination": "platform=macOS,arch=arm64",
             "error_count": 0, "warning_count": 0, "errors": [], "warnings": [], "log_tail": "", "truncated": False}

    async def go():
        h = await paired(srv)
        t, out = _submit_in_thread(srv, h.helper_id)
        sub = await h.recv()
        c = h.verify_contract(sub["body"])
        await h.send("job.result", {"job_id": c["job_id"], "state": "succeeded", "result": build, "error": None})
        await h.recv()
        await asyncio.to_thread(t.join, 10)
        await h.close()
        return out["r"]
    r = asyncio.run(go())
    assert r["ok"] is False and r["state"] == "failed" and r["error"]["detail"]["reason"] == "bad_result"
    assert "result" not in r
