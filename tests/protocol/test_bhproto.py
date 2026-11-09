"""PR-1, PR-2 (frame level), PR-5: bhproto against protocol/test-vectors/vectors.json and PROTOCOL 2, 3.1, 4.1."""
import base64
import hashlib
import json
from pathlib import Path

import pytest

import bhproto as bp

ROOT = Path(__file__).resolve().parents[2]
VEC = json.loads((ROOT / "protocol" / "test-vectors" / "vectors.json").read_text(encoding="utf-8"))


def _priv(who):
    return bp.priv_from_seed(bytes.fromhex(VEC["keys"][who]["seed_hex"]))


# ---------------------------------------------------------------- PR-1 canonical
@pytest.mark.parametrize("case", VEC["canonical"], ids=lambda c: c["name"])
def test_canonical_vector(case):
    obj = bp.load_strict(case["input_json"].encode("utf-8"))
    assert bp.canonical(obj).hex() == case["canonical_hex"]


@pytest.mark.parametrize("case", VEC["reject"], ids=lambda c: c["name"])
def test_reject_vector(case):
    with pytest.raises(bp.ProtocolError):
        bp.canonical(bp.load_strict(case["input_json"].encode("utf-8")))


@pytest.mark.parametrize("bad", [
    {"A": 1}, {"a": 1.0}, {"a": 2**53}, {"a": float("nan")}, {"a": "\ud800"}, {"a": (1, 2)}, {"": 1}, {1: 2},
])
def test_canonical_rejects_objects_outside_subset(bad):
    with pytest.raises(bp.ProtocolError):
        bp.canonical(bad)


@pytest.mark.parametrize("raw", [b"\xff{}", b"\xef\xbb\xbf{}", b"[" * 100000 + b"]" * 100000, b"", b"{\"a\":Infinity}"])
def test_load_strict_rejects_bad_bytes(raw):
    with pytest.raises(bp.ProtocolError):
        bp.load_strict(raw)


def test_load_strict_rejects_str_input():
    with pytest.raises(TypeError):
        bp.load_strict("{}")


# ---------------------------------------------------------------- PR-1 keys, signatures
@pytest.mark.parametrize("who", ["server", "device"])
def test_key_vectors(who):
    k = VEC["keys"][who]
    pub = bp.pub_raw(_priv(who))
    assert pub.hex() == k["public_hex"]
    assert bp.b64u(pub) == k["public_b64u"]
    assert bp.key_id(pub) == k["key_id"]


@pytest.mark.parametrize("case", VEC["signatures"], ids=lambda c: c["name"])
def test_signature_vector(case):
    ctx = bytes.fromhex(case["context_hex"])
    msg = bytes.fromhex(case["message_canonical_hex"])
    assert bp.canonical(bp.load_strict(case["message_json"].encode())) == msg
    assert ctx + msg == bytes.fromhex(case["signed_bytes_hex"])
    sig = bp.sign(_priv(case["signer"]), ctx, msg)
    assert sig == case["signature_b64u"]
    pub = bytes.fromhex(VEC["keys"][case["signer"]]["public_hex"])
    assert bp.verify(pub, ctx, msg, sig) is True
    assert bp.verify(pub, ctx, bytes.fromhex(case["tampered_message_hex"]), sig) is case["tampered_must_verify"]


def test_verify_wrong_context_or_key_or_garbage_is_false():
    case = VEC["signatures"][0]
    ctx = bytes.fromhex(case["context_hex"])
    msg = bytes.fromhex(case["message_canonical_hex"])
    sig = case["signature_b64u"]
    s_pub = bytes.fromhex(VEC["keys"]["server"]["public_hex"])
    d_pub = bytes.fromhex(VEC["keys"]["device"]["public_hex"])
    assert bp.verify(s_pub, bp.CTX_FRAME, msg, sig) is False
    assert bp.verify(d_pub, ctx, msg, sig) is False
    assert bp.verify(s_pub, ctx, msg, "not-a-sig") is False
    assert bp.verify(b"short", ctx, msg, sig) is False
    assert bp.verify(s_pub, ctx, msg, sig + "A") is False


def test_contexts_end_with_nul():
    for c in (bp.CTX_FRAME, bp.CTX_JOB, bp.CTX_AUTH_DEVICE, bp.CTX_AUTH_SERVER, bp.CTX_PAIR, bp.CTX_PAIR_OK,
              bp.CTX_ROTATE_DEVICE, bp.CTX_ROTATE_SERVER):
        assert c.startswith(b"bh1/") and c.endswith(b"\0")


# ---------------------------------------------------------------- b64u
def test_b64u_roundtrip_and_strictness():
    for n in range(0, 40):
        b = bytes(range(n))
        s = bp.b64u(b)
        assert "=" not in s
        assert bp.unb64u(s) == b
    for bad in ["A", "AA=", "a+b/", "AB", "A B", "AAA\n"]:
        # "AB" decodes 'A','B' -> non-canonical trailing bits (B = 000001)
        with pytest.raises(bp.ProtocolError):
            bp.unb64u(bad)


