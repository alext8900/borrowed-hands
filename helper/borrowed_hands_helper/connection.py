"""The helper's side of the wire (PROTOCOL 1 to 3): transport, handshakes, the signed-frame session codec,
reconnect backoff and close-code reactions.

Connection states (one process):
    disconnected -> connecting -> awaiting hello -> awaiting auth_ok -> authenticated -> (closed) -> backoff
    -> connecting ...   and from any state: stopped (4401, 4403, 4409, a changed server key, a failed server
    proof), which ends the process with the exit code SPEC 8.4 gives.
`Session` is pure: every inbound frame is checked in PROTOCOL order (envelope and signature over the received
bytes, payload schema, sid, seq, id, exp, body schema) before anything reads it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol

from . import schema
from ._shared import bhproto
from .config import ServerPin

log = logging.getLogger("borrowed_hands.helper")

SUBPROTOCOL = "bh.v1"

# Inbound (server to helper) message types and their body schemas (PROTOCOL 5).
S2H_BODY = {"job.submit": "job-submit", "job.cancel": "job-cancel", "job.ack": "job-ack",
            "server.rotate": "server-rotate", "unpaired": "unpaired", "ack": "empty", "error": "error"}


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Timing:
    heartbeat_s: float = 20.0      # our WebSocket ping interval
    dead_s: float = 45.0           # no inbound frame, ping or pong for this long: the socket is dead
    handshake_s: float = 10.0
    stable_s: float = 60.0         # a session this long resets the backoff
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 60.0
    connect_s: float = 15.0
    close_s: float = 5.0           # a server that never answers CLOSE cannot keep the helper from exiting


def backoff_delay(attempt: int, rand: Callable[[], float] = random.random, timing: Timing = Timing()) -> float:
    """Full jitter: uniform(0, min(cap, base * 2**attempt)) (PROTOCOL 1.2)."""
    return rand() * min(timing.backoff_cap_s, timing.backoff_base_s * (2 ** min(attempt, 30)))


class Stop(Exception):
    """End the process: the server or the pins say reconnecting cannot help."""

    def __init__(self, exit_code: int, message: str, unpaired: bool = False):
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message
        self.unpaired = unpaired


class Closed(Exception):
    def __init__(self, code: Optional[int], reason: str = ""):
        super().__init__(f"closed {code} {reason}".strip())
        self.code = code


class ConnectFailed(Exception):
    pass


def close_reaction(code: Optional[int]) -> Optional[Stop]:
    """PROTOCOL 1.1: None means reconnect with backoff."""
    if code == 4401:
        return Stop(1, "server does not recognise this Mac; run `bh pair` again")
    if code == 4403:
        return Stop(3, "unpaired by server", unpaired=True)
    if code == 4409:
        return Stop(1, "another connection for this Mac took over (superseded); exiting")
    if code == 4410:
        return Stop(2, "pairing code rejected; run /hands pair again")
    return None


# ------------------------------------------------------------------ transport
class Transport(Protocol):
    async def send(self, text: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self, code: int = 1000) -> None: ...


class AiohttpTransport:
    """One WebSocket. We answer pings and send our own every `heartbeat_s`; anything inbound (text, ping, pong)
    resets the dead timer, and `dead_s` of silence closes the socket (half-open detection)."""

    def __init__(self, session, ws, timing: Timing):
        self._session = session
        self._ws = ws
        self._timing = timing
        self._last_in = time.monotonic()
        self.dead = False
        self._hb = asyncio.create_task(self._heartbeat())

    @classmethod
    async def connect(cls, url: str, timing: Timing) -> "AiohttpTransport":
        import aiohttp
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=timing.connect_s))
        try:
            # connect_s bounds the answer to the upgrade too: the session timeout covers only the TCP and TLS connect
            ws = await asyncio.wait_for(session.ws_connect(
                url, protocols=(SUBPROTOCOL,), autoping=False, heartbeat=None, max_msg_size=bhproto.MAX_FRAME,
                autoclose=True), timing.connect_s)
        except Exception as e:
            await session.close()
            raise ConnectFailed(f"{type(e).__name__}: {e}") from None
        if ws.protocol != SUBPROTOCOL:
            try:
                await asyncio.wait_for(ws.close(code=1002), timing.close_s)
            except asyncio.TimeoutError:
                pass
            await session.close()
            raise ConnectFailed("server did not accept the bh.v1 subprotocol")
        return cls(session, ws, timing)

    async def _heartbeat(self) -> None:
        """Ping every heartbeat_s; close the socket once nothing came in for dead_s (half-open detection).
        A watchdog, because aiohttp's receive(timeout=...) marks the socket abnormal on every timeout."""
        tick = min(self._timing.heartbeat_s, self._timing.dead_s / 4)
        next_ping = time.monotonic() + self._timing.heartbeat_s
        try:
            while not self._ws.closed:
                await asyncio.sleep(tick)
                now = time.monotonic()
                if now - self._last_in > self._timing.dead_s:
                    self.dead = True
                    await asyncio.wait_for(self._ws.close(code=1001), self._timing.close_s)
                    return
                if now >= next_ping:
                    next_ping = now + self._timing.heartbeat_s
                    await self._ws.ping()
        except (asyncio.CancelledError, asyncio.TimeoutError, ConnectionError, RuntimeError):
            pass

    async def send(self, text: str) -> None:
        if self._ws.closed:
            raise Closed(self._ws.close_code)
        await self._ws.send_str(text)

    async def recv(self) -> str:
        import aiohttp
        WT = aiohttp.WSMsgType
        while True:
            msg = await self._ws.receive()
            if self.dead:
                raise Closed(None, "no frame or pong within the dead timeout")
            self._last_in = time.monotonic()
            if msg.type == WT.TEXT:
                return msg.data
            if msg.type == WT.PING:
                await self._ws.pong(msg.data)
            elif msg.type == WT.PONG:
                pass
            elif msg.type == WT.BINARY:
                await self.close(1003)
                raise Closed(1003, "binary frame")
            else:     # CLOSE, CLOSING, CLOSED, ERROR
                raise Closed(self._ws.close_code)

    async def close(self, code: int = 1000) -> None:
        self._hb.cancel()
        try:
            if not self._ws.closed:
                # aiohttp restarts its own wait for every frame that is not CLOSE, so a server that keeps sending
                # could hold this open for good
                await asyncio.wait_for(self._ws.close(code=code), self._timing.close_s)
        except asyncio.TimeoutError:
            pass                         # the server withheld its CLOSE: drop the socket without it
        finally:
            await self._session.close()


