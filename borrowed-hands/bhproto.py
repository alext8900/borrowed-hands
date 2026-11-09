"""Borrowed Hands protocol v1 primitives, shared by the Hermes plugin and the Mac helper.

PROTOCOL.md is the authority: 2.2 (signed envelope), 2.3 (canonical JSON, the RFC 8785 subset), 3.1 (pairing
string, key id), 4.1 (contract signature, repo fingerprint). `protocol/bhproto.py` is the source;
`borrowed-hands/bhproto.py` is a byte-identical copy (a test enforces it). Stdlib plus `cryptography` only.

Every parse here is strict and raises `ProtocolError` (with the WebSocket close code a receiver should use).
Signatures are always verified over the bytes as received, never over a re-serialization.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Any, Optional, Tuple
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

V = 1
MAX_FRAME = 16777216
MAX_INT = 2**53 - 1
MAX_WINDOW_MS = 120_000          # PROTOCOL 2.3: ts < exp <= ts + 120000
SKEW_MS = 60_000                 # PROTOCOL 2.3: reject if now > exp + 60000

CTX_FRAME = b"bh1/frame\0"
CTX_JOB = b"bh1/job\0"
CTX_AUTH_DEVICE = b"bh1/auth-device\0"
CTX_AUTH_SERVER = b"bh1/auth-server\0"
CTX_PAIR = b"bh1/pair\0"
CTX_PAIR_OK = b"bh1/pair-ok\0"
CTX_ROTATE_DEVICE = b"bh1/rotate-device\0"
CTX_ROTATE_SERVER = b"bh1/rotate-server\0"

_KEY_RE = re.compile(r"[a-z0-9_]+\Z")
_B64U_RE = re.compile(r"[A-Za-z0-9_-]*\Z")
_WS_PATH = "/bh/v1/ws"


class ProtocolError(ValueError):
    """A peer sent something outside the protocol. `close_code` is what the receiver closes with."""

    def __init__(self, message: str, close_code: int = 4400):
        super().__init__(message)
        self.close_code = close_code


# ---------------------------------------------------------------- canonical JSON (PROTOCOL 2.3)
def _check(v: Any, depth: int = 0) -> None:
    if depth > 64:
        raise ProtocolError("nesting too deep")
    if v is None or isinstance(v, bool):
        return
    if isinstance(v, str):
        try:
            v.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProtocolError("lone surrogate") from exc
        return
    if isinstance(v, int):
        if not -MAX_INT <= v <= MAX_INT:
            raise ProtocolError("integer out of range")
        return
    if isinstance(v, float):
        raise ProtocolError("float")
    if isinstance(v, list):
        for x in v:
            _check(x, depth + 1)
        return
    if isinstance(v, dict):
        for k, x in v.items():
            if not isinstance(k, str) or not _KEY_RE.match(k):
                raise ProtocolError("key not [a-z0-9_]+")
            _check(x, depth + 1)
        return
    raise ProtocolError(f"type {type(v).__name__} not allowed")


def canonical(obj: Any) -> bytes:
    """C(x): RFC 8785 restricted to the PROTOCOL 2.3 subset. Raises ProtocolError outside the subset."""
    _check(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _pairs(items):
    d = {}
    for k, v in items:
        if k in d:
            raise ProtocolError("duplicate key")
        d[k] = v
    return d


def _no_float(_s):
    raise ProtocolError("float")


def _no_const(c):
    raise ProtocolError(f"non-finite number {c}")


def load_strict(raw: bytes) -> Any:
    """Parse UTF-8 JSON bytes, rejecting duplicate keys, floats, NaN/Infinity, trailing data and anything
    outside the canonical subset."""
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("load_strict takes bytes")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("invalid UTF-8") from exc
    try:
        v = json.loads(text, object_pairs_hook=_pairs, parse_float=_no_float, parse_constant=_no_const)
    except ProtocolError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    _check(v)
    return v


# ---------------------------------------------------------------- base64url without padding
def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(b)).rstrip(b"=").decode("ascii")


def unb64u(s: str) -> bytes:
    """Strict base64url decode: alphabet [A-Za-z0-9_-], no padding, canonical trailing bits."""
    if not isinstance(s, str) or not _B64U_RE.match(s) or len(s) % 4 == 1:
        raise ProtocolError("bad base64url")
    try:
        out = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError("bad base64url") from exc
    if b64u(out) != s:
        raise ProtocolError("non-canonical base64url")
    return out


# ---------------------------------------------------------------- keys and signatures
def priv_from_seed(seed: bytes) -> Ed25519PrivateKey:
    if len(seed) != 32:
        raise ProtocolError("Ed25519 seed must be 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(bytes(seed))


def new_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def seed_of(priv: Ed25519PrivateKey) -> bytes:
    return priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())


def pub_raw(priv: Ed25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def key_id(pub: bytes) -> str:
    """First 16 hex chars of sha256(raw public key) (PROTOCOL 3.1)."""
    return hashlib.sha256(bytes(pub)).hexdigest()[:16]


def sign(priv: Ed25519PrivateKey, context: bytes, data: bytes) -> str:
    """b64u(Ed25519(priv, context || data)). `context` includes its trailing NUL."""
    return b64u(priv.sign(bytes(context) + bytes(data)))


def verify(pub: bytes, context: bytes, data: bytes, sig_b64u: str) -> bool:
    """True only for a valid signature over context || data. Never raises on bad input."""
    try:
        sig = unb64u(sig_b64u)
        if len(sig) != 64 or len(pub) != 32:
            return False
        Ed25519PublicKey.from_public_bytes(bytes(pub)).verify(sig, bytes(context) + bytes(data))
        return True
    except (InvalidSignature, ProtocolError, ValueError, TypeError):
        return False


# ---------------------------------------------------------------- repo fingerprint (PROTOCOL 4.1)
def normalize_repo_url(url: str) -> str:
    if not isinstance(url, str):
        raise ProtocolError("not a plain https URL")
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError as exc:
        raise ProtocolError("not a plain https URL") from exc
    if (p.scheme.lower() != "https" or p.username is not None or p.password is not None or port is not None
            or p.query or p.fragment or "#" in url or "?" in url or not p.hostname or "@" in p.netloc
            or ":" in p.netloc):
        raise ProtocolError("not a plain https URL")
    path = p.path
    if path.endswith("/"):
        path = path[:-1]
    if path.endswith(".git"):
        path = path[:-4]
    return f"https://{p.hostname.lower()}{path}"


def repo_fingerprint(url: str) -> str:
    return hashlib.sha256(b"bh1/repo\0" + normalize_repo_url(url).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- pairing string (PROTOCOL 3.1)
def code_hash(code: bytes) -> str:
    return hashlib.sha256(bytes(code)).hexdigest()


def _check_endpoint(endpoint: str, allow_insecure_local: bool) -> None:
    if "#" in endpoint or "?" in endpoint or any(c.isspace() for c in endpoint):
        raise ProtocolError("bad endpoint")
    try:
        p = urlsplit(endpoint)
        p.port
    except ValueError as exc:
        raise ProtocolError("bad endpoint") from exc
    if p.path != _WS_PATH or not p.hostname or p.username is not None or p.password is not None:
        raise ProtocolError("bad endpoint")
    if p.scheme == "wss":
        return
    if p.scheme == "ws" and allow_insecure_local and p.hostname in ("127.0.0.1", "localhost"):
        return
    raise ProtocolError("endpoint must be wss:// (ws:// only to localhost with --insecure-local)")


def pairing_string(endpoint: str, code: bytes, server_pub: bytes) -> str:
    return f"bh1:{endpoint}#{b64u(code)}.{b64u(server_pub)}"


def parse_pairing(s: str, allow_insecure_local: bool = False) -> Tuple[str, bytes, bytes]:
    """`bh1:<endpoint>#<code>.<server_pubkey>` -> (endpoint, code_bytes, server_pub_raw)."""
    if not isinstance(s, str) or not s.startswith("bh1:"):
        raise ProtocolError("pairing string must start with bh1:")
    endpoint, sep, frag = s[4:].rpartition("#")
    if not sep:
        raise ProtocolError("pairing string has no '#'")
    parts = frag.split(".")
    if len(parts) != 2 or len(parts[0]) != 22 or len(parts[1]) != 43:
        raise ProtocolError("pairing code or key has the wrong length")
    code, pub = unb64u(parts[0]), unb64u(parts[1])
    _check_endpoint(endpoint, allow_insecure_local)
    return endpoint, code, pub


# ---------------------------------------------------------------- envelopes (PROTOCOL 2.1, 2.2)
def wrap(payload: dict, priv: Optional[Ed25519PrivateKey]) -> str:
    """Serialize a payload into an envelope; signed with `priv` (frame context) when given."""
    raw = canonical(payload)
    env = {"v": V, "p": b64u(raw)}
    if priv is not None:
        env["s"] = sign(priv, CTX_FRAME, raw)
    return canonical(env).decode("utf-8")


def unwrap(text: str, peer_pub: Optional[bytes]) -> Tuple[dict, bytes]:
    """Parse an envelope. With `peer_pub`, a valid frame signature over the received payload bytes is required;
    without it, the envelope must be unsigned (handshake). Returns (payload, payload_bytes)."""
    if isinstance(text, str):
        data = text.encode("utf-8", "surrogatepass")
    else:
        data = bytes(text)
    if len(data) > MAX_FRAME:
        raise ProtocolError("frame too large", 1009)
    env = load_strict(data)
    if not isinstance(env, dict) or type(env.get("v")) is not int or env["v"] != V \
            or not isinstance(env.get("p"), str):
        raise ProtocolError("bad envelope")
    expected = {"v", "p", "s"} if peer_pub is not None else {"v", "p"}
    if set(env) != expected:
        raise ProtocolError("bad envelope fields")
    raw = unb64u(env["p"])
    if peer_pub is not None and not verify(peer_pub, CTX_FRAME, raw, env["s"]):
        raise ProtocolError("bad frame signature")
    payload = load_strict(raw)
    if not isinstance(payload, dict):
        raise ProtocolError("payload is not an object")
    return payload, raw


def check_expiry(payload: dict, now_ms: int) -> None:
    """PROTOCOL 2.3: ts < exp <= ts + 120000, and now <= exp + 60000. Raises ProtocolError (4400)."""
    ts, exp = payload.get("ts"), payload.get("exp")
    if type(ts) is not int or type(exp) is not int:
        raise ProtocolError("ts and exp must be integers")
    if not ts < exp <= ts + MAX_WINDOW_MS:
        raise ProtocolError("bad exp window")
    if now_ms > exp + SKEW_MS:
        raise ProtocolError("frame expired")
