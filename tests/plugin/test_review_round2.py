"""Fix round 2 (re-review): each new probe inverted into an attack test, plus tests that make the
session-binding and global-connection-cap rules load-bearing."""
import asyncio
import json
import time
import uuid

import pytest

import bhproto as bp
from fake_helper import CAPS, INFO, FakeHelper, now_ms
from test_listener import Server, paired, srv, wait_for  # noqa: F401

from borrowed_hands import config, hooks, listener, schema
from borrowed_hands.store import JobState


def _insert(srv, h):
    m = srv.jobs.mint(h.helper_id, "system.info", {}, "none")
    c = m["contract"]
    srv.store.insert_job(job_id=c["job_id"], helper_id=h.helper_id, action="system.info", contract=m["raw"],
                         sig=m["sig"], contract_hash=m["hash"], nonce=c["nonce"], session_id="r",
                         now=c["issued_at"], expires_at=c["expires_at"])
    return c


async def _submit_with_failing_drain(srv, h, c):
    async def go():
        sess = srv.lst._sessions[h.helper_id]
        orig = sess.ws.send_str

        async def send(text, **kw):
            await orig(text, **kw)
            raise ConnectionResetError("write succeeded, drain failed")
        sess.ws.send_str = send
        try:
            try:
                await srv.lst.submit(c["job_id"])
            except ConnectionResetError:
                pass
        finally:
            sess.ws.send_str = orig
    await asyncio.to_thread(srv.lst.call, go(), 5)


# ---------------------------------------------------------------- HIGH 1: a write attempt is never "unsent"
def test_send_failure_after_write_is_not_cancelled_locally(srv):
    async def go():
        h = await paired(srv)
        c = _insert(srv, h)
        await _submit_with_failing_drain(srv, h, c)
        request = await h.recv()
        assert request["type"] == "job.submit"
        mid = srv.store.get_job(c["job_id"])
        out = {}
        t = asyncio.create_task(asyncio.to_thread(lambda: out.setdefault("c", srv.jobs.cancel(c["job_id"]))))
        req = await h.recv()                       # the cancel must go to the Mac
        assert req["type"] == "job.cancel" and req["body"] == {"job_id": c["job_id"]}
        await h.send("error", {"code": "invalid_arguments", "message": "already finished"}, re=req["id"])
        await t
        await h.answer_system_info(request)
        await h.close()
        return mid, out["c"], srv.store.get_job(c["job_id"])
    mid, cancelled, row = asyncio.run(go())
    assert mid.state is JobState.SUBMITTED and mid.pending_send   # delivery uncertain, not unsent
    assert cancelled["ok"] is False
    assert row.state is JobState.SUCCEEDED and row.result == INFO


def test_uncertain_send_is_resent_with_identical_bytes_after_reconnect(srv):
    async def go():
        h = await paired(srv)
        c = _insert(srv, h)
        await _submit_with_failing_drain(srv, h, c)
        first = await h.recv()
        await h.close()
        assert wait_for(lambda: not srv.lst.is_online(h.helper_id))
        h2 = FakeHelper(srv.url, srv.pub, device_key=h.key, helper_id=h.helper_id)
        await h2.auth()
        await h2.startup()                                       # helper does not know the job
        again = await h2.recv()
        await h2.answer_system_info(again)
        await h2.close()
        return first, again
    first, again = asyncio.run(go())
    assert again["type"] == "job.submit" and again["body"] == first["body"]


# ---------------------------------------------------------------- MEDIUM 3: active report clears pending-send
def test_partial_send_then_active_report_then_expiry_and_empty_sync_is_lost(srv):
    async def go():
        h = await paired(srv)
        c = _insert(srv, h)
        await _submit_with_failing_drain(srv, h, c)
        req = await h.recv()
        await h.send("job.accepted", {"job_id": c["job_id"], "state": "queued", "at": now_ms()}, re=req["id"])
        await h.send("caps", CAPS)
        await h.recv()
        mid = srv.store.get_job(c["job_id"])
        await h.close()
        assert wait_for(lambda: not srv.lst.is_online(h.helper_id))
        srv.store._conn().execute("UPDATE jobs SET expires_at=? WHERE job_id=?", (now_ms() - 1, c["job_id"]))
        h2 = FakeHelper(srv.url, srv.pub, device_key=h.key, helper_id=h.helper_id)
        await h2.auth()
        await h2.startup()
        await asyncio.sleep(0.8)
        await h2.close()
        return mid, srv.store.get_job(c["job_id"])
    mid, after = asyncio.run(go())
    assert mid.state is JobState.QUEUED and not mid.pending_send
    assert after.state is JobState.FAILED and after.error["detail"]["reason"] == "lost"


