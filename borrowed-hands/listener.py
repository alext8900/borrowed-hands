"""The WSS listener: a thread-owned asyncio loop running aiohttp (SPEC 6), and the server side of PROTOCOL 1-6.

Connection state machine (one per WebSocket):

    UPGRADED --send hello--> HANDSHAKE --auth ok--> AUTHED --close/err--> CLOSED
                                 |      --pair ok--^
                                 +-- timeout 4408 / bad frame 4400 / auth 4401 / code 4410 / rate 4429 --> CLOSED

In AUTHED every inbound frame must be signed by the device key, carry this session's sid, seq = last + 1, an
unseen id and a live exp; anything else closes 4400 (PROTOCOL 2.2, 2.3). Nothing here trusts the helper beyond
recording what it reports about its own jobs.

Never call ctx.spawn_task (no running loop in register) and never touch the gateway loop from here; blocking work
(SQLite is fast enough, the owner-notice edit is not) goes to the default executor.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from . import bhproto, schema
from .config import Settings
from .pairing import Notifier, RateLimiter
from .store import JobState, Store

log = logging.getLogger("borrowed_hands")

WS_PATH = "/bh/v1/ws"
PRE_AUTH_MAX_FRAME = 16 * 1024     # hello/auth/pair frames are small; refuse bigger ones before decoding
HEALTH_PATH = "/bh/v1/healthz"

BODY_SCHEMA = {
    "caps": "caps.json", "jobs.sync": "jobs-sync.json", "job.accepted": "job-state.json",
    "job.state": "job-state.json", "job.result": "job-result.json", "unpair": "empty.json",
    "device.rotate": "device-rotate.json", "ack": "empty.json", "error": "error.json",
}
NONTERMINAL_REPORTABLE = {"received": JobState.RECEIVED, "queued": JobState.QUEUED,
                          "materializing": JobState.MATERIALIZING, "building": JobState.BUILDING}
RESULT_SHAPE = {"system.info": "os_build", "screen.capture": "png_b64", "xcode.build": "status"}


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Timings:
    handshake_s: float = 10.0
    reconcile_after_ms: int = 30_000
    tick_s: float = 2.0
    submit_reply_s: float = 150.0
    cancel_reply_s: float = 15.0


class _Close(Exception):
    def __init__(self, code: int, why: str):
        super().__init__(why)
        self.code = code


class ListenerDown(RuntimeError):
    pass


@dataclass
class _Session:
    ws: Any
    helper_id: str
    device_pub: bytes
    sid: str
    auth_at: int
    name: str
    seq_in: int = 0
    seq_out: int = 0
    ids_in: Set[str] = field(default_factory=set)
    reported: Set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _Pending:
    kind: str                 # "submit" | "cancel"
    job_id: str
    helper_id: str
    sid: str                  # replies count only on the session the request went out on
    deadline: float
    future: Optional[asyncio.Future] = None


class Listener:
    # Connections that have not finished auth/pair yet. aiohttp's frame limit is fixed at upgrade to the post-auth
    # limit (settings.max_frame_bytes, at most 16 MiB). At most 128 MiB of incomplete message bytes can be
    # accumulated across unauthenticated sockets; parser copies/decoded strings add overhead (README.md).
    MAX_UNAUTH_TOTAL = 8
    MAX_UNAUTH_PER_IP = 4
    def __init__(self, settings: Settings, store: Store, server_key, notifier: Notifier,
                 timings: Timings = Timings()):
        self.settings = settings
        self.store = store
        self.key = server_key
        self.pub = bhproto.pub_raw(server_key)
        self.key_id = bhproto.key_id(self.pub)
        self.notifier = notifier
        self.t = timings
        self.jobs = None                      # set by the wiring (jobs.JobService); notified on job changes
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.error: Optional[str] = None
        self.pid = os.getpid()
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._runner = None
        self._tick_task = None
        self._sessions: Dict[str, _Session] = {}
        self._pending: Dict[str, _Pending] = {}
        self._rl = RateLimiter()
        self._unauth_total = 0
        self._unauth_by_ip: Dict[str, int] = {}

    # ================================================================ lifecycle (called from Hermes threads)
    def start(self, wait_s: float = 3.0) -> None:
        self._thread = threading.Thread(target=self._run, name="bh-listener", daemon=True)
        self._thread.start()
        if not self._ready.wait(wait_s):
            self.error = self.error or f"listener did not start within {wait_s:.0f} s"

    @property
    def running(self) -> bool:
        return self.error is None and self.loop is not None and self.loop.is_running()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except BaseException as exc:  # bind errors, missing aiohttp: degrade, never crash the gateway
            self.error = f"{type(exc).__name__}: {exc}"
            log.warning("borrowed_hands: listener not running on %s:%s (%s)", self.settings.host,
                        self.settings.port, self.error)
            self._ready.set()
            loop.close()
            return
        self.loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    async def _serve(self) -> None:
        try:
            from aiohttp import web
        except ImportError as exc:
            raise RuntimeError("aiohttp missing; listener disabled") from exc

        async def healthz(_req):
            return web.json_response({"ok": True, "v": 1})

        app = web.Application(client_max_size=self.settings.max_frame_bytes)
        app.router.add_get(HEALTH_PATH, healthz)
        app.router.add_get(WS_PATH, self._ws_handler)
        # keepalive_timeout bounds a TCP connection that never completes an HTTP request (aiohttp arms it at
        # connection_made); after the upgrade the handshake deadline applies.
        self._runner = web.AppRunner(app, access_log=None, handle_signals=False,
                                     keepalive_timeout=self.t.handshake_s)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.settings.host, self.settings.port)
        await site.start()
        self._tick_task = asyncio.get_running_loop().create_task(self._ticker())
        log.info("borrowed_hands: listener up on %s:%s (server key %s)", self.settings.host, self.settings.port,
                 self.key_id)

    def stop(self) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(5)
        except Exception as exc:
            log.warning("borrowed_hands: listener shutdown: %s", type(exc).__name__)
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(5)
        self.loop = None
        log.info("borrowed_hands: listener stopped")

    async def _shutdown(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
        for s in list(self._sessions.values()):
            await s.ws.close(code=1001, message=b"server going away")
        if self._runner is not None:
            await self._runner.cleanup()

    def call(self, coro, timeout: float) -> Any:
        """Run a coroutine on the listener loop from another thread and wait for it."""
        loop = self.loop
        if loop is None or not loop.is_running():
            coro.close()
            raise ListenerDown(self.error or "listener not running")
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    def is_online(self, helper_id: str) -> bool:
        return helper_id in self._sessions

    def online_helpers(self) -> Set[str]:
        return set(self._sessions)

    # ================================================================ notifications
    def _changed(self, job_id: str) -> None:
        if self.jobs is not None:
            self.jobs.changed(job_id)

    # ================================================================ connection
    async def _ws_handler(self, request):
        from aiohttp import WSMsgType, web
        ip = self._client_ip(request)
        if self._unauth_total >= self.MAX_UNAUTH_TOTAL or self._unauth_by_ip.get(ip, 0) >= self.MAX_UNAUTH_PER_IP:
            log.info("borrowed_hands: refusing a connection: too many unauthenticated connections")
            return web.Response(status=429, text="too many pending connections")
        self._unauth_total += 1
        self._unauth_by_ip[ip] = self._unauth_by_ip.get(ip, 0) + 1
        counted = [True]

        def authed():
            if counted[0]:
                counted[0] = False
                self._unauth_total -= 1
                left = self._unauth_by_ip.get(ip, 1) - 1
                if left > 0:
                    self._unauth_by_ip[ip] = left
                else:
                    self._unauth_by_ip.pop(ip, None)
        # aiohttp's limit has to be the post-auth one (16 MiB); pre-auth frames are checked against
        # PRE_AUTH_MAX_FRAME before any decoding, and unauthenticated connections are capped above.
        ws = web.WebSocketResponse(protocols=("bh.v1",), heartbeat=20, max_msg_size=self.settings.max_frame_bytes,
                                   compress=False)
        sess: Optional[_Session] = None
        try:
            await ws.prepare(request)
            if ws.ws_protocol != "bh.v1":
                raise _Close(4400, "missing subprotocol bh.v1")
            sess = await self._handshake(ws, ip)
            authed()
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_frame(sess, msg.data)
                elif msg.type == WSMsgType.BINARY:
                    raise _Close(1003, "binary frame")
                elif msg.type == WSMsgType.ERROR:
                    break
        except _Close as c:
            log.info("borrowed_hands: closing %s with %s (%s)", (sess.helper_id[:8] if sess else ip), c.code, c)
            await ws.close(code=c.code)
        except bhproto.ProtocolError as e:
            log.info("borrowed_hands: closing %s with %s (%s)", (sess.helper_id[:8] if sess else ip),
                     e.close_code, e)
            await ws.close(code=e.close_code)
        except (asyncio.CancelledError, ConnectionResetError):
            raise
        except Exception:
            log.exception("borrowed_hands: connection handler error")
            await ws.close(code=1011)
        finally:
            authed()
            if sess is not None and self._sessions.get(sess.helper_id) is sess:
                del self._sessions[sess.helper_id]
                self.notifier.disconnected(sess.helper_id, sess.sid)
                self.store.set_detached(sess.helper_id, now_ms())
                log.info("borrowed_hands: helper %s disconnected", sess.helper_id[:8])
            # Delivery is now uncertain, not failed: drop this session's reply deadlines. The jobs stay
            # non-terminal and are reconciled from jobs.sync on the next authentication (PROTOCOL 4.5).
            for rid, p in list(self._pending.items()):
                if sess is not None and p.sid == sess.sid:
                    del self._pending[rid]
                    if p.future is not None and not p.future.done():
                        p.future.set_result(None)
        return ws

    def _client_ip(self, request) -> str:
        """The socket peer, unless settings.trusted_proxy names the proxy in front of us; then the rightmost
        X-Forwarded-For entry, the one that proxy appended (anything left of it is client-controlled)."""
        if self.settings.trusted_proxy == "railway":
            parts = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
            if parts:
                return parts[-1][:64]
        return request.remote or "?"

    async def _send_handshake(self, ws, type_: str, body: dict, re: Optional[str] = None) -> None:
        p = {"type": type_, "id": str(uuid.uuid4()), "ts": now_ms(), "body": body}
        if re:
            p["re"] = re
        await ws.send_str(bhproto.wrap(p, None))

    async def _handshake(self, ws, ip: str) -> _Session:
        server_nonce = bhproto.b64u(os.urandom(32))
        await self._send_handshake(ws, "hello", {"v": 1, "server_nonce": server_nonce, "server_key_id": self.key_id,
                                                 "server_time_ms": now_ms(), "max_frame": bhproto.MAX_FRAME})
        from aiohttp import WSMsgType
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=self.t.handshake_s)
        except asyncio.TimeoutError:
            raise _Close(4408, "handshake timeout")
        if msg.type == WSMsgType.BINARY:
            raise _Close(1003, "binary frame")
        if msg.type != WSMsgType.TEXT:
            raise _Close(1000, "closed during handshake")
        if len(msg.data) > PRE_AUTH_MAX_FRAME or len(msg.data.encode("utf-8", "surrogatepass")) > PRE_AUTH_MAX_FRAME:
            raise _Close(1009, "handshake frame too large")
        payload, _ = bhproto.unwrap(msg.data, None)
        if schema.errors("payload-handshake.json", payload):
            raise _Close(4400, "bad handshake payload")
        if payload["type"] == "auth":
            return await self._auth(ws, payload, server_nonce)
        if payload["type"] == "pair":
            return await self._pair(ws, payload, server_nonce, ip)
        raise _Close(4400, "expected auth or pair")

    async def _auth(self, ws, payload: dict, server_nonce: str) -> _Session:
        body = payload["body"]
        if schema.errors("auth.json", body):
            raise _Close(4400, "bad auth body")
        helper = self.store.get_helper(body["helper_id"])
        if helper is None or helper.revoked_at is not None or not self.settings.public_url:
            raise _Close(4401, "unknown or revoked helper")
        signed = bhproto.canonical({"client_nonce": body["client_nonce"], "endpoint": self.settings.public_url,
                                    "helper_id": helper.helper_id, "server_key_id": self.key_id,
                                    "server_nonce": server_nonce})
        if not bhproto.verify(helper.device_pubkey, bhproto.CTX_AUTH_DEVICE,
                              signed, body["device_sig"]):
            raise _Close(4401, "bad device signature or endpoint")
        sid = str(uuid.uuid4())
        sig = bhproto.sign(self.key, bhproto.CTX_AUTH_SERVER, bhproto.canonical({
            "client_nonce": body["client_nonce"], "helper_id": helper.helper_id, "server_nonce": server_nonce,
            "sid": sid}))
        await self._send_handshake(ws, "auth_ok", {"sid": sid, "server_sig": sig, "server_time_ms": now_ms()},
                                   re=payload["id"])
        log.info("borrowed_hands: helper %s authenticated", helper.helper_id[:8])
        return await self._register(ws, helper.helper_id, helper.device_pubkey, sid, helper.name)

    async def _pair(self, ws, payload: dict, server_nonce: str, ip: str) -> _Session:
        now = now_ms()
        wait = self._rl.allow(ip, now)
        if wait is not None:
            await self._send_handshake(ws, "error", {"code": "helper_unavailable", "message": "too many pairing "
                                                     "attempts; try again later", "detail": {"reason": "rate_limited"},
                                                     "retry_after_s": int(wait)}, re=payload["id"])
            raise _Close(4429, "pair rate limited")
        body = payload["body"]
        try:  # every way a pair attempt can fail before the code check counts as a failure (one path)
            if schema.errors("pair.json", body) or not self.settings.public_url:
                raise _Close(4400, "bad pair body")
            code = bhproto.unb64u(body["code"])
            dev_pub = bhproto.unb64u(body["device_pubkey"])
            code_hash = bhproto.code_hash(code)
            signed = bhproto.canonical({"client_nonce": body["client_nonce"], "code_hash": code_hash,
                                        "device_pubkey": body["device_pubkey"], "endpoint": self.settings.public_url,
                                        "server_nonce": server_nonce})
            if not bhproto.verify(dev_pub, bhproto.CTX_PAIR, signed, body["pair_sig"]):
                raise _Close(4400, "bad pair signature")
        except (_Close, bhproto.ProtocolError, ValueError):
            self._pair_failed(now)
            raise
        if not self.store.consume_code(code_hash, now=now):
            self._pair_failed(now)
            await self._send_handshake(ws, "error", {"code": "expired", "message": "pairing code rejected; run "
                                                     "/hands pair again"}, re=payload["id"])
            raise _Close(4410, "pairing code invalid, expired or used")
        helper_id = str(uuid.uuid4())
        paired_by = self.store.code_issued_to(code_hash) or "?"
        self.store.add_helper(helper_id, dev_pub, body["device_name"], now=now, paired_by=paired_by)
        self.store.audit("paired", now=now, helper_id=helper_id, detail={"name": body["device_name"]})
        sid = str(uuid.uuid4())
        sig = bhproto.sign(self.key, bhproto.CTX_PAIR_OK, bhproto.canonical({
            "client_nonce": body["client_nonce"], "device_pubkey": body["device_pubkey"], "helper_id": helper_id,
            "server_nonce": server_nonce, "sid": sid}))
        text = (f"Paired: {body['device_name']} ({helper_id[:8]}); waiting for Mac to advertise capabilities. "
                "Pairing alone grants nothing.")
        await self._send_handshake(ws, "pair_ok", {"helper_id": helper_id, "sid": sid, "server_sig": sig},
                                   re=payload["id"])
        # No caps read begins until _handshake returns. Associate after the send succeeds,
        # before _register/read-loop entry, so a failed send never confirms pairing to the owner.
        self.notifier.associate(code_hash, helper_id, text, now)
        log.info("borrowed_hands: paired helper %s", helper_id[:8])
        return await self._register(ws, helper_id, dev_pub, sid, body["device_name"])

    def _pair_failed(self, now: int) -> None:
        """Every failed pair attempt counts toward the global limit; crossing it invalidates the code."""
        self._rl.failed(now)
        if self._rl.global_tripped(now):
            self.store.invalidate_codes(now=now)
            log.warning("borrowed_hands: too many failed pairing attempts; outstanding code invalidated")

    async def _register(self, ws, helper_id: str, device_pub: bytes, sid: str, name: str) -> _Session:
        now = now_ms()
        sess = _Session(ws=ws, helper_id=helper_id, device_pub=device_pub, sid=sid, auth_at=now, name=name)
        old = self._sessions.get(helper_id)
        self._sessions[helper_id] = sess
        self.notifier.connected(helper_id, sid)
        self.store.set_detached(helper_id, None)
        self.store.touch(helper_id, now=now)
        if old is not None:
            log.info("borrowed_hands: helper %s superseded by a newer connection", helper_id[:8])
            await old.ws.close(code=4409, message=b"superseded")
        return sess

    # ================================================================ authenticated frames
    async def _send(self, sess: _Session, type_: str, body: dict, re: Optional[str] = None) -> str:
        async with sess.lock:
            return await self._send_locked(sess, type_, body, re)

    async def _send_locked(self, sess: _Session, type_: str, body: dict, re: Optional[str] = None,
                           fid: Optional[str] = None) -> str:
        sess.seq_out += 1
        ts = now_ms()
        p = {"type": type_, "id": fid or str(uuid.uuid4()), "sid": sess.sid, "seq": sess.seq_out, "ts": ts,
             "exp": ts + bhproto.MAX_WINDOW_MS, "body": body}
        if re:
            p["re"] = re
        await sess.ws.send_str(bhproto.wrap(p, self.key))
        return p["id"]

    async def _on_frame(self, sess: _Session, text: str) -> None:
        payload, _ = bhproto.unwrap(text, sess.device_pub)
        if schema.errors("payload.json", payload):
            raise _Close(4400, "bad payload")
        if payload["sid"] != sess.sid:
            raise _Close(4400, "wrong sid")
        if payload["seq"] != sess.seq_in + 1:
            raise _Close(4400, "bad seq")
        sess.seq_in += 1
        if payload["id"] in sess.ids_in:
            raise _Close(4400, "repeated id")
        sess.ids_in.add(payload["id"])
        bhproto.check_expiry(payload, now_ms())
        type_, body, fid = payload["type"], payload["body"], payload["id"]
        name = BODY_SCHEMA.get(type_)
        if name is None:
            await self._send(sess, "error", {"code": "invalid_arguments", "message": "unknown message type",
                                             "detail": {"reason": "unknown_type"}}, re=fid)
            return
        if schema.errors(name, body):
            raise _Close(4400, f"bad {type_} body")
        self.store.touch(sess.helper_id, now=now_ms())
        await getattr(self, "_h_" + type_.replace(".", "_"))(sess, body, payload)

    async def _h_caps(self, sess, body, payload):
        if self._sessions.get(sess.helper_id) is not sess:
            return
        if not self.store.set_caps(sess.helper_id, body, now=now_ms()):  # atomic with revoke: no write, no ack
            return
        self.notifier.caps(sess.helper_id, sess.sid, body)
        await self._send(sess, "ack", {}, re=payload["id"])

    async def _h_jobs_sync(self, sess, body, payload):
        acked = []
        for a in body["active"]:
            if self._owned(sess, a["job_id"]):
                sess.reported.add(a["job_id"])
                st = NONTERMINAL_REPORTABLE.get(a["state"])
                if st is not None and self.store.update_state(a["job_id"], st, now=now_ms()):
                    self._changed(a["job_id"])
        for r in body["results"]:
            sess.reported.add(r["job_id"])
            self._apply_result(sess, r)
            acked.append(r["job_id"])
        await self._send(sess, "job.ack", {"job_ids": acked}, re=payload["id"])
        for job in self.store.jobs_nonterminal(sess.helper_id):
            if job.pending_send:
                await self._submit_job(sess, job.job_id)

    async def _h_job_accepted(self, sess, body, payload):
        await self._h_job_state(sess, body, payload)

    def _take_reply(self, sess: _Session, re: Optional[str], kinds: tuple,
                    job_id: Optional[str] = None) -> Optional[_Pending]:
        """Pop the request `re` answers, only if it went to this helper on this session, is of a kind this
        reply may answer, and (when the reply names one) is about the same job. Otherwise leave it alone."""
        p = self._pending.get(re or "")
        if p is None or p.kind not in kinds or p.helper_id != sess.helper_id or p.sid != sess.sid:
            return None
        if job_id is not None and p.job_id != job_id:
            return None
        return self._pending.pop(re)

    async def _h_job_state(self, sess, body, payload):
        if not self._owned(sess, body["job_id"]):
            return
        self._take_reply(sess, payload.get("re"), ("submit",), body["job_id"])
        sess.reported.add(body["job_id"])
        st = NONTERMINAL_REPORTABLE.get(body["state"])
        if st is not None and self.store.update_state(body["job_id"], st, now=now_ms()):
            self._changed(body["job_id"])

    async def _h_job_result(self, sess, body, payload):
        if self._owned(sess, body["job_id"]):
            self._take_reply(sess, payload.get("re"), ("submit",), body["job_id"])
            sess.reported.add(body["job_id"])
        self._apply_result(sess, body)
        await self._send(sess, "job.ack", {"job_ids": [body["job_id"]]}, re=payload["id"])

    def _apply_result(self, sess: _Session, r: dict) -> None:
        job = self.store.get_job(r["job_id"])
        if job is None or job.helper_id != sess.helper_id:
            log.warning("borrowed_hands: helper %s reported a job it does not own", sess.helper_id[:8])
            return
        result, error, state = r["result"], r["error"], JobState(r["state"])
        if result is not None and RESULT_SHAPE[job.action] not in result:
            result, state = None, JobState.FAILED
            error = {"code": "internal_error", "message": "the Mac returned a result of the wrong kind",
                     "detail": {"reason": "bad_result"}}
        if self.store.finish(job.job_id, state, result=result, error=error, now=now_ms()):
            log.info("borrowed_hands: job %s %s", job.job_id[:8], state.value)
            self.store.audit("job_" + state.value, now=now_ms(), helper_id=job.helper_id, job_id=job.job_id)
            self._changed(job.job_id)

    async def _h_error(self, sess, body, payload):
        p = self._take_reply(sess, payload.get("re"), ("submit", "cancel"))
        if p is None:
            log.info("borrowed_hands: helper %s sent an unsolicited error (%s)", sess.helper_id[:8], body["code"])
            return
        if p.kind == "cancel":
            if p.future is not None and not p.future.done():
                p.future.set_result({"type": "error", "body": body})
            return
        state = {"expired": JobState.EXPIRED, "cancelled": JobState.CANCELLED}.get(body["code"], JobState.FAILED)
        if self.store.finish(p.job_id, state, result=None, error=body, now=now_ms()):
            log.info("borrowed_hands: job %s refused by the Mac (%s)", p.job_id[:8], body["code"])
            self._changed(p.job_id)

    async def _h_ack(self, sess, body, payload):
        p = self._take_reply(sess, payload.get("re"), ("cancel",))
        if p is not None and p.future is not None and not p.future.done():
            p.future.set_result({"type": "ack", "body": body})

    async def _h_unpair(self, sess, body, payload):
        self.revoke(sess.helper_id, "unpaired on the Mac")
        self.store.add_notice(f"Unpaired from the Mac: {sess.name} ({sess.helper_id[:8]}).", now=now_ms())
        await self._send(sess, "ack", {}, re=payload["id"])
        raise _Close(1000, "unpaired by helper")

    async def _h_device_rotate(self, sess, body, payload):
        signed = bhproto.canonical({"helper_id": sess.helper_id, "new_pubkey": body["new_pubkey"], "sid": sess.sid})
        new_pub = bhproto.unb64u(body["new_pubkey"])
        if not bhproto.verify(new_pub, bhproto.CTX_ROTATE_DEVICE, signed, body["sig_new"]):
            await self._send(sess, "error", {"code": "denied", "message": "new key signature invalid",
                                             "detail": {"reason": "bad_signature"}}, re=payload["id"])
            return
        self.store.set_device_pubkey(sess.helper_id, new_pub)
        self.store.audit("device_rotate", now=now_ms(), helper_id=sess.helper_id)
        log.info("borrowed_hands: helper %s rotated its device key", sess.helper_id[:8])
        await self._send(sess, "ack", {}, re=payload["id"])

    def _owned(self, sess: _Session, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        return job is not None and job.helper_id == sess.helper_id

    # ================================================================ requests from tools (run on this loop)
    async def _submit_job(self, sess: _Session, job_id: str) -> bool:
        """Send job.submit for a job that has not been delivered: pending_send, or an earlier write that may not
        have arrived (submitted with pending_send set). Before any byte is written the job becomes `submitted`
        (delivery uncertain), so a cancel can no longer call it unsent; if the write fails it stays marked for a
        resend with identical bytes on the next authentication (idempotent on the Mac, PROTOCOL 4.2 step 6)."""
        async with sess.lock:
            job = self.store.get_job(job_id)
            if job is None or self._sessions.get(job.helper_id) is not sess or not job.pending_send \
                    or job.state not in (JobState.PENDING_SEND, JobState.SUBMITTED):
                return False
            self.store.mark_dispatching(job_id, now=now_ms())
            self._changed(job_id)
            fid = str(uuid.uuid4())
            self._pending[fid] = _Pending("submit", job_id, sess.helper_id, sess.sid,
                                          time.monotonic() + self.t.submit_reply_s)
            try:
                await self._send_locked(sess, "job.submit", {"contract": bhproto.b64u(job.contract), "sig": job.sig},
                                        fid=fid)
            except BaseException:
                self._pending.pop(fid, None)
                raise
            if self.store.mark_sent(job_id, now=now_ms()):
                self._changed(job_id)
        log.info("borrowed_hands: job %s submitted to %s", job_id[:8], sess.helper_id[:8])
        return True

    async def submit(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        sess = self._sessions.get(job.helper_id) if job else None
        if sess is None:
            return False
        return await self._submit_job(sess, job_id)

    async def cancel_unsent(self, job_id: str) -> str:
        """Cancel a job that has not been dispatched. Runs on this loop, so it is ordered with _submit_job:
        'cancelled' (no byte of it was ever written), 'dispatched' (use the helper cancel path), 'unknown'."""
        job = self.store.get_job(job_id)
        if job is None:
            return "unknown"
        if job.state is not JobState.PENDING_SEND:
            return "dispatched"
        if self.store.finish(job_id, JobState.CANCELLED, result=None, now=now_ms(), error={
                "code": "cancelled", "message": "cancelled before it reached the Mac"}):
            self._changed(job_id)
        return "cancelled"

    async def cancel(self, job_id: str) -> Optional[dict]:
        job = self.store.get_job(job_id)
        sess = self._sessions.get(job.helper_id) if job else None
        if sess is None:
            return None
        fut = asyncio.get_running_loop().create_future()
        fid = str(uuid.uuid4())
        self._pending[fid] = _Pending("cancel", job_id, sess.helper_id, sess.sid,
                                      time.monotonic() + self.t.cancel_reply_s, fut)
        async with sess.lock:
            await self._send_locked(sess, "job.cancel", {"job_id": job_id}, fid=fid)
        return await fut

    async def unpair(self, helper_id: str, reason: str) -> bool:
        revoked = self.revoke(helper_id, "revoked")
        sess = self._sessions.get(helper_id)
        if sess is not None:
            try:
                await self._send(sess, "unpaired", {"reason": reason[:200]})
            finally:
                await sess.ws.close(code=4403, message=b"unpaired")
        return revoked

    def revoke(self, helper_id: str, reason: str) -> bool:
        """Revoke a helper and cancel its open jobs server-side (PROTOCOL 3.5). Sync: safe from any thread."""
        now = now_ms()
        revoked = self.store.revoke_helper(helper_id, now=now)
        self.notifier.forget(helper_id)
        for job in self.store.jobs_nonterminal(helper_id):
            if self.store.finish(job.job_id, JobState.CANCELLED, result=None, now=now, error={
                    "code": "cancelled", "message": "the Mac was unpaired", "detail": {"reason": "revoked"}}):
                self._changed(job.job_id)
        if revoked:
            self.store.audit("unpaired", now=now, helper_id=helper_id, detail={"by": reason})
            log.info("borrowed_hands: helper %s unpaired (%s)", helper_id[:8], reason)
        return revoked

    # ================================================================ deadlines and reconciliation
    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self.t.tick_s)
            try:
                self._tick()
            except Exception:
                log.exception("borrowed_hands: tick failed")

    def _tick(self) -> None:
        mono, now = time.monotonic(), now_ms()
        for rid, p in list(self._pending.items()):
            if mono < p.deadline:
                continue
            del self._pending[rid]
            if p.kind == "cancel":
                if p.future is not None and not p.future.done():
                    p.future.set_result(None)
            elif self.store.finish(p.job_id, JobState.FAILED, result=None, now=now, error={
                    "code": "timeout", "message": "the Mac did not answer the request in time",
                    "detail": {"reason": "no_reply"}}):
                self._changed(p.job_id)
        # PROTOCOL 4.5: a job the helper does not report within the window after auth, whose contract has
        # expired, is lost.
        for sess in list(self._sessions.values()):
            if now - sess.auth_at < self.t.reconcile_after_ms:
                continue
            for job in self.store.jobs_nonterminal(sess.helper_id):
                if job.pending_send or job.job_id in sess.reported or job.created_at >= sess.auth_at \
                        or job.expires_at >= now:
                    continue
                if self.store.finish(job.job_id, JobState.FAILED, result=None, now=now, error={
                        "code": "internal_error", "message": "the Mac no longer knows this job",
                        "detail": {"reason": "lost"}}):
                    log.info("borrowed_hands: job %s lost", job.job_id[:8])
                    self._changed(job.job_id)
