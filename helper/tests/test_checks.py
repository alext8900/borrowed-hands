"""PR-3 helper side: PROTOCOL 4.2 checks 2 to 12, one test per check, in order, plus "earlier check wins".
PR-4: idempotent resend and replay. SE-3: refused contracts never reach anything that executes."""
from __future__ import annotations

import asyncio
import uuid

import pytest

import helpers as H
from borrowed_hands_helper import checks
from borrowed_hands_helper._shared import bhproto
from borrowed_hands_helper.checks import Rejected
from borrowed_hands_helper.executors import AcceptedJob
from borrowed_hands_helper.jobstore import JobStore


class Env:
    def __init__(self, tmp_path, cfg=None, room=True, confirm_answer=True):
        self.cfg = cfg or H.paired_config()
        self.store = JobStore(tmp_path / "jobs.sqlite3")
        self.room = room
        self.confirm_answer = confirm_answer
        self.confirm_calls = []
        self.enabled = frozenset(a for a in ("system.info", "screen.capture", "xcode.build")
                                 if self.cfg.capabilities.enabled(a))

    async def _confirm(self, summary):
        self.confirm_calls.append(summary)
        return self.confirm_answer

    def run(self, raw, sig, now=H.NOW, retired=None):
        return asyncio.run(checks.verify_contract(
            raw, sig, cfg=self.cfg, keys={H.SERVER_KID: H.SERVER_PUB}, retired_keys=retired or {},
            store=self.store, now_ms=now, enabled=self.enabled, has_room=lambda action: self.room,
            confirm=self._confirm, retry_after_s=lambda: 42))


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.store.close()


def submit(env, c, priv=H.SERVER_PRIV, now=H.NOW):
    raw, sig = H.sign_contract(c, priv)
    return env.run(raw, sig, now=now)


def code(out):
    assert isinstance(out, Rejected), out
    return out.code, out.detail.get("reason") or out.detail.get("field")


def test_happy_path(env):
    c = H.contract()
    out = submit(env, c)
    assert isinstance(out, AcceptedJob)
    assert out.job_id == c["job_id"] and out.workspace.id == "demo" and out.params["commit"] == "a" * 40
    assert env.store.get(c["job_id"]).state == "received"     # the caller marks it queued (step 13)
    assert env.confirm_calls == []                             # confirm is off by default


# ---- step 2: signature
def test_2_wrong_key(env):
    assert code(submit(env, H.contract(), priv=H.OTHER_PRIV)) == ("denied", "bad_signature")


def test_2_tampered_bytes(env):
    raw, sig = H.sign_contract(H.contract())
    raw = raw.replace(b'"Debug"', b'"Debuh"')
    assert code(env.run(raw, sig)) == ("denied", "bad_signature")


def test_2_key_id_must_match_the_verifying_key(env):
    assert code(submit(env, H.contract(server_key_id="00000000000000ff"))) == ("denied", "bad_signature")


def test_2_retired_key_only_for_already_accepted_jobs(env):
    old_kid = bhproto.key_id(bhproto.pub_raw(H.OTHER_PRIV))
    c = H.contract(server_key_id=old_kid)
    raw, sig = H.sign_contract(c, H.OTHER_PRIV)
    out = env.run(raw, sig, retired={old_kid: bhproto.pub_raw(H.OTHER_PRIV)})
    assert code(out) == ("denied", "bad_signature")


def test_2_garbage_sig(env):
    raw, _ = H.sign_contract(H.contract())
    assert code(env.run(raw, "not-a-sig")) == ("denied", "bad_signature")


# ---- step 3: schema
@pytest.mark.parametrize("mut", [
    lambda c: c.update(extra=1),
    lambda c: c["params"].update(url="https://evil.example/x.git"),       # SE-3: a URL in params
    lambda c: c["params"].update(commit="main"),
    lambda c: c.update(v=2),
])
def test_3_schema(env, mut):
    c = H.contract()
    mut(c)
    assert code(submit(env, c))[0] == "invalid_arguments"


def test_3_unparseable_but_signed(env):
    raw = b'{"v":1,"v":1}'
    assert code(env.run(raw, bhproto.sign(H.SERVER_PRIV, bhproto.CTX_JOB, raw)))[0] == "invalid_arguments"


# ---- step 4: helper id
def test_4_helper_id(env):
    assert code(submit(env, H.contract(helper_id=str(uuid.uuid4()))))[0] == "invalid_arguments"


