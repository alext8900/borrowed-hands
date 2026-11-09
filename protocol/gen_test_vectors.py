"""Generate protocol/test-vectors/vectors.json for Borrowed Hands protocol v1 (PROTOCOL 2.3, 3.1, 4.1).

Usage: python3 protocol/gen_test_vectors.py [out_file]     (needs: cryptography)

The vectors are language-neutral: inputs are given as JSON text, outputs as hex / base64url bytes, keys as raw
32-byte seeds. An implementation in any language passes if it reproduces every `canonical_hex`, rejects every
`reject` case, produces every `signature_b64u` (Ed25519 is deterministic, RFC 8032) and verifies it, and computes
every fingerprint and key id. This script also self-checks each vector before writing.
"""
import base64
import hashlib
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

MAX_INT = 2**53 - 1
KEY_RE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_")


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Reject(ValueError):
    pass


def _check(v):
    if isinstance(v, bool) or v is None or isinstance(v, str):
        if isinstance(v, str):
            try:
                v.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise Reject("lone surrogate") from exc
        return
    if isinstance(v, int):
        if not -MAX_INT <= v <= MAX_INT:
            raise Reject("integer out of range")
        return
    if isinstance(v, float):
        raise Reject("float")
    if isinstance(v, list):
        for x in v:
            _check(x)
        return
    if isinstance(v, dict):
        for k, x in v.items():
            if not k or not set(k) <= KEY_RE_CHARS:
                raise Reject("key not [a-z0-9_]+")
            _check(x)
        return
    raise Reject(f"type {type(v).__name__}")


def load_strict(text: str):
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise Reject("duplicate key")
            d[k] = v
        return d

    def no_const(c):
        raise Reject(f"non-finite number {c}")

    try:
        v = json.loads(text, object_pairs_hook=pairs, parse_float=lambda s: (_ for _ in ()).throw(Reject("float")),
                       parse_constant=no_const)
    except Reject:
        raise
    except ValueError as exc:
        raise Reject(f"invalid JSON: {exc}") from exc
    _check(v)
    return v


def canonical(v) -> bytes:
    _check(v)
    try:
        return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise Reject("lone surrogate") from exc


def key(seed: bytes):
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv, pub


def key_id(pub: bytes) -> str:
    return hashlib.sha256(pub).hexdigest()[:16]


def normalize_repo_url(url: str) -> str:
    from urllib.parse import urlsplit
    p = urlsplit(url)
    if p.scheme.lower() != "https" or p.username or p.password or p.port or p.query or p.fragment or not p.hostname:
        raise Reject("not a plain https URL")
    path = p.path
    if path.endswith("/"):
        path = path[:-1]
    if path.endswith(".git"):
        path = path[:-4]
    return f"https://{p.hostname.lower()}{path}"


def fingerprint(url: str) -> str:
    return hashlib.sha256(b"bh1/repo\0" + normalize_repo_url(url).encode()).hexdigest()


SERVER_SEED = bytes(range(0, 32))
DEVICE_SEED = bytes(range(32, 64))

CANON = [
    ("empty_object", "{}"),
    ("key_order", '{"b":1,"a":2,"a_b":3,"a0":4}'),
    ("nested", '{"z":{"y":[3,2,1],"x":{"w":null}},"a":[{"b":true,"a":false}]}'),
    ("integers", '{"zero":0,"neg":-1,"max":9007199254740991,"min":-9007199254740991}'),
    ("unicode_literal", '{"s":"caf\\u00e9 \\u6f22 \\ud83d\\ude00"}'),
    ("escapes", '{"s":"q\\" b\\\\ n\\n t\\t r\\r b\\b f\\f c\\u0001 c\\u001f del\\u007f slash/"}'),
    ("whitespace_input", '{ "a" : [ 1 , 2 ] ,\n "b" : "x" }'),
    ("empty_containers", '{"a":[],"b":{},"c":""}'),
]