# ---------------------------------------------------------------- HIGH 2: client address and buffering bounds
def test_x_forwarded_for_is_ignored_without_a_trusted_proxy(srv):
    async def go():
        held = []
        for i in range(srv.lst.MAX_UNAUTH_PER_IP):
            h = FakeHelper(srv.url, srv.pub)
            await h.open(headers={"X-Forwarded-For": f"10.9.0.{i}"})
            held.append(h)
        extra = FakeHelper(srv.url, srv.pub)
        with pytest.raises(Exception, match="429"):
            await extra.open(headers={"X-Forwarded-For": "10.9.9.9"})
        await extra.close()
        for h in held:
            await h.close()
    asyncio.run(go())


def test_trusted_proxy_uses_the_rightmost_forwarded_entry(data_dir):
    s = Server(data_dir, trusted_proxy="railway").start()
    try:
        async def go():
            held = []
            for i in range(s.lst.MAX_UNAUTH_PER_IP):   # spoofed left part differs, edge-appended part is the same
                h = FakeHelper(s.url, s.pub)
                await h.open(headers={"X-Forwarded-For": f"6.6.6.{i}, 203.0.113.7"})
                held.append(h)
            extra = FakeHelper(s.url, s.pub)
            with pytest.raises(Exception, match="429"):
                await extra.open(headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.7"})
            await extra.close()
            other = FakeHelper(s.url, s.pub)
            hello = await other.open(headers={"X-Forwarded-For": "6.6.6.0, 203.0.113.8"})
            await other.close()
            for h in held:
                await h.close()
            return hello
        assert asyncio.run(go())["type"] == "hello"
    finally:
        s.stop()


def test_global_unauthenticated_cap_holds_across_many_client_addresses(data_dir):
    s = Server(data_dir, trusted_proxy="railway").start()
    try:
        async def go():
            held = []
            for i in range(s.lst.MAX_UNAUTH_TOTAL):
                h = FakeHelper(s.url, s.pub)
                await h.open(headers={"X-Forwarded-For": f"198.51.100.{i}"})
                held.append(h)
            extra = FakeHelper(s.url, s.pub)
            with pytest.raises(Exception, match="429"):
                await extra.open(headers={"X-Forwarded-For": "198.51.100.250"})
            await extra.close()
            for h in held:
                await h.close()
        asyncio.run(go())
    finally:
        s.stop()


def test_pre_auth_buffering_is_bounded():
    worst = listener.Listener.MAX_UNAUTH_TOTAL * config.MAX_FRAME
    assert listener.Listener.MAX_UNAUTH_TOTAL <= 8 and config.MAX_FRAME == bp.MAX_FRAME
    assert worst <= 128 * 1024 * 1024


def test_trusted_proxy_setting_parses():
    get = lambda d: (lambda key, default=None: d.get(key, default))  # noqa: E731
    assert config.load(get({}), {"BH_TRUSTED_PROXY": "railway"}).trusted_proxy == "railway"
    assert config.load(get({}), {}).trusted_proxy is None
    bad = config.load(get({}), {"BH_TRUSTED_PROXY": "anything"})
    assert bad.trusted_proxy is None and bad.problems


def test_authenticated_frame_over_the_limit_closes_1009(data_dir):
    s = Server(data_dir, max_frame_bytes=256 * 1024).start()
    try:
        async def go():
            h = await paired(s)
            results = [{"job_id": str(uuid.uuid4()), "state": "failed", "result": None,
                        "error": {"code": "internal_error", "message": "x" * 1000}} for _ in range(100)]
            big = {"active": [], "results": results}
            await h.send("caps", CAPS)
            await h.recv()
            await h.send("jobs.sync", {**big, "results": results * 1})   # ~ 180 KiB wire: fine
            await h.recv()
            pad = "y" * 300_000
            await h.send("caps", {**CAPS, "helper_version": pad})          # > 256 KiB
            code = await h.expect_close()
            await h.close()
            return code
        assert asyncio.run(go()) == 1009
    finally:
        s.stop()


# ---------------------------------------------------------------- session binding is load-bearing
def test_reply_on_another_session_of_the_same_helper_is_ignored(srv):
    async def go():
        h = await paired(srv)
        c = _insert(srv, h)
        fid = str(uuid.uuid4())
        srv.lst._pending[fid] = listener._Pending("submit", c["job_id"], h.helper_id, str(uuid.uuid4()),
                                                  time.monotonic() + 60)   # went out on an older session
        await h.send("error", {"code": "denied", "message": "stale"}, re=fid)
        await h.send("caps", CAPS)
        await h.recv()
        await h.close()
        return c["job_id"], fid in srv.lst._pending
    jid, still = asyncio.run(go())
    assert srv.store.get_job(jid).state is JobState.PENDING_SEND and still


# ---------------------------------------------------------------- MEDIUM 4: approved scope must not drift
def test_ticket_refused_when_the_resolved_default_destination_changed():
    hid = "6a1e2b3c-4d5e-4f60-8172-839405a6b7c8"
    dest = ["platform=macOS,arch=arm64"]
    hooks.TICKETS.clear()
    hooks.configure(lambda tool, a: (hid, "Mac", {**a, "destination": a.get("destination") or dest[0]}))
    try:
        args = {"helper_id": hid, "workspace_id": "demo", "scheme": "Demo", "configuration": "Debug",
                "commit": "5" * 40, "repo_url": "https://github.com/a/demo"}
        hooks.pre_tool_call(tool_name="hands_xcode_build", args=args, session_id="s", tool_call_id="c1")
        dest[0] = "generic/platform=iOS Simulator"
        assert hooks.consume_ticket("s", "hands_xcode_build", args, helper_id=hid, tool_call_id="c1") is None
        dest[0] = "platform=macOS,arch=arm64"
        hooks.pre_tool_call(tool_name="hands_xcode_build", args=args, session_id="s", tool_call_id="c2")
        assert hooks.consume_ticket("s", "hands_xcode_build", args, helper_id=hid, tool_call_id="c2") is not None
    finally:
        hooks.TICKETS.clear()


def test_ticket_checks_the_scope_the_handler_will_execute():
    hid = "6a1e2b3c-4d5e-4f60-8172-839405a6b7c8"
    hooks.TICKETS.clear()
    hooks.configure(lambda tool, a: (hid, "Mac", {**a, "destination": "platform=macOS,arch=arm64"}))
    try:
        args = {"workspace_id": "demo", "scheme": "Demo", "commit": "5" * 40, "repo_url": "https://github.com/a/b"}
        hooks.pre_tool_call(tool_name="hands_xcode_build", args=args, session_id="s", tool_call_id="c3")
        assert hooks.consume_ticket("s", "hands_xcode_build", args, helper_id=hid, tool_call_id="c3",
                                    scope={"destination": "generic/platform=iOS Simulator"}) is None
        hooks.pre_tool_call(tool_name="hands_xcode_build", args=args, session_id="s", tool_call_id="c4")
        assert hooks.consume_ticket("s", "hands_xcode_build", args, helper_id=hid, tool_call_id="c4",
                                    scope={"destination": "platform=macOS,arch=arm64", "configuration": "Debug"})
    finally:
        hooks.TICKETS.clear()


# ---------------------------------------------------------------- MEDIUM 5: no tool_call_id and modified args
def test_denied_ticket_without_call_id_and_modified_args_is_dropped():
    hooks.TICKETS.clear()
    hooks.configure(lambda tool, a: ("h1", None, dict(a)))
    try:
        original = {"helper_id": "h1", "display": 1}
        decision = hooks.pre_tool_call(tool_name="hands_screen_capture", args=original, session_id="s")
        assert decision["action"] == "block"
        assert hooks.consume_ticket("s", "hands_screen_capture", original, helper_id="h1") is None
        hooks.post_tool_call(tool_name="hands_screen_capture", args={**original, "display": 2}, session_id="s",
                             status="blocked")
        assert hooks.consume_ticket("s", "hands_screen_capture", original, helper_id="h1") is None
    finally:
        hooks.TICKETS.clear()


# ---------------------------------------------------------------- LOW 6: every pairing failure is counted
@pytest.mark.parametrize("field,value", [("code", "A" * 21 + "B"), ("device_pubkey", "A" * 42 + "B"),
                                         ("pair_sig", "A" * 85 + "B")])
def test_noncanonical_pair_values_count_as_pairing_failures(srv, field, value):
    from borrowed_hands import pairing
    pair = srv.pair_string()
    code_hash = bp.code_hash(FakeHelper.from_pairing(pair).code)

    async def go():
        for i in range(pairing.GLOBAL_FAIL_LIMIT):
            h = FakeHelper(srv.url, srv.pub)
            await h.open()
            body = {
                "code": "A" * 22, "device_pubkey": "A" * 43, "device_name": "x", "client_nonce": "A" * 43,
                "helper_version": "0.1.0", "os": "macos", "os_version": "26.2", "arch": "arm64",
                "pair_sig": "A" * 86}
            await h.send_raw(h.handshake_frame("pair", {**body, field: value}))
            await h.expect_close()
            await h.close()
    old = pairing.PER_IP_LIMIT
    pairing.PER_IP_LIMIT = 1000          # one test address: lift the per-IP attempt limit, keep the global one
    try:
        asyncio.run(go())
    finally:
        pairing.PER_IP_LIMIT = old
    assert len(srv.lst._rl._fails) >= pairing.GLOBAL_FAIL_LIMIT
    assert srv.store.consume_code(code_hash, now=now_ms()) is False


def test_uncertain_delivery_survives_listener_replacement(data_dir):
    s = Server(data_dir).start()
    try:
        async def first():
            h = await paired(s)
            c = _insert(s, h)
            await _submit_with_failing_drain(s, h, c)
            frame = await h.recv()
            await h.close()
            return h, c, frame
        h, c, first_frame = asyncio.run(first())
    finally:
        s.stop()
    restarted = Server(data_dir).start()
    try:
        restarted.jobs.on_startup()
        row = restarted.store.get_job(c["job_id"])
        assert row.state is JobState.SUBMITTED and row.pending_send and row.stale
        assert restarted.jobs.cancel(c["job_id"])["ok"] is False   # offline never means unsent
        assert restarted.store.get_job(c["job_id"]).state is JobState.SUBMITTED

        async def again():
            h2 = FakeHelper(restarted.url, restarted.pub, device_key=h.key, helper_id=h.helper_id)
            await h2.auth()
            await h2.startup()
            frame = await h2.recv()
            await h2.answer_system_info(frame)
            await h2.close()
            return frame
        assert asyncio.run(again())["body"] == first_frame["body"]
        assert restarted.store.get_job(c["job_id"]).state is JobState.SUCCEEDED
    finally:
        restarted.stop()


@pytest.mark.parametrize("path", ["schema", "signature", "unknown_code", "missing_endpoint"])
def test_pair_validation_failures_invalidate_the_outstanding_code(srv, path):
    from dataclasses import replace
    from borrowed_hands import pairing
    pair = srv.pair_string()
    for _ in range(pairing.GLOBAL_FAIL_LIMIT - 1):
        srv.lst._rl.failed(now_ms())

    async def go():
        h = FakeHelper.from_pairing(pair)
        try:
            await h.open()
            code = bytes(16) if path == "unknown_code" else h.code
            pub = bp.b64u(bp.pub_raw(h.key))
            nonce = bp.b64u(bytes(32))
            sig = bp.sign(bp.new_private_key() if path == "signature" else h.key, bp.CTX_PAIR, bp.canonical({
                "client_nonce": nonce, "code_hash": bp.code_hash(code), "device_pubkey": pub,
                "endpoint": h.endpoint, "server_nonce": h.hello["body"]["server_nonce"]}))
            body = {"code": bp.b64u(code), "device_pubkey": pub, "device_name": 42 if path == "schema" else "x",
                    "client_nonce": nonce, "helper_version": "0.1.0", "os": "macos", "os_version": "26.2",
                    "arch": "arm64", "pair_sig": sig}
            if path == "missing_endpoint":
                srv.lst.settings = replace(srv.lst.settings, public_url=None)
            await h.send_raw(h.handshake_frame("pair", body))
            if path == "unknown_code":
                assert (await h.recv_handshake())["body"]["code"] == "expired"
            assert await h.expect_close() == (4410 if path == "unknown_code" else 4400)
        finally:
            await h.close()
    asyncio.run(go())
    assert len(srv.lst._rl._fails) == pairing.GLOBAL_FAIL_LIMIT
    assert not srv.store.consume_code(bp.code_hash(FakeHelper.from_pairing(pair).code), now=now_ms())


@pytest.mark.parametrize("field,new", [("helper_id", "h2"), ("workspace_id", "other"), ("scheme", "Other"),
                                       ("configuration", "Release"), ("destination", "generic/platform=iOS Simulator")])
def test_every_resolved_approval_field_is_bound(field, new):
    hooks.TICKETS.clear()
    resolved = {"helper_id": "h1", "workspace_id": "demo", "scheme": "Demo", "configuration": "Debug",
                "destination": "platform=macOS,arch=arm64"}
    hooks.configure(lambda tool, a: (resolved["helper_id"], "Mac", {**a, **resolved}))
    try:
        args = {"workspace_id": "demo", "scheme": "Demo", "commit": "5" * 40, "repo_url": "https://github.com/a/b"}
        hooks.pre_tool_call(tool_name="hands_xcode_build", args=args, session_id="s", tool_call_id="c5")
        resolved[field] = new
        assert hooks.consume_ticket("s", "hands_xcode_build", args) is None
    finally:
        hooks.TICKETS.clear()


def _screen(n):
    return {"mime": "image/png", "width": 2000, "height": 2000, "display": 1, "png_b64": "A" * n}


def test_screen_near_v1_wire_maximum_is_received_and_persisted(srv):
    async def go():
        h = await paired(srv)
        c = _insert(srv, h)
        # Only the result action matters to _apply_result; no screen command executes on this fake helper.
        srv.store._conn().execute("UPDATE jobs SET action='screen.capture' WHERE job_id=?", (c["job_id"],))
        result = _screen(12 * 1024 * 1024 - 1024)
        body = {"job_id": c["job_id"], "state": "succeeded", "result": result, "error": None}
        assert not schema.errors("job-result.json", body)
        frame = bp.wrap(h.frame("job.result", body), h.key)
        assert 8 * 1024 * 1024 < len(frame) < bp.MAX_FRAME
        await h.send_raw(frame)
        ack = await h.recv()
        row = srv.store.get_job(c["job_id"])
        await h.close()
        return row, ack, result
    row, ack, result = asyncio.run(go())
    assert ack["type"] == "job.ack" and ack["body"]["job_ids"] == [row.job_id]
    assert row.state is JobState.SUCCEEDED and row.result == result


def test_schema_maximum_screen_still_obeys_envelope_wire_ceiling():
    # PROTOCOL 1 requires the sender to fail frame_too_large even if the inner body passes its schema.
    body = {"job_id": str(uuid.uuid4()), "state": "succeeded", "result": _screen(12 * 1024 * 1024), "error": None}
    assert not schema.errors("job-result.json", body)
    key = bp.new_private_key()
    ts = now_ms()
    p = {"type": "job.result", "id": str(uuid.uuid4()), "sid": str(uuid.uuid4()), "seq": 1,
         "ts": ts, "exp": ts + 120_000, "body": body}
    frame = bp.wrap(p, key)
    assert len(frame) > bp.MAX_FRAME
    with pytest.raises(bp.ProtocolError, match="frame too large"):
        bp.unwrap(frame, bp.pub_raw(key))


def test_full_bounded_diagnostics_fit_the_transport_and_are_persisted(srv):
    async def go():
        h = await paired(srv)
        c = _insert(srv, h)
        srv.store._conn().execute("UPDATE jobs SET action='xcode.build' WHERE job_id=?", (c["job_id"],))
        diag = {"file": "x" * 500, "message": "\x00" * 500, "type": "\x00" * 100,
                "line": 9007199254740991, "column": 9007199254740991}
        result = {"status": "failed", "duration_ms": 9007199254740991, "xcode": "26.1", "destination": "x" * 200,
                  "error_count": 50, "warning_count": 50, "errors": [diag] * 50, "warnings": [diag] * 50,
                  "log_tail": "\x00" * 4000, "truncated": True}
        body = {"job_id": c["job_id"], "state": "failed", "result": result, "error": None}
        assert not schema.errors("job-result.json", body)
        frame = bp.wrap(h.frame("job.result", body), h.key)
        assert len(frame) < 1024 * 1024
        await h.send_raw(frame)
        ack = await h.recv()
        row = srv.store.get_job(c["job_id"])
        await h.close()
        return row, ack, result
    row, ack, result = asyncio.run(go())
    assert ack["type"] == "job.ack" and row.state is JobState.FAILED and row.result == result