# ---- step 5: time
@pytest.mark.parametrize("kw", [
    dict(issued_at=H.NOW - 700_000, expires_at=H.NOW - 1),                # expired
    dict(issued_at=H.NOW + 60_001, expires_at=H.NOW + 120_000),           # issued in the future
    dict(issued_at=H.NOW - 1000, expires_at=H.NOW - 1000 + 600_001),      # window > 10 min
    dict(issued_at=H.NOW, expires_at=H.NOW - 1),                          # expires before issue
])
def test_5_time(env, kw):
    assert code(submit(env, H.contract(**kw)))[0] == "expired"


def test_5_boundaries_pass(env):
    assert isinstance(submit(env, H.contract(issued_at=H.NOW + 60_000, expires_at=H.NOW + 60_000)), AcceptedJob)
    assert isinstance(submit(env, H.contract(issued_at=H.NOW - 600_000, expires_at=H.NOW)), AcceptedJob)


# ---- step 6: replay (PR-4)
def test_6_idempotent_resend(env):
    c = H.contract()
    raw, sig = H.sign_contract(c)
    assert isinstance(env.run(raw, sig), AcceptedJob)
    again = env.run(raw, sig)
    assert isinstance(again, checks.Duplicate) and again.row.job_id == c["job_id"]


def test_6_same_job_id_different_bytes(env):
    c = H.contract()
    submit(env, c)
    c2 = dict(c, nonce=bhproto.b64u(b"x" * 16))
    assert code(submit(env, c2))[0] == "replay"


def test_6_reused_nonce(env):
    c = H.contract()
    submit(env, c)
    assert code(submit(env, dict(c, job_id=str(uuid.uuid4()))))[0] == "replay"


def test_6_rejected_job_resend_reports_failed_state(env):
    env.cfg = H.paired_config(caps=())
    env.enabled = frozenset()
    c = H.contract()
    raw, sig = H.sign_contract(c)
    assert code(env.run(raw, sig)) == ("helper_unavailable", "capability_disabled")
    again = env.run(raw, sig)
    assert isinstance(again, checks.Duplicate) and again.row.state == "failed"


# ---- step 7: capability
def test_7_capability_disabled(env):
    env.enabled = frozenset({"system.info"})
    assert code(submit(env, H.contract())) == ("helper_unavailable", "capability_disabled")


def test_7_system_info_needs_its_capability(env):
    env.enabled = frozenset({"xcode.build"})
    assert code(submit(env, H.contract(action="system.info"))) == ("helper_unavailable", "capability_disabled")


# ---- step 8: grant
def test_8_no_grant(env):
    out = submit(env, H.contract(params=H.build_params(workspace_id="other")))
    assert out.code == "needs_grant" and "bh grant add xcode.build --workspace other" in out.detail["hint"]


def test_8_grant_expired(env):
    env.cfg = env.cfg.with_workspace(H.workspace(expires_at=H.NOW))
    assert code(submit(env, H.contract()))[0] == "needs_grant"


# ---- step 9: allowlists
@pytest.mark.parametrize("field,value", [
    ("scheme", "Other"), ("configuration", "Release"),
    ("destination", "generic/platform=iOS Simulator"),          # valid destination, not in this grant
    ("destination", "platform=iOS Simulator,name=iPhone 16"),
    ("destination", "platform=macOS,arch=arm64 "),               # exact strings only
])
def test_9_allowlists(env, field, value):
    assert code(submit(env, H.contract(params=H.build_params(**{field: value})))) == ("needs_grant", field)


def test_9_destination_in_grant_but_not_dispatcher_accepted_is_refused(env):
    # a hand-edited config can never widen what the dispatcher accepts: config parsing refuses it, and the
    # check re-asserts it against the fixed list
    import dataclasses
    ws = dataclasses.replace(H.workspace(), destinations=("platform=macOS,arch=arm64", "platform=iOS,name=x"))
    env.cfg = dataclasses.replace(env.cfg, workspaces=(ws,))
    out = submit(env, H.contract(params=H.build_params(destination="platform=iOS,name=x")))
    assert code(out) == ("needs_grant", "destination")


# ---- step 10: fingerprint
def test_10_repo_mismatch(env):
    fp = bhproto.repo_fingerprint("https://github.com/someone/else")
    assert code(submit(env, H.contract(params=H.build_params(repo_fingerprint=fp)))) == \
        ("needs_resolution", "repo_mismatch")