# ------------------------------------------------------------------ handshake payloads (unsigned)
def _hs_payload(type_: str, body: dict) -> dict:
    return {"type": type_, "id": str(uuid.uuid4()), "ts": now_ms(), "body": body}


def _parse_handshake(text: str, expect: tuple[str, ...]) -> tuple[str, dict]:
    payload, _ = bhproto.unwrap(text, None)
    try:
        schema.validate("payload-handshake", payload)
    except schema.SchemaError as e:
        raise bhproto.ProtocolError(f"bad handshake frame: {e}") from None
    t = payload["type"]
    if t not in expect:
        raise bhproto.ProtocolError(f"unexpected {t} during the handshake")
    body_schema = {"hello": "hello", "auth_ok": "auth-ok", "pair_ok": "pair-ok", "error": "error"}[t]
    try:
        schema.validate(body_schema, payload["body"])
    except schema.SchemaError as e:
        raise bhproto.ProtocolError(f"bad {t}: {e}") from None
    return t, payload["body"]


async def _recv_hs(t: Transport, timeout: float, expect: tuple[str, ...]) -> tuple[str, dict]:
    try:
        text = await asyncio.wait_for(t.recv(), timeout)
    except asyncio.TimeoutError:
        await t.close(4408)
        raise Closed(4408, "handshake timeout") from None
    return _parse_handshake(text, expect)


# ------------------------------------------------------------------ session codec (signed frames)
@dataclass(frozen=True)
class Frame:
    type: str
    id: str
    re: Optional[str]
    body: dict


class UnknownType(Exception):
    def __init__(self, frame_id: str, type_: str):
        super().__init__(type_)
        self.frame_id = frame_id
        self.type = type_


class BadBody(Exception):
    def __init__(self, frame_id: str, message: str):
        super().__init__(message)
        self.frame_id = frame_id


