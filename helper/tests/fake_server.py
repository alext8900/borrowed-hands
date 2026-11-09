"""A scripted Borrowed Hands server for helper tests (no plugin needed).

It implements the server side of PROTOCOL 1.3 (hello/auth/auth_ok), 3.3 (pair), signed frames with sid/seq/exp,
and auto-replies (`ack` to caps/unpair/device.rotate, `job.ack` to job.result and jobs.sync results). Every
connection has its own sid and seq. Tests watch the **current** connection (the latest authenticated one): its
frames land in `inbox`, and `send`/`submit` go to it. Only binds 127.0.0.1.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from typing import Optional

from aiohttp import WSMsgType, web

from borrowed_hands_helper._shared import bhproto

PATH = "/bh/v1/ws"


def bhproto_now() -> int:
    return int(time.time() * 1000)


class Conn:
    def __init__(self, ws, sid, helper_id):
        self.ws, self.sid, self.helper_id, self.seq = ws, sid, helper_id, 0


class FakeServer:
    def __init__(self, priv=None, bad_server_sig=False, pair_error: Optional[str] = None):
        self.priv = priv or bhproto.new_private_key()
        self.pub = bhproto.pub_raw(self.priv)
        self.kid = bhproto.key_id(self.pub)
        self.helpers: dict[str, bytes] = {}          # helper_id -> device pubkey
        self.revoked: set[str] = set()
        self.codes: dict[str, bool] = {}             # code_hash -> used
        self.inbox: asyncio.Queue = asyncio.Queue()  # (type, payload) from the current connection
        self.events: list = []
        self.bad_server_sig = bad_server_sig
        self.pair_error = pair_error
        self.fail_next = 0
        self.fail_status = 502
        self.auto_ack = True
        self.connections = 0
        self.auths = 0
        self.conn: Optional[Conn] = None
        self.helper_id: Optional[str] = None
        self.paused = False
        self._runner = None
        self.url = None

    # ---------------------------------------------------------------- lifecycle
    async def start(self):
        app = web.Application(client_max_size=bhproto.MAX_FRAME)
        app.router.add_get(PATH, self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}{PATH}"
        return self

    async def stop(self):
        if self.conn is not None and not self.conn.ws.closed:
            await self.conn.ws.close(code=1001)
        await self._runner.cleanup()

    def pairing_string(self) -> str:
        code = os.urandom(16)
        self.codes[hashlib.sha256(code).hexdigest()] = False
        return bhproto.pairing_string(self.url, code, self.pub)

    # ---------------------------------------------------------------- test controls
    def fail_upgrades(self, n: int, status: int = 502):
        self.fail_next = n
        self.fail_status = status

    async def send(self, type_: str, body: dict, re: Optional[str] = None, *, seq=None, sid=None, ts=None,
                   exp=None, fid=None, priv=None, conn: Optional[Conn] = None) -> str:
        conn = conn or self.conn
        conn.seq += 1
        ts = bhproto_now() if ts is None else ts
        p = {"type": type_, "id": fid or str(uuid.uuid4()), "sid": sid or conn.sid,
             "seq": conn.seq if seq is None else seq, "ts": ts, "exp": ts + 120_000 if exp is None else exp,
             "body": body}
        if re:
            p["re"] = re
        await conn.ws.send_str(bhproto.wrap(p, priv or self.priv))
        return p["id"]

    def contract(self, helper_id, action="system.info", params=None, **kw) -> tuple[dict, bytes, str]:
        now = bhproto_now()
        c = {"v": 1, "job_id": str(uuid.uuid4()), "nonce": bhproto.b64u(os.urandom(16)), "helper_id": helper_id,
             "server_key_id": self.kid, "action": action, "params": params if params is not None else {},
             "issued_at": now, "expires_at": now + 300_000, "approval_ref": "none"}
        c.update(kw)
        raw = bhproto.canonical(c)
        return c, raw, bhproto.sign(self.priv, bhproto.CTX_JOB, raw)

    async def submit(self, raw: bytes, sig: str) -> str:
        return await self.send("job.submit", {"contract": bhproto.b64u(raw), "sig": sig})

    async def close_helper(self, code: int):
        await self.conn.ws.close(code=code)

    async def next(self, type_: Optional[str] = None, timeout: float = 5.0) -> dict:
        """Next payload from the current connection (optionally of one type, skipping others)."""
        while True:
            t, p = await asyncio.wait_for(self.inbox.get(), timeout)
            if type_ is None or t == type_:
                return p

    def pause(self, on: bool = True):
        """Simulate a half-open socket: stop reading frames and stop answering pings."""
        self.paused = on
        if on and self.conn is not None:
            self.conn.ws._autoping = False

    # ---------------------------------------------------------------- server
    def _hs(self, type_, body):
        return bhproto.wrap({"type": type_, "id": str(uuid.uuid4()), "ts": bhproto_now(), "body": body}, None)

    async def _handle(self, request):
        self.connections += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            return web.Response(status=self.fail_status, text="upstream unavailable")
        ws = web.WebSocketResponse(protocols=("bh.v1",), max_msg_size=bhproto.MAX_FRAME, autoping=True)
        await ws.prepare(request)
        nonce = bhproto.b64u(os.urandom(32))
        await ws.send_str(self._hs("hello", {"server_nonce": nonce, "server_key_id": self.kid,
                                             "server_time_ms": bhproto_now(), "max_frame": bhproto.MAX_FRAME,
                                             "v": 1}))
        msg = await ws.receive()
        if msg.type != WSMsgType.TEXT:
            return ws
        p, _ = bhproto.unwrap(msg.data, None)
        if p["type"] == "pair":
            conn = await self._pair(ws, p["body"], nonce)
        elif p["type"] == "auth":
            conn = await self._auth(ws, p["body"], nonce)
        else:
            conn = None
            await ws.close(code=4400)
        if conn is None:
            return ws
        old, self.conn = self.conn, conn
        self.helper_id = conn.helper_id
        if old is not None and not old.ws.closed:
            await old.ws.close(code=4409)
        await self._loop(conn)
        return ws

    async def _pair(self, ws, b, server_nonce) -> Optional[Conn]:
        ch = hashlib.sha256(bhproto.unb64u(b["code"])).hexdigest()
        signed = bhproto.canonical({"client_nonce": b["client_nonce"], "code_hash": ch,
                                    "device_pubkey": b["device_pubkey"], "endpoint": self.url,
                                    "server_nonce": server_nonce})
        dpub = bhproto.unb64u(b["device_pubkey"])
        if self.pair_error or not bhproto.verify(dpub, bhproto.CTX_PAIR, signed, b["pair_sig"]) \
                or self.codes.get(ch) is not False:
            await ws.send_str(self._hs("error", {"code": self.pair_error or "expired",
                                                 "message": "pairing code rejected"}))
            await ws.close(code=4410)
            return None
        self.codes[ch] = True
        helper_id, sid = str(uuid.uuid4()), str(uuid.uuid4())
        self.helpers[helper_id] = dpub
        self.events.append(("paired", helper_id, b["device_name"]))
        proof = bhproto.canonical({"client_nonce": b["client_nonce"], "device_pubkey": b["device_pubkey"],
                                   "helper_id": helper_id, "server_nonce": server_nonce, "sid": sid})
        sig_priv = bhproto.new_private_key() if self.bad_server_sig else self.priv
        await ws.send_str(self._hs("pair_ok", {"helper_id": helper_id, "sid": sid,
                                               "server_sig": bhproto.sign(sig_priv, bhproto.CTX_PAIR_OK, proof)}))
        return Conn(ws, sid, helper_id)

    async def _auth(self, ws, b, server_nonce) -> Optional[Conn]:
        hid = b["helper_id"]
        self.events.append(("auth_attempt", hid))
        pub = self.helpers.get(hid)
        signed = bhproto.canonical({"client_nonce": b["client_nonce"], "endpoint": self.url, "helper_id": hid,
                                    "server_key_id": self.kid, "server_nonce": server_nonce})
        if pub is None or hid in self.revoked or not bhproto.verify(pub, bhproto.CTX_AUTH_DEVICE, signed,
                                                                     b["device_sig"]):
            await ws.close(code=4403 if hid in self.revoked else 4401)
            return None
        self.auths += 1
        sid = str(uuid.uuid4())
        proof = bhproto.canonical({"client_nonce": b["client_nonce"], "helper_id": hid,
                                   "server_nonce": server_nonce, "sid": sid})
        sig_priv = bhproto.new_private_key() if self.bad_server_sig else self.priv
        await ws.send_str(self._hs("auth_ok", {"sid": sid, "server_time_ms": bhproto_now(),
                                               "server_sig": bhproto.sign(sig_priv, bhproto.CTX_AUTH_SERVER, proof)}))
        return Conn(ws, sid, hid)

    async def _loop(self, conn: Conn):
        expect = 1
        async for msg in conn.ws:
            if self.paused and conn is self.conn:
                continue
            if msg.type != WSMsgType.TEXT:
                break
            try:
                p, _ = bhproto.unwrap(msg.data, self.helpers[conn.helper_id])
                assert p["sid"] == conn.sid and p["seq"] == expect, (p["sid"], p["seq"], expect)
                bhproto.check_expiry(p, bhproto_now())
            except Exception as e:      # the helper broke the protocol: record it and close
                self.events.append(("helper_violation", repr(e)))
                await conn.ws.close(code=4400)
                break
            expect += 1
            if conn is self.conn:
                await self.inbox.put((p["type"], p))
            if not self.auto_ack:
                continue
            t = p["type"]
            try:
                if t in ("caps", "unpair"):
                    await self.send("ack", {}, re=p["id"], conn=conn)
                elif t == "device.rotate":
                    self.helpers[conn.helper_id] = bhproto.unb64u(p["body"]["new_pubkey"])
                    await self.send("ack", {}, re=p["id"], conn=conn)
                elif t == "jobs.sync":
                    ids = [r["job_id"] for r in p["body"]["results"]]
                    await self.send("job.ack", {"job_ids": ids}, re=p["id"], conn=conn)
                elif t == "job.result":
                    await self.send("job.ack", {"job_ids": [p["body"]["job_id"]]}, re=p["id"], conn=conn)
            except ConnectionError:
                break