# ---- step 11: device confirm
def test_11_confirm_allow(env):
    env.cfg = env.cfg.with_workspace(H.workspace(confirm=True))
    assert isinstance(submit(env, H.contract()), AcceptedJob)
    assert env.confirm_calls and env.confirm_calls[0]["scheme"] == "Demo"


def test_11_confirm_deny(env):
    env.cfg = env.cfg.with_workspace(H.workspace(confirm=True))
    env.confirm_answer = False
    assert code(submit(env, H.contract()))[0] == "denied"


def test_11_confirm_timeout_is_deny(env):
    env.cfg = env.cfg.with_workspace(H.workspace(confirm=True))
    env.confirm_answer = None
    assert code(submit(env, H.contract())) == ("denied", "device_confirm_timeout")


def test_11_not_for_system_info(env):
    env.cfg = env.cfg.with_workspace(H.workspace(confirm=True))
    assert isinstance(submit(env, H.contract(action="system.info")), AcceptedJob)
    assert env.confirm_calls == []


# ---- step 12: capacity
def test_12_queue_full(env):
    env.room = False
    out = submit(env, H.contract())
    assert code(out) == ("helper_busy", "queue_full") and out.retry_after_s == 42


# ---- order: an earlier check wins
def test_order_signature_before_schema(env):
    c = H.contract(extra=1)
    assert code(submit(env, c, priv=H.OTHER_PRIV)) == ("denied", "bad_signature")


def test_order_schema_before_helper_and_time(env):
    c = H.contract(helper_id=str(uuid.uuid4()), expires_at=H.NOW - 1, issued_at=H.NOW - 2)
    c["params"]["extra"] = 1
    assert code(submit(env, c))[0] == "invalid_arguments"


def test_order_helper_before_time(env):
    c = H.contract(helper_id=str(uuid.uuid4()), issued_at=H.NOW - 700_000, expires_at=H.NOW - 1)
    assert code(submit(env, c))[0] == "invalid_arguments"


def test_order_time_before_replay(env):
    c = H.contract()
    submit(env, c)
    late = dict(c, nonce=bhproto.b64u(b"y" * 16))
    assert code(submit(env, late, now=H.NOW + 400_000))[0] == "expired"


def test_order_replay_before_capability(env):
    c = H.contract()
    submit(env, c)
    env.enabled = frozenset()
    assert code(submit(env, dict(c, nonce=bhproto.b64u(b"z" * 16))))[0] == "replay"


def test_order_capability_before_grant(env):
    env.enabled = frozenset()
    assert code(submit(env, H.contract(params=H.build_params(workspace_id="nope"))))[0] == "helper_unavailable"


def test_order_grant_before_allowlist(env):
    assert code(submit(env, H.contract(params=H.build_params(workspace_id="nope", scheme="X"))))[0] == "needs_grant"
    assert "hint" in submit(env, H.contract(params=H.build_params(workspace_id="nope", scheme="X"))).detail


def test_order_allowlist_before_fingerprint(env):
    fp = bhproto.repo_fingerprint("https://github.com/someone/else")
    out = submit(env, H.contract(params=H.build_params(scheme="X", repo_fingerprint=fp)))
    assert code(out) == ("needs_grant", "scheme")


def test_order_fingerprint_before_confirm(env):
    env.cfg = env.cfg.with_workspace(H.workspace(confirm=True))
    fp = bhproto.repo_fingerprint("https://github.com/someone/else")
    assert code(submit(env, H.contract(params=H.build_params(repo_fingerprint=fp))))[0] == "needs_resolution"
    assert env.confirm_calls == []


def test_order_confirm_before_capacity(env):
    env.cfg = env.cfg.with_workspace(H.workspace(confirm=True))
    env.confirm_answer = False
    env.room = False
    assert code(submit(env, H.contract()))[0] == "denied"


def test_rejections_after_claim_keep_the_row_failed(env):
    c = H.contract(params=H.build_params(scheme="X"))
    submit(env, c)
    r = env.store.get(c["job_id"])
    assert r.state == "failed" and r.error["code"] == "needs_grant"


def test_rejections_before_claim_leave_no_row(env):
    c = H.contract(helper_id=str(uuid.uuid4()))
    submit(env, c)
    assert env.store.get(c["job_id"]) is None


def test_error_bodies_match_schema(env):
    from borrowed_hands_helper import schema
    env.room = False
    for c in (H.contract(params=H.build_params(scheme="X")), H.contract()):
        schema.validate("error", submit(env, c).body())