class Session:
    """Per-session state after auth_ok/pair_ok. `decode` raises bhproto.ProtocolError (close with its
    close_code) for anything PROTOCOL 2 says closes the socket, UnknownType / BadBody for a well-formed,
    authentic frame we cannot use (reply `error invalid_arguments`, keep the session)."""

    def __init__(self, sid: str, device_priv, server_pub: bytes, clock: Callable[[], int] = now_ms):
        self.sid = sid
        self._priv = device_priv
        self._server_pub = server_pub
        self._clock = clock
        self._seq_out = 0
        self._seq_in = 0
        self._seen: set[str] = set()

    def encode(self, type_: str, body: dict, re: Optional[str] = None) -> tuple[str, str]:
        self._seq_out += 1
        ts = self._clock()
        fid = str(uuid.uuid4())
        p = {"type": type_, "id": fid, "sid": self.sid, "seq": self._seq_out, "ts": ts, "exp": ts + 120_000,
             "body": body}
        if re is not None:
            p["re"] = re
        return bhproto.wrap(p, self._priv), fid

    def decode(self, text: str) -> Frame:
        payload, _ = bhproto.unwrap(text, self._server_pub)        # signature over the received bytes
        try:
            schema.validate("payload", payload)
        except schema.SchemaError as e:
            raise bhproto.ProtocolError(f"bad payload: {e}") from None
        if payload["sid"] != self.sid:
            raise bhproto.ProtocolError("frame from another session")
        if payload["seq"] != self._seq_in + 1:
            raise bhproto.ProtocolError(f"seq {payload['seq']} after {self._seq_in}")
        if payload["id"] in self._seen:
            raise bhproto.ProtocolError("repeated frame id")
        bhproto.check_expiry(payload, self._clock())
        self._seq_in += 1
        self._seen.add(payload["id"])
        t = payload["type"]
        if t not in S2H_BODY:
            raise UnknownType(payload["id"], t)
        try:
            schema.validate(S2H_BODY[t], payload["body"])
        except schema.SchemaError as e:
            raise BadBody(payload["id"], str(e)) from None
        return Frame(t, payload["id"], payload.get("re"), payload["body"])


# ------------------------------------------------------------------ handshakes
async def handshake_auth(t: Transport, pin: ServerPin, device_priv, timing: Timing = Timing(),
                         clock: Callable[[], int] = now_ms) -> Session:
    """PROTOCOL 1.3, helper side. Raises Stop on a changed or unproven server key, Closed if the server
    closes (the caller applies close_reaction)."""
    _, hello = await _recv_hs(t, timing.handshake_s, ("hello",))
    if hello["server_key_id"] != pin.key_id:
        await t.close(1000)
        raise Stop(1, "server key changed; re-pair")
    cn = bhproto.b64u(os.urandom(32))
    signed = bhproto.canonical({"client_nonce": cn, "endpoint": pin.url, "helper_id": pin.helper_id,
                                "server_key_id": pin.key_id, "server_nonce": hello["server_nonce"]})
    await t.send(bhproto.wrap(_hs_payload("auth", {
        "helper_id": pin.helper_id, "client_nonce": cn,
        "device_sig": bhproto.sign(device_priv, bhproto.CTX_AUTH_DEVICE, signed)}), None))
    kind, body = await _recv_hs(t, timing.handshake_s, ("auth_ok", "error"))
    if kind == "error":
        raise Closed(None, f"server error during auth: {body.get('code')}")
    proof = bhproto.canonical({"client_nonce": cn, "helper_id": pin.helper_id,
                               "server_nonce": hello["server_nonce"], "sid": body["sid"]})
    if not bhproto.verify(pin.pubkey, bhproto.CTX_AUTH_SERVER, proof, body["server_sig"]):
        await t.close(4401)
        raise Stop(1, "server failed to prove its key")
    return Session(body["sid"], device_priv, pin.pubkey, clock)


@dataclass(frozen=True)
class DeviceInfo:
    device_name: str
    helper_version: str
    os_version: str
    arch: str