REJECT = [
    ("float", '{"a":1.5}'),
    ("exponent", '{"a":1e3}'),
    ("negative_zero_float", '{"a":-0.0}'),
    ("int_too_big", '{"a":9007199254740992}'),
    ("duplicate_key", '{"a":1,"a":2}'),
    ("uppercase_key", '{"A":1}'),
    ("non_ascii_key", '{"\\u00e9":1}'),
    ("dash_key", '{"a-b":1}'),
    ("lone_surrogate", '{"a":"\\ud800"}'),
    ("nan", '{"a":NaN}'),
    ("trailing_garbage", '{"a":1} x'),
]


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "test-vectors" / "vectors.json")
    s_priv, s_pub = key(SERVER_SEED)
    d_priv, d_pub = key(DEVICE_SEED)
    doc = {
        "v": 1,
        "description": "Borrowed Hands protocol v1 test vectors. See PROTOCOL.md 2.3 (canonical JSON), 2.2 and 4.1 "
                       "(signatures), 3.1 (key id, pairing string), 4.1 (repo fingerprint). Generated by "
                       "protocol/gen_test_vectors.py.",
        "keys": {
            "server": {"seed_hex": SERVER_SEED.hex(), "public_hex": s_pub.hex(), "public_b64u": b64u(s_pub),
                       "key_id": key_id(s_pub)},
            "device": {"seed_hex": DEVICE_SEED.hex(), "public_hex": d_pub.hex(), "public_b64u": b64u(d_pub),
                       "key_id": key_id(d_pub)},
        },
        "canonical": [], "reject": [], "signatures": [], "fingerprints": [], "pairing": [],
    }
    for name, text in CANON:
        c = canonical(load_strict(text))
        doc["canonical"].append({"name": name, "input_json": text, "canonical_utf8": c.decode(),
                                 "canonical_hex": c.hex()})
    for name, text in REJECT:
        try:
            load_strict(text)
        except Reject as exc:
            doc["reject"].append({"name": name, "input_json": text, "reason": str(exc)})
        else:
            raise SystemExit(f"reject vector {name} was accepted")

    contract = {"v": 1, "job_id": "3f2b8c1e-9a4d-4e6f-8b2a-1c3d5e7f9a0b", "nonce": b64u(bytes(range(16))),
                "helper_id": "6a1e2b3c-4d5e-4f60-8172-839405a6b7c8", "server_key_id": key_id(s_pub),
                "action": "xcode.build",
                "params": {"workspace_id": "demo", "repo_fingerprint": fingerprint("https://github.com/example/demo.git"),
                           "commit": "538ab8e965ddbbcb2de6eabd39855a410b3f50ec", "scheme": "Demo",
                           "configuration": "Debug", "destination": "platform=macOS,arch=arm64"},
                "issued_at": 1793861063000, "expires_at": 1793861663000,
                "approval_ref": "hermes:0b9e8d7c-6a5b-4c3d-9e2f-1a0b9c8d7e6f"}
    frame = {"type": "job.submit", "id": "11111111-2222-4333-8444-555555555555",
             "sid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "seq": 7, "ts": 1793861063968, "exp": 1793861183968,
             "body": {"contract": "<b64u contract>", "sig": "<b64u sig>"}}
    auth = {"client_nonce": b64u(bytes(range(100, 132))), "endpoint": "wss://bh.example.com/bh/v1/ws",
            "helper_id": contract["helper_id"], "server_key_id": key_id(s_pub),
            "server_nonce": b64u(bytes(range(200, 232)))}
    cases = [("job_contract", b"bh1/job\0", contract, s_priv, s_pub, "server"),
             ("frame_server", b"bh1/frame\0", frame, s_priv, s_pub, "server"),
             ("auth_device", b"bh1/auth-device\0", auth, d_priv, d_pub, "device")]
    for name, ctx, obj, priv, pub, who in cases:
        msg = canonical(obj)
        sig = priv.sign(ctx + msg)
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, ctx + msg)
        tampered = bytearray(msg)
        tampered[-2] ^= 1
        try:
            Ed25519PublicKey.from_public_bytes(pub).verify(sig, ctx + bytes(tampered))
            raise SystemExit("tampered message verified")
        except Exception as exc:  # InvalidSignature
            if "tampered" in str(exc):
                raise
        doc["signatures"].append({
            "name": name, "signer": who, "context_hex": ctx.hex(), "context_text": ctx[:-1].decode() + "\\0",
            "message_json": json.dumps(obj, sort_keys=True), "message_canonical_hex": msg.hex(),
            "signed_bytes_hex": (ctx + msg).hex(), "signature_b64u": b64u(sig),
            "tampered_message_hex": bytes(tampered).hex(), "tampered_must_verify": False})

    for url in ["https://github.com/example/demo.git", "https://GitHub.com/example/Demo.git/",
                "https://github.com/example/demo", "https://gitlab.example.org/group/sub/repo.git"]:
        doc["fingerprints"].append({"url": url, "normalized": normalize_repo_url(url), "fingerprint": fingerprint(url)})
    for url in ["http://github.com/example/demo.git", "https://user@github.com/x/y.git",
                "https://github.com:8443/x/y.git", "https://github.com/x/y.git?a=1"]:
        try:
            normalize_repo_url(url)
            raise SystemExit(f"bad url accepted {url}")
        except Reject as exc:
            doc["fingerprints"].append({"url": url, "reject": str(exc)})

    code = bytes(range(16))
    doc["pairing"].append({
        "endpoint": "wss://bh.example.com/bh/v1/ws", "code_hex": code.hex(), "code_b64u": b64u(code),
        "code_hash_hex": hashlib.sha256(code).hexdigest(), "server_public_b64u": b64u(s_pub),
        "string": f"bh1:wss://bh.example.com/bh/v1/ws#{b64u(code)}.{b64u(s_pub)}"})

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out}: {len(doc['canonical'])} canonical, {len(doc['reject'])} reject, "
          f"{len(doc['signatures'])} signatures, {len(doc['fingerprints'])} fingerprints")


if __name__ == "__main__":
    main()
