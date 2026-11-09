"""A scripted fake Mac helper speaking protocol v1 with bhproto (tests only).

It pairs, authenticates, sends caps and jobs.sync, and answers job.submit for system.info. Every server frame
is verified (server signature over the received bytes, sid, seq, expiry). Low-level `send_raw` lets tests inject
hostile frames.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "protocol"))
import bhproto as bp  # noqa: E402

INFO = {"os_version": "26.2", "os_build": "25C56", "arch": "arm64", "cpu": "Apple M4", "memory_gb": 16,
        "xcode": {"version": "26.1", "build": "17B55"}, "disk_free_gb": 100}
CAPS = {"helper_version": "0.1.0", "name": "Fake Mac", "os": "macos", "os_version": "26.2", "arch": "arm64",
        "busy": False, "queue_depth": 0, "capabilities": [{"name": "system.info", "v": 1}]}


def now_ms() -> int:
    return int(time.time() * 1000)


class Closed(Exception):
    def __init__(self, code):
        super().__init__(f"closed {code}")
        self.code = code


class FakeHelper:
    def __init__(self, endpoint: str, server_pub: bytes, device_key=None, helper_id: Optional[str] = None,
                 name: str = "Fake Mac"):
        self.endpoint = endpoint
        self.server_pub = server_pub
        self.key = device_key or bp.new_private_key()
        self.helper_id = helper_id
        self.name = name
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.sid: Optional[str] = None
        self.seq_out = 0
        self.seq_in = 0
        self.hello: Optional[dict] = None
        self.contracts: list = []

    @classmethod
    def from_pairing(cls, s: str, **kw) -> "FakeHelper":
        endpoint, code, pub = bp.parse_pairing(s, allow_insecure_local=True)
        h = cls(endpoint, pub, **kw)
        h.code = code
        return h

    # ------------------------------------------------------------ transport
    async def open(self, protocols=("bh.v1",), headers=None) -> dict:
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(self.endpoint, protocols=protocols, headers=headers or {},
                                                max_msg_size=bp.MAX_FRAME, heartbeat=None)
        self.hello = await self.recv_handshake()
        return self.hello

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
        if self.session is not None:
            await self.session.close()

    async def _recv_text(self, timeout=10.0) -> str:
        msg = await self.ws.receive(timeout=timeout)
        if msg.type == aiohttp.WSMsgType.CLOSE:
            raise Closed(msg.data)  # the code the peer sent; ws.close_code can still read 1006 here
        if msg.type in (aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
            raise Closed(self.ws.close_code)
        assert msg.type == aiohttp.WSMsgType.TEXT, msg
        return msg.data

    async def recv_handshake(self, timeout=10.0) -> dict:
        payload, _ = bp.unwrap(await self._recv_text(timeout), None)
        return payload

    async def recv(self, timeout=10.0) -> dict:
        payload, _ = bp.unwrap(await self._recv_text(timeout), self.server_pub)
        assert payload["sid"] == self.sid
        self.seq_in += 1
        assert payload["seq"] == self.seq_in, (payload["seq"], self.seq_in)
        bp.check_expiry(payload, now_ms())
        return payload

    async def expect_close(self, timeout=12.0) -> int:
        while True:
            try:
                await self._recv_text(timeout)
            except Closed as c:
                return c.code

    async def send_raw(self, text: str):
        await self.ws.send_str(text)

    def handshake_frame(self, type_: str, body: dict, re: Optional[str] = None) -> str:
        p = {"type": type_, "id": str(uuid.uuid4()), "ts": now_ms(), "body": body}
        if re:
            p["re"] = re
        return bp.wrap(p, None)

    def frame(self, type_: str, body: dict, re: Optional[str] = None, **override) -> dict:
        self.seq_out += 1
        ts = now_ms()
        p = {"type": type_, "id": str(uuid.uuid4()), "sid": self.sid, "seq": self.seq_out, "ts": ts,
             "exp": ts + 120_000, "body": body}
        if re:
            p["re"] = re
        p.update(override)
        return p

    async def send(self, type_: str, body: dict, re: Optional[str] = None, **override) -> dict:
        p = self.frame(type_, body, re, **override)
        await self.ws.send_str(bp.wrap(p, self.key))
        return p

    # ------------------------------------------------------------ handshakes
    async def pair(self, name: Optional[str] = None, code: Optional[bytes] = None) -> dict:
        await self.open()
        code = code if code is not None else self.code
        dev_pub = bp.b64u(bp.pub_raw(self.key))
        client_nonce = bp.b64u(os.urandom(32))
        sig = bp.sign(self.key, bp.CTX_PAIR, bp.canonical({
            "client_nonce": client_nonce, "code_hash": bp.code_hash(code), "device_pubkey": dev_pub,
            "endpoint": self.endpoint, "server_nonce": self.hello["body"]["server_nonce"]}))
        await self.send_raw(self.handshake_frame("pair", {
            "code": bp.b64u(code), "device_pubkey": dev_pub, "device_name": name or self.name,
            "client_nonce": client_nonce, "helper_version": "0.1.0", "os": "macos", "os_version": "26.2",
            "arch": "arm64", "pair_sig": sig}))
        reply = await self.recv_handshake()
        if reply["type"] != "pair_ok":
            return reply
        b = reply["body"]
        assert bp.verify(self.server_pub, bp.CTX_PAIR_OK, bp.canonical({
            "client_nonce": client_nonce, "device_pubkey": dev_pub, "helper_id": b["helper_id"],
            "server_nonce": self.hello["body"]["server_nonce"], "sid": b["sid"]}), b["server_sig"])
        self.helper_id, self.sid = b["helper_id"], b["sid"]
        self.seq_in = self.seq_out = 0
        return reply

    async def auth(self, endpoint: Optional[str] = None, key=None) -> dict:
        await self.open()
        client_nonce = bp.b64u(os.urandom(32))
        sig = bp.sign(key or self.key, bp.CTX_AUTH_DEVICE, bp.canonical({
            "client_nonce": client_nonce, "endpoint": endpoint or self.endpoint, "helper_id": self.helper_id,
            "server_key_id": bp.key_id(self.server_pub), "server_nonce": self.hello["body"]["server_nonce"]}))
        await self.send_raw(self.handshake_frame("auth", {"helper_id": self.helper_id, "client_nonce": client_nonce,
                                                          "device_sig": sig}))
        reply = await self.recv_handshake()
        b = reply["body"]
        assert reply["type"] == "auth_ok"
        assert bp.verify(self.server_pub, bp.CTX_AUTH_SERVER, bp.canonical({
            "client_nonce": client_nonce, "helper_id": self.helper_id,
            "server_nonce": self.hello["body"]["server_nonce"], "sid": b["sid"]}), b["server_sig"])
        self.sid = b["sid"]
        self.seq_in = self.seq_out = 0
        return reply

    async def startup(self, caps: Optional[dict] = None, active=(), results=()):
        """caps (seq 1) and jobs.sync (seq 2), then read the two replies."""
        await self.send("caps", caps or {**CAPS, "name": self.name})
        a = await self.recv()
        assert a["type"] == "ack", a
        await self.send("jobs.sync", {"active": list(active), "results": list(results)})
        b = await self.recv()
        assert b["type"] == "job.ack", b
        return b

    # ------------------------------------------------------------ jobs
    def verify_contract(self, body: dict) -> dict:
        raw = bp.unb64u(body["contract"])
        assert bp.verify(self.server_pub, bp.CTX_JOB, raw, body["sig"]), "bad contract signature"
        c = bp.load_strict(raw)
        assert c["helper_id"] == self.helper_id
        assert c["server_key_id"] == bp.key_id(self.server_pub)
        assert c["expires_at"] - c["issued_at"] <= 600_000
        self.contracts.append((raw, c))
        return c

    async def answer_system_info(self, submit: dict, info: Optional[dict] = None):
        c = self.verify_contract(submit["body"])
        await self.send("job.accepted", {"job_id": c["job_id"], "state": "queued", "at": now_ms(), "position": 0},
                        re=submit["id"])
        await self.send("job.result", {"job_id": c["job_id"], "state": "succeeded", "result": info or INFO,
                                       "error": None})
        ack = await self.recv()
        assert ack["type"] == "job.ack" and c["job_id"] in ack["body"]["job_ids"], ack
        return c


def run(coro):
    return asyncio.run(coro)


def dumps(x: Any) -> str:
    return json.dumps(x)
