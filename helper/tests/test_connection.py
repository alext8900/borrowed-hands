"""PR-2 / SE-4 helper side: signed frames verified over received bytes; seq, id, sid and exp enforced.
HE-2 (unit part): full-jitter backoff and the close codes that stop the helper."""
from __future__ import annotations

import uuid

import pytest

import helpers as H
from borrowed_hands_helper import connection as C
from borrowed_hands_helper._shared import bhproto

SID = str(uuid.uuid4())
DEV = bhproto.new_private_key()
NOW = 1_800_000_000_000


def frame(seq=1, type_="ack", body=None, sid=SID, ts=NOW, exp=None, fid=None, priv=H.SERVER_PRIV, re=None):
    p = {"type": type_, "id": fid or str(uuid.uuid4()), "sid": sid, "seq": seq, "ts": ts,
         "exp": ts + 120_000 if exp is None else exp, "body": {} if body is None else body}
    if re:
        p["re"] = re
    return bhproto.wrap(p, priv)


def sess():
    return C.Session(SID, DEV, H.SERVER_PUB, clock=lambda: NOW)


def test_good_frames_in_order():
    s = sess()
    assert s.decode(frame(1)).type == "ack"
    f = s.decode(frame(2, "job.cancel", {"job_id": str(uuid.uuid4())}))
    assert f.type == "job.cancel"


def test_flipped_byte_fails():
    text = frame(1)
    env = bhproto.load_strict(text.encode())
    raw = bytearray(bhproto.unb64u(env["p"]))
    raw[raw.index(b'"ack"') + 1] ^= 0x01
    env["p"] = bhproto.b64u(bytes(raw))
    with pytest.raises(bhproto.ProtocolError):
        sess().decode(bhproto.canonical(env).decode())


@pytest.mark.parametrize("make", [
    lambda: frame(1, priv=H.OTHER_PRIV),                      # injected by the edge: wrong key
    lambda: bhproto.wrap({"type": "ack", "id": str(uuid.uuid4()), "ts": NOW, "body": {}}, None),   # unsigned
    lambda: frame(2),                                         # seq gap
    lambda: frame(1, sid=str(uuid.uuid4())),                  # another session
    lambda: frame(1, ts=NOW - 200_000, exp=NOW - 60_001),     # expired beyond the skew
    lambda: frame(1, ts=NOW, exp=NOW + 120_001),              # window > 120 s
])
def test_rejected_frames_close_4400(make):
    with pytest.raises(bhproto.ProtocolError) as e:
        sess().decode(make())
    assert e.value.close_code == 4400


def test_replayed_frame_and_repeated_id():
    s = sess()
    f1 = frame(1)
    s.decode(f1)
    with pytest.raises(bhproto.ProtocolError):
        s.decode(f1)                                          # replay of an old signed frame
    s2 = sess()
    fid = str(uuid.uuid4())
    s2.decode(frame(1, fid=fid))
    with pytest.raises(bhproto.ProtocolError, match="repeated"):
        s2.decode(frame(2, fid=fid))


def test_skew_allowance_passes():
    sess().decode(frame(1, ts=NOW - 179_000, exp=NOW - 59_000))


def test_unknown_type_is_not_a_close():
    with pytest.raises(C.UnknownType):
        sess().decode(frame(1, "shell.exec", {"cmd": "id"}))


def test_helper_bound_type_from_server_is_unknown():
    with pytest.raises(C.UnknownType):
        sess().decode(frame(1, "caps", {}))


def test_bad_body_is_reported_not_closed():
    with pytest.raises(C.BadBody):
        sess().decode(frame(1, "job.cancel", {"job_id": "nope"}))


def test_seq_not_advanced_by_a_rejected_frame():
    s = sess()
    with pytest.raises(bhproto.ProtocolError):
        s.decode(frame(1, priv=H.OTHER_PRIV))
    # the session is closed after a violation in practice; the counter did not move either way
    assert s.decode(frame(1)).type == "ack"


def test_encode_is_signed_and_sequenced():
    s = sess()
    t1, _ = s.encode("caps", {"x": 1})
    t2, _ = s.encode("jobs.sync", {}, re=str(uuid.uuid4()))
    p1, _ = bhproto.unwrap(t1, bhproto.pub_raw(DEV))
    p2, _ = bhproto.unwrap(t2, bhproto.pub_raw(DEV))
    assert (p1["seq"], p2["seq"]) == (1, 2) and p1["sid"] == SID and p1["exp"] - p1["ts"] == 120_000
    bhproto.check_expiry(p2, NOW)


def test_backoff_full_jitter_and_cap():
    t = C.Timing()
    assert C.backoff_delay(0, lambda: 1.0, t) == 1.0
    assert C.backoff_delay(3, lambda: 1.0, t) == 8.0
    assert C.backoff_delay(10, lambda: 1.0, t) == 60.0
    assert C.backoff_delay(1000, lambda: 0.5, t) == 30.0
    assert C.backoff_delay(5, lambda: 0.0, t) == 0.0


@pytest.mark.parametrize("code,stops,exit_code", [
    (1000, False, None), (1001, False, None), (1003, False, None), (1009, False, None), (4400, False, None),
    (4408, False, None), (4429, False, None), (None, False, None),
    (4401, True, 1), (4403, True, 3), (4409, True, 1), (4410, True, 2),
])
def test_close_reactions(code, stops, exit_code):
    r = C.close_reaction(code)
    assert (r is not None) == stops
    if r:
        assert r.exit_code == exit_code
    if code == 4401:
        assert "run `bh pair` again" in r.message
    if code == 4403:
        assert r.unpaired