# ---------------------------------------------------------------- PR-1 fingerprints
@pytest.mark.parametrize("case", VEC["fingerprints"], ids=lambda c: c["url"])
def test_fingerprint_vector(case):
    if "reject" in case:
        with pytest.raises(bp.ProtocolError):
            bp.repo_fingerprint(case["url"])
        with pytest.raises(bp.ProtocolError):
            bp.normalize_repo_url(case["url"])
    else:
        assert bp.normalize_repo_url(case["url"]) == case["normalized"]
        assert bp.repo_fingerprint(case["url"]) == case["fingerprint"]


@pytest.mark.parametrize("url", ["https://github.com:443/x/y", "https://github.com:bad/x", "https://github.com/x#f",
                                 "https:///x", "ftp://github.com/x", "https://u:p@github.com/x", 7])
def test_fingerprint_more_rejects(url):
    with pytest.raises(bp.ProtocolError):
        bp.repo_fingerprint(url)


def test_protocol_doc_example_normalization():
    assert bp.normalize_repo_url("https://GitHub.com/alext8900/Demo.git/") == "https://github.com/alext8900/Demo"


# ---------------------------------------------------------------- PR-1 + PR-5 pairing string
def test_pairing_vector():
    p = VEC["pairing"][0]
    pub = bytes.fromhex(VEC["keys"]["server"]["public_hex"])
    code = bytes.fromhex(p["code_hex"])
    assert bp.pairing_string(p["endpoint"], code, pub) == p["string"]
    assert bp.code_hash(code) == p["code_hash_hex"]
    endpoint, code2, pub2 = bp.parse_pairing(p["string"])
    assert (endpoint, code2, pub2) == (p["endpoint"], code, pub)


_P = VEC["pairing"][0]
_TAIL = "#" + _P["code_b64u"] + "." + _P["server_public_b64u"]


@pytest.mark.parametrize("s", [
    "bh2:wss://bh.example.com/bh/v1/ws" + _TAIL,                       # wrong prefix
    "wss://bh.example.com/bh/v1/ws" + _TAIL,                           # no prefix
    "bh1:wss://bh.example.com/bh/v1/ws#x" + _TAIL,                     # '#' in URL
    "bh1:wss://bh.example.com/bh/v1/ws#" + _P["code_b64u"][:-1] + "." + _P["server_public_b64u"],  # short code
    "bh1:wss://bh.example.com/bh/v1/ws#" + _P["code_b64u"] + "." + _P["server_public_b64u"] + "A",  # long key
    "bh1:wss://bh.example.com/bh/v1/ws#" + _P["code_b64u"] + _P["server_public_b64u"],  # no dot
    "bh1:wss://bh.example.com/bh/v1/ws#" + _P["code_b64u"] + ".." + _P["server_public_b64u"],  # two dots
    "bh1:wss://bh.example.com/bh/v1/ws#" + _P["code_b64u"].replace("A", "+", 1) + "." + _P["server_public_b64u"],
    "bh1:ws://bh.example.com/bh/v1/ws" + _TAIL,                        # ws:// to a remote host
    "bh1:ws://127.0.0.1:8790/bh/v1/ws" + _TAIL,                        # ws:// local without the flag
    "bh1:https://bh.example.com/bh/v1/ws" + _TAIL,
    "bh1:wss://bh.example.com/other" + _TAIL,                          # wrong path
    "bh1:" + _TAIL,
    " bh1:wss://bh.example.com/bh/v1/ws" + _TAIL,
])
def test_parse_pairing_rejects(s):
    with pytest.raises(bp.ProtocolError):
        bp.parse_pairing(s)


@pytest.mark.parametrize("ep", ["ws://127.0.0.1:8790/bh/v1/ws", "ws://localhost:8790/bh/v1/ws"])
def test_parse_pairing_insecure_local_only_with_flag(ep):
    s = "bh1:" + ep + _TAIL
    assert bp.parse_pairing(s, allow_insecure_local=True)[0] == ep
    with pytest.raises(bp.ProtocolError):
        bp.parse_pairing(s)


def test_parse_pairing_insecure_flag_still_rejects_remote_ws():
    with pytest.raises(bp.ProtocolError):
        bp.parse_pairing("bh1:ws://127.0.0.1.evil.com/bh/v1/ws" + _TAIL, allow_insecure_local=True)


def test_parse_pairing_with_port():
    s = "bh1:wss://bh.example.com:8443/bh/v1/ws" + _TAIL
    assert bp.parse_pairing(s)[0] == "wss://bh.example.com:8443/bh/v1/ws"


# ---------------------------------------------------------------- PR-2 frame level: wrap/unwrap, expiry
def _payload(**kw):
    p = {"type": "job.submit", "id": "11111111-2222-4333-8444-555555555555",
         "sid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "seq": 7, "ts": 1793861063968, "exp": 1793861183968,
         "body": {}}
    p.update(kw)
    return p


def test_wrap_unwrap_signed_roundtrip():
    s = _priv("server")
    text = bp.wrap(_payload(), s)
    env = json.loads(text)
    assert set(env) == {"v", "p", "s"} and env["v"] == 1
    payload, raw = bp.unwrap(text, bp.pub_raw(s))
    assert payload == _payload()
    assert raw == bp.canonical(_payload())
    assert bp.verify(bp.pub_raw(s), bp.CTX_FRAME, raw, env["s"])