async def handshake_pair(t: Transport, endpoint: str, code: bytes, server_pub: bytes, device_priv,
                         info: DeviceInfo, timing: Timing = Timing(),
                         clock: Callable[[], int] = now_ms) -> tuple[str, Session]:
    """PROTOCOL 3.3, helper side. Returns (helper_id, session). Writes nothing: the caller persists the key
    and pins only after this returns, i.e. only after the server proved the key from the pairing string."""
    _, hello = await _recv_hs(t, timing.handshake_s, ("hello",))
    if hello["server_key_id"] != bhproto.key_id(server_pub):
        await t.close(1000)
        raise Stop(3, "server failed to prove its key; check the pairing string")
    cn = bhproto.b64u(os.urandom(32))
    dpub = bhproto.b64u(bhproto.pub_raw(device_priv))
    signed = bhproto.canonical({"client_nonce": cn, "code_hash": bhproto.code_hash(code), "device_pubkey": dpub,
                                "endpoint": endpoint, "server_nonce": hello["server_nonce"]})
    await t.send(bhproto.wrap(_hs_payload("pair", {
        "code": bhproto.b64u(code), "device_pubkey": dpub, "device_name": info.device_name, "client_nonce": cn,
        "helper_version": info.helper_version, "os": "macos", "os_version": info.os_version, "arch": info.arch,
        "pair_sig": bhproto.sign(device_priv, bhproto.CTX_PAIR, signed)}), None))
    try:
        kind, body = await _recv_hs(t, timing.handshake_s, ("pair_ok", "error"))
    except Closed as c:
        raise (close_reaction(c.code) or c) from None
    if kind == "error":
        if body["code"] in ("expired", "denied", "invalid_arguments"):
            raise Stop(2, "pairing code rejected; run /hands pair again")
        raise Stop(4, f"cannot pair: {body['code']}: {body['message'][:200]}")
    proof = bhproto.canonical({"client_nonce": cn, "device_pubkey": dpub, "helper_id": body["helper_id"],
                               "server_nonce": hello["server_nonce"], "sid": body["sid"]})
    if not bhproto.verify(server_pub, bhproto.CTX_PAIR_OK, proof, body["server_sig"]):
        await t.close(4401)
        raise Stop(3, "server failed to prove its key; check the pairing string")
    return body["helper_id"], Session(body["sid"], device_priv, server_pub, clock)


# ------------------------------------------------------------------ reconnect loop
Connect = Callable[[str, Timing], Awaitable[Transport]]
OnSession = Callable[[Session, Transport], Awaitable[None]]


async def run_forever(get_pin: Callable[[], ServerPin], get_key: Callable[[], object], on_session: OnSession, *,
                      timing: Timing = Timing(), connect: Connect = AiohttpTransport.connect,
                      rand: Callable[[], float] = random.random, sleep=asyncio.sleep,
                      clock: Callable[[], int] = now_ms, on_status: Callable[[str, str], None] = lambda s, d: None,
                      retry_after: Callable[[], Optional[int]] = lambda: None) -> Stop:
    """Reconnect forever (PROTOCOL 1.2). Returns the Stop that ended it."""
    attempt = 0
    while True:
        pin = get_pin()
        started = None
        code: Optional[int] = None
        try:
            on_status("connecting", pin.url)
            t = await connect(pin.url, timing)
            close_with = 1001
            try:
                session = await handshake_auth(t, pin, get_key(), timing, clock)
                started = time.monotonic()
                on_status("connected", session.sid[:8])
                log.info("authenticated, session %s", session.sid[:8])
                await on_session(session, t)
                code = close_with = 1000
            except bhproto.ProtocolError as e:
                log.warning("protocol violation from server: %s; closing %s", e, e.close_code)
                code = close_with = e.close_code
            except Closed as c:
                code, close_with = c.code, 1000
                log.info("connection closed (%s) %s", code, c)
            except Stop:
                close_with = 1000
                raise
            finally:                     # every transport we created is closed, on every path
                try:
                    await asyncio.wait_for(t.close(close_with), timing.close_s)
                except Exception:
                    pass
        except Stop as s:
            on_status("stopped", s.message)
            return s
        except ConnectFailed as e:
            on_status("disconnected", f"cannot reach server: {e}")
            log.info("cannot reach server: %s", e)
        except (OSError, asyncio.TimeoutError) as e:
            on_status("disconnected", f"{type(e).__name__}: {e}")
        stop = close_reaction(code)
        if stop is not None:
            on_status("stopped", stop.message)
            return stop
        if started is not None:
            on_status("disconnected", f"connection closed ({code})")
            if time.monotonic() - started >= timing.stable_s:
                attempt = 0
        delay = backoff_delay(attempt, rand, timing)
        if code == 4429:
            delay = max(delay, float(retry_after() or 60))
        attempt += 1
        await sleep(delay)