def test_wrap_unwrap_unsigned_roundtrip():
    text = bp.wrap({"type": "hello", "id": "11111111-2222-4333-8444-555555555555", "ts": 1, "body": {}}, None)
    assert set(json.loads(text)) == {"v", "p"}
    payload, _ = bp.unwrap(text, None)
    assert payload["type"] == "hello"


def test_unwrap_verifies_received_bytes_not_reserialized():
    """A non-canonical but validly signed payload verifies; the verifier never re-serializes."""
    s = _priv("server")
    raw = b'{"type":"ack","id":"11111111-2222-4333-8444-555555555555","sid":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee","seq":1,"ts":1,"exp":2,"body":{}}'
    env = json.dumps({"v": 1, "p": bp.b64u(raw), "s": bp.sign(s, bp.CTX_FRAME, raw)})
    payload, got = bp.unwrap(env, bp.pub_raw(s))
    assert got == raw and payload["type"] == "ack"


def test_unwrap_flipped_payload_byte_fails():
    s = _priv("server")
    env = json.loads(bp.wrap(_payload(), s))
    raw = bytearray(bp.unb64u(env["p"]))
    raw[10] ^= 1
    env["p"] = bp.b64u(bytes(raw))
    with pytest.raises(bp.ProtocolError) as ei:
        bp.unwrap(json.dumps(env), bp.pub_raw(s))
    assert ei.value.close_code == 4400


def test_unwrap_wrong_key_fails():
    env = bp.wrap(_payload(), _priv("server"))
    with pytest.raises(bp.ProtocolError):
        bp.unwrap(env, bp.pub_raw(_priv("device")))


@pytest.mark.parametrize("env", [
    '{"v":1,"p":"e30"}',                      # missing signature when one is required
    '{"v":2,"p":"e30","s":"' + "A" * 86 + '"}',
    '{"v":1,"p":"e30","s":"' + "A" * 86 + '","x":1}',
    'not json', '[]', '{"v":1}',
])
def test_unwrap_rejects_bad_signed_envelopes(env):
    with pytest.raises(bp.ProtocolError):
        bp.unwrap(env, bp.pub_raw(_priv("server")))


def test_unwrap_unsigned_rejects_signature_field():
    env = bp.wrap(_payload(), _priv("server"))
    with pytest.raises(bp.ProtocolError):
        bp.unwrap(env, None)


def test_unwrap_rejects_non_object_payload_and_floats():
    s = _priv("server")
    for raw in (b"[1]", b'{"a":1.5}', b'{"a":1,"a":2}'):
        env = json.dumps({"v": 1, "p": bp.b64u(raw), "s": bp.sign(s, bp.CTX_FRAME, raw)})
        with pytest.raises(bp.ProtocolError):
            bp.unwrap(env, bp.pub_raw(s))


def test_unwrap_rejects_bytes_text_too_large():
    with pytest.raises(bp.ProtocolError) as ei:
        bp.unwrap("x" * (bp.MAX_FRAME + 1), None)
    assert ei.value.close_code == 1009


NOW = 1_800_000_000_000


@pytest.mark.parametrize("ts,exp,ok", [
    (NOW, NOW + 120_000, True),                # max window
    (NOW, NOW + 120_001, False),               # window too large
    (NOW, NOW, False),                         # ts < exp is required
    (NOW, NOW - 1, False),
    (NOW - 180_000, NOW - 60_000, True),       # expired, but inside the 60 s skew
    (NOW - 180_001, NOW - 60_001, False),      # beyond the skew
])
def test_check_expiry(ts, exp, ok):
    p = {"ts": ts, "exp": exp}
    if ok:
        bp.check_expiry(p, NOW)
    else:
        with pytest.raises(bp.ProtocolError) as ei:
            bp.check_expiry(p, NOW)
        assert ei.value.close_code == 4400


@pytest.mark.parametrize("p", [{}, {"ts": 1}, {"exp": 1}, {"ts": "1", "exp": 2}, {"ts": True, "exp": 2}])
def test_check_expiry_requires_integer_fields(p):
    with pytest.raises(bp.ProtocolError):
        bp.check_expiry(p, NOW)


# ---------------------------------------------------------------- copy is byte-identical (SPEC 2)
def test_plugin_copy_is_byte_identical():
    a = (ROOT / "protocol" / "bhproto.py").read_bytes()
    b = (ROOT / "borrowed-hands" / "bhproto.py").read_bytes()
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()


@pytest.mark.parametrize("v", ["true", "1.0", "\"1\"", "2", "0"])
def test_unwrap_requires_integer_version_1(v):
    raw = bp.canonical({"type": "hello", "id": "11111111-2222-4333-8444-555555555555", "ts": 1, "body": {}})
    with pytest.raises(bp.ProtocolError):
        bp.unwrap('{"v":%s,"p":"%s"}' % (v, bp.b64u(raw)), None)
