"""PR readiness: a verdict the plugin computes itself, on request, for one build job.

Ready only if the job is a build that succeeded, its provenance has a branch, and the plugin's own fresh, anonymous
read of the granted repository's refs (refs.py, the reader the friendly build path uses) shows that branch's tip equal
to the commit the signed contract binds, right now. Everything else is not ready with a machine-readable reason, and
any error or ambiguity fails closed. The read happens only when hands_job is called with check_pr_ready=true, only
for the repository URL the plugin remembered for the contract's fingerprint, and only while the job's Mac still
grants that repository. The verdict is never stored.
"""
import json
import time

import pytest

from test_friendly_build import (FP, HID, HID2, MACOS, PUBLIC_IP, REPO, SHA_FIXED, SHA_OTHER, FakeResponse,  # noqa: F401
                                 Net, advertisement, caps, env, grant)

from borrowed_hands import bhproto, jobs, prompt, refs, tools
from borrowed_hands.store import JobState, Store

JID = "3f2b8c1e-9a4d-4e6f-8b2a-1c3d5e7f9a0b"
JID2 = "0b8c1e3f-9a4d-4e6f-8b2a-1c3d5e7f9a0b"
BRANCH = "bh/hermes/fix-title"
REFS_URL = "https://github.com/alext8900/bh-demo.git/info/refs?service=git-upload-pack"
OK_RESULT = {"status": "succeeded", "duration_ms": 1, "xcode": "26.1", "destination": MACOS, "error_count": 0,
             "warning_count": 0, "errors": [], "warnings": [], "log_tail": "", "truncated": False}
FAILED_RESULT = {**OK_RESULT, "status": "failed", "error_count": 1,
                 "errors": [{"file": "a.swift", "line": 1, "message": "x", "type": "error"}]}
NO_NET = AssertionError("no network read for this verdict")


def contract(commit=SHA_FIXED, action="xcode.build", workspace="demo", fingerprint=FP, jid=JID):
    params = {"workspace_id": workspace, "repo_fingerprint": fingerprint, "commit": commit, "scheme": "Demo",
              "configuration": "Debug", "destination": MACOS} if action == "xcode.build" else {}
    return bhproto.canonical({"v": 1, "job_id": jid, "helper_id": HID, "action": action, "params": params})


def add_job(env, *, jid=JID, state=JobState.SUCCEEDED, result=OK_RESULT, error=None, branch=BRANCH,
            commit_source=None, raw=None, action="xcode.build", helper_id=HID):
    source = None
    if commit_source is not None or branch is not None:
        source = {"branch": branch, "commit_source": commit_source or (f"latest on {branch}" if branch else None)}
    env.store.insert_job(job_id=jid, helper_id=helper_id, action=action, contract=raw or contract(jid=jid, action=action),
                         sig="s", contract_hash="c", nonce="n" + jid, session_id="s", now=1,
                         expires_at=9_999_999_999_999, source=source)
    if state.terminal:
        env.store.finish(jid, state, result=result, error=error, now=2)
    elif state is not JobState.PENDING_SEND:
        env.store.update_state(jid, state, now=2)
    return jid


@pytest.fixture
def pr(env):
    """The friendly-build env, with the real JobService behind hands_job (no listener) and refs at BRANCH ->
    SHA_FIXED."""
    env.rt.jobs = jobs.JobService(env.store, None, listener_error="off")
    env.set_net(pairs=[("HEAD", SHA_OTHER), (f"refs/heads/{BRANCH}", SHA_FIXED), ("refs/heads/bh/main", SHA_OTHER)])
    return env


def check(args=None, **over):
    a = {"job_id": JID, "check_pr_ready": True, **(args or {}), **over}
    return json.loads(tools.hands_job(a))


def verdict(**over):
    r = check(**over)
    assert "pr_ready" in r, r
    return r["pr_ready"]


def meta_keys(store):
    return sorted(r[0] for r in store._conn().execute("SELECT k FROM meta"))


# ---------------------------------------------------------------- ready
def test_ready_when_the_branch_tip_is_the_built_commit(pr):
    add_job(pr)
    before = int(time.time() * 1000)
    r = check()
    v = r["pr_ready"]
    assert r["ok"] is True and r["state"] == "succeeded" and r["provenance"]["commit"] == SHA_FIXED
    assert v["ready"] is True and v["reason"] == "ready"
    assert v["commit"] == SHA_FIXED and v["branch"] == BRANCH and v["tip"] == SHA_FIXED
    assert v["open_from"] == BRANCH and v["pr_head_must_equal"] == SHA_FIXED
    assert before <= v["checked_at_ms"] <= int(time.time() * 1000) + 1
    assert v["checked_at"].endswith("Z") and v["checked_at"][:4].isdigit()
    msg = v["message"]
    assert SHA_FIXED in msg and BRANCH in msg and v["checked_at"] in msg
    assert "point-in-time" in msg and "can move" in msg
    assert "head SHA" in msg and "close" in msg and "cannot see pull requests" in msg
    # exactly one anonymous read of the granted repository, through the hardened reader
    assert [(u, h, ip) for u, h, ip, _ in pr.net.urls] == [(REFS_URL, "github.com", PUBLIC_IP)]


def test_ready_with_wait_action_reads_after_the_wait(pr):
    add_job(pr)
    v = verdict(action="wait", wait_seconds=0)
    assert v["ready"] is True and len(pr.net.urls) == 1


def test_explicit_commit_with_branch_is_checked_against_that_branch(pr):
    add_job(pr, commit_source="explicit commit; branch not checked")
    v = verdict()
    assert v["ready"] is True and v["branch"] == BRANCH


# ---------------------------------------------------------------- build_not_succeeded (no network)
@pytest.mark.parametrize("state,result,error", [
    (JobState.FAILED, FAILED_RESULT, None),
    (JobState.CANCELLED, None, {"code": "cancelled", "message": "x"}),
    (JobState.EXPIRED, None, {"code": "expired", "message": "x"}),
    (JobState.FAILED, None, {"code": "refused", "message": "the Mac refused"}),     # a refusal is a failed job
    (JobState.PENDING_SEND, None, None),
    (JobState.SUBMITTED, None, None),
    (JobState.QUEUED, None, None),
    (JobState.BUILDING, None, None),
    # look-alikes: state succeeded but the result or error disagrees
    (JobState.SUCCEEDED, FAILED_RESULT, None),
    (JobState.SUCCEEDED, None, None),
    (JobState.SUCCEEDED, OK_RESULT, {"code": "internal_error", "message": "x"}),
    (JobState.SUCCEEDED, {"status": "succeeded"}, None),                              # not a build result
    # and the other way: a terminal state that is not succeeded, whatever the result says
    (JobState.FAILED, OK_RESULT, None),
    (JobState.EXPIRED, OK_RESULT, None),
    (JobState.CANCELLED, OK_RESULT, None),
])
def test_only_a_succeeded_build_can_be_ready(pr, state, result, error):
    pr.set_net(exc=NO_NET)
    add_job(pr, state=state, result=result, error=error)
    v = verdict()
    assert v["ready"] is False and v["reason"] == "build_not_succeeded", v
    assert v["state"] == state.value and "tip" not in v
    assert pr.net.urls == []


def test_a_stale_running_job_is_not_ready(pr):
    pr.set_net(exc=NO_NET)
    add_job(pr, state=JobState.BUILDING)
    pr.store.mark_nonterminal_stale()
    v = verdict()
    assert v["reason"] == "build_not_succeeded" and pr.net.urls == []


def test_not_a_build(pr):
    pr.set_net(exc=NO_NET)
    add_job(pr, action="system.info", branch=None, result={"os_version": "26.2"})
    v = verdict()
    assert v["ready"] is False and v["reason"] == "not_a_build" and pr.net.urls == []


@pytest.mark.parametrize("raw", [b"{}", b"not json", contract(commit="Z" * 40), contract(commit="main")])
def test_no_signed_commit_is_not_ready(pr, raw):
    pr.set_net(exc=NO_NET)
    add_job(pr, raw=raw)
    v = verdict()
    assert v["ready"] is False and v["reason"] == "no_provenance" and pr.net.urls == []


# ---------------------------------------------------------------- no_branch (no network)
@pytest.mark.parametrize("branch,source", [(None, "latest on bh/main"), (None, "explicit commit"), (None, None)])
def test_no_branch_in_provenance_is_not_ready(pr, branch, source):
    pr.set_net(exc=NO_NET)
    add_job(pr, branch=branch, commit_source=source)
    v = verdict()
    assert v["ready"] is False and v["reason"] == "no_branch" and pr.net.urls == []


def test_tampered_source_row_counts_as_no_branch(pr):
    pr.set_net(exc=NO_NET)
    add_job(pr)
    pr.store._conn().execute("UPDATE meta SET v=? WHERE k=?",
                             (json.dumps({"branch": "main", "commit_source": "latest on main"}), f"job_source:{JID}"))
    assert verdict()["reason"] == "no_branch" and pr.net.urls == []


# ---------------------------------------------------------------- the ref read
def test_branch_moved_reports_the_tip_found(pr):
    add_job(pr)
    pr.set_net(pairs=[(f"refs/heads/{BRANCH}", SHA_OTHER)])
    v = verdict()
    assert v["ready"] is False and v["reason"] == "branch_moved" and v["tip"] == SHA_OTHER
    assert v["commit"] == SHA_FIXED and SHA_OTHER in v["message"] and "Do not open" in v["message"]
    assert "pr_head_must_equal" not in v and "open_from" not in v


@pytest.mark.parametrize("pairs", [[("refs/heads/bh/main", SHA_FIXED)], [],
                                   [(f"refs/tags/{BRANCH}", SHA_FIXED)],
                                   [(f"refs/remotes/origin/{BRANCH}", SHA_FIXED)],
                                   [(f"refs/heads/{BRANCH}/x", SHA_FIXED)]])
def test_branch_missing(pr, pairs):
    add_job(pr)
    pr.set_net(pairs=pairs)
    v = verdict()
    assert v["ready"] is False and v["reason"] == "branch_missing" and "tip" not in v


@pytest.mark.parametrize("net", [
    dict(exc=ConnectionResetError("reset")),
    dict(exc=TimeoutError("slow")),
    dict(response=FakeResponse(b"<html>hi</html>", ctype="text/html")),
    dict(response=FakeResponse(b"", status=404)),
    dict(response=FakeResponse(b"", status=301)),
    dict(response=FakeResponse(b"zzzz garbage")),
    dict(response=FakeResponse(b"0010not a service0000")),
])
def test_reader_failures_fail_closed(pr, net):
    add_job(pr)
    pr.set_net(**net)
    v = verdict()
    assert v["ready"] is False and v["reason"] == "refs_unavailable", v
    assert v["refs_reason"] in ("refs_unreachable", "repo_unreadable") and "tip" not in v


def test_reader_timeout_fails_closed_within_the_deadline(pr, monkeypatch):
    add_job(pr)
    monkeypatch.setattr(refs, "TIMEOUT_S", 0.3)
    pr.set_net(response=FakeResponse(advertisement([(f"refs/heads/{BRANCH}", SHA_FIXED)]), delay=0.02))
    t0 = time.monotonic()
    v = verdict()
    assert time.monotonic() - t0 < 1.0
    assert v["ready"] is False and v["reason"] == "refs_unavailable" and v["refs_reason"] == "refs_unreachable"


def test_a_host_that_resolves_to_a_private_address_is_not_read(pr, monkeypatch):
    add_job(pr)
    monkeypatch.setattr(refs, "_getaddrinfo", lambda host: ["10.0.0.5"])
    v = verdict()
    assert v["reason"] == "refs_unavailable" and v["refs_reason"] == "non_public_address" and pr.net.urls == []


@pytest.mark.parametrize("junk", [None, [], "refs", {f"refs/heads/{BRANCH}": "not-a-sha"},
                                  {f"refs/heads/{BRANCH}": 123}, {f"refs/heads/{BRANCH}": SHA_FIXED.upper()},
                                  {f"refs/heads/{BRANCH}": SHA_FIXED + "\n"}])
def test_reader_returning_junk_fails_closed(pr, monkeypatch, junk):
    add_job(pr)
    monkeypatch.setattr(refs, "fetch_refs", lambda url: junk)
    v = verdict()
    assert v["ready"] is False and v["reason"] == "refs_unavailable" and v["refs_reason"] == "bad_reply"


@pytest.mark.parametrize("exc", [RuntimeError("boom"), ValueError("x"), KeyError("k"), MemoryError()])
def test_reader_raising_anything_fails_closed(pr, monkeypatch, exc):
    add_job(pr)

    def boom(url):
        raise exc
    monkeypatch.setattr(refs, "fetch_refs", boom)
    v = verdict()
    assert v["ready"] is False and v["reason"] == "refs_unavailable"


def test_an_internal_error_fails_closed(pr, monkeypatch):
    add_job(pr)
    pr.set_net(exc=NO_NET)
    monkeypatch.setattr(pr.store, "get_helper", lambda hid: (_ for _ in ()).throw(RuntimeError("db")))
    r = check()
    v = r["pr_ready"]
    assert v["ready"] is False and v["reason"] == "check_failed" and pr.net.urls == []
    assert "open_from" not in v and "pr_head_must_equal" not in v and "Do not open" in v["message"]


# ---------------------------------------------------------------- which repository may be read
def test_repo_url_unknown_is_not_read(pr):
    pr.set_net(exc=NO_NET)
    add_job(pr)
    pr.store._conn().execute("DELETE FROM meta WHERE k LIKE 'repo_url:%'")
    v = verdict()
    assert v["ready"] is False and v["reason"] == "repo_url_unknown" and pr.net.urls == []


def test_a_tampered_remembered_url_for_another_repo_is_not_read(pr):
    pr.set_net(exc=NO_NET)
    add_job(pr)
    pr.store._conn().execute("UPDATE meta SET v=? WHERE k=?", ("https://evil.example/x.git", f"repo_url:{FP}"))
    v = verdict()
    assert v["reason"] == "repo_url_unknown" and pr.net.urls == []


@pytest.mark.parametrize("change", ["other_fingerprint", "other_workspace", "no_grants", "expired", "revoked",
                                    "caps_missing", "unknown_helper", "contract_fingerprint"])
def test_a_repository_the_mac_does_not_grant_is_not_read(pr, change):
    """Same rule as the build path: never read the refs of a repository the job's Mac does not grant."""
    pr.set_net(exc=NO_NET)
    helper, raw = HID, None
    other_fp = bhproto.repo_fingerprint("https://github.com/someone/else.git")
    if change == "other_fingerprint":
        pr.store.set_caps(HID, caps([grant(repo_fingerprint=other_fp)]), now=3)
    elif change == "other_workspace":
        pr.store.set_caps(HID, caps([grant(workspace_id="other")]), now=3)
    elif change == "no_grants":
        pr.store.set_caps(HID, caps([]), now=3)
    elif change == "expired":
        pr.store.set_caps(HID, caps([grant(expires_at=1)]), now=3)
    elif change == "revoked":
        pr.store.revoke_helper(HID, now=3)
    elif change == "caps_missing":
        pr.store._conn().execute("UPDATE helpers SET caps_json=NULL WHERE helper_id=?", (HID,))
    elif change == "unknown_helper":
        helper = HID2
    elif change == "contract_fingerprint":
        # the job names another repository; even a URL remembered for it must not be read via this grant
        pr.store.remember_repo_url(other_fp, "https://github.com/someone/else.git")
        raw = contract(fingerprint=other_fp)
    add_job(pr, helper_id=helper, raw=raw)
    v = verdict()
    assert v["ready"] is False and v["reason"] == "repo_not_granted", v
    assert pr.net.urls == []


def test_the_model_cannot_supply_a_url_or_host():
    props = tools.HANDS_JOB["parameters"]["properties"]
    assert set(props) == {"job_id", "action", "wait_seconds", "check_pr_ready"}
    assert props["check_pr_ready"] == {"type": "boolean", "default": False,
                                       "description": props["check_pr_ready"]["description"]}
    assert tools.HANDS_JOB["parameters"]["additionalProperties"] is False


@pytest.mark.parametrize("extra", [{"repo_url": REPO}, {"url": "https://169.254.169.254/"}, {"branch": BRANCH},
                                   {"commit": SHA_FIXED}])
def test_extra_arguments_are_refused_before_any_read(pr, extra):
    pr.set_net(exc=NO_NET)
    add_job(pr)
    r = check(**extra)
    assert r["ok"] is False and r["error"]["code"] == "invalid_arguments" and "pr_ready" not in r
    assert pr.net.urls == []


# ---------------------------------------------------------------- opt-in only
@pytest.mark.parametrize("args", [{}, {"action": "status"}, {"action": "wait", "wait_seconds": 0},
                                  {"action": "cancel"}, {"check_pr_ready": False},
                                  {"action": "wait", "wait_seconds": 0, "check_pr_ready": False}])
def test_no_network_read_unless_asked(pr, args):
    pr.set_net(exc=NO_NET)
    add_job(pr)
    r = json.loads(tools.hands_job({"job_id": JID, **args}))
    assert r["ok"] is True and "pr_ready" not in r and pr.net.urls == []


def test_cancel_with_the_check_is_refused_without_cancelling(pr):
    pr.set_net(exc=NO_NET)
    add_job(pr, state=JobState.BUILDING)
    r = check(action="cancel")
    assert r["ok"] is False and r["error"]["code"] == "invalid_arguments"
    assert r["error"]["detail"]["reason"] == "check_needs_status_or_wait"
    assert pr.store.get_job(JID).state is JobState.BUILDING and pr.net.urls == []


def test_unknown_job_gets_no_verdict_and_no_read(pr):
    pr.set_net(exc=NO_NET)
    r = check(job_id=JID2)
    assert r["ok"] is False and r["error"]["detail"]["reason"] == "unknown_job" and "pr_ready" not in r
    assert pr.net.urls == []


def test_build_results_never_carry_a_verdict(env):
    from test_friendly_build import run
    env.set_net(pairs=[(f"refs/heads/{BRANCH}", SHA_FIXED)])
    d, r = run({"branch": BRANCH})
    assert r["ok"] and "pr_ready" not in r and len(env.net.urls) == 1       # only the hook's resolution read


# ---------------------------------------------------------------- fresh every time, never stored
def test_computed_fresh_on_every_call(pr):
    add_job(pr)
    keys = meta_keys(pr.store)
    assert verdict()["ready"] is True
    pr.set_net(pairs=[(f"refs/heads/{BRANCH}", SHA_OTHER)])                   # the branch moves
    v = verdict()
    assert v["ready"] is False and v["reason"] == "branch_moved"
    pr.set_net(pairs=[(f"refs/heads/{BRANCH}", SHA_FIXED)])                   # and back
    assert verdict()["ready"] is True and len(pr.net.urls) == 1
    pr.set_net(exc=ConnectionResetError("down"))                             # a later failure is not masked
    assert verdict()["reason"] == "refs_unavailable"
    assert meta_keys(pr.store) == keys, "the verdict must never be stored"


def test_after_a_restart_the_verdict_is_read_again(pr, monkeypatch):
    from borrowed_hands import runtime
    add_job(pr)
    assert verdict()["ready"] is True
    pr.store.close()
    st2 = Store(pr.store.path)                                                # the restarted gateway
    svc2 = jobs.JobService(st2, None, listener_error="off")
    svc2.on_startup()
    monkeypatch.setattr(runtime, "RT", runtime.Runtime(settings=None, store=st2, listener=None, jobs=svc2))
    pr.set_net(pairs=[(f"refs/heads/{BRANCH}", SHA_OTHER)])
    v = verdict()
    assert v["ready"] is False and v["reason"] == "branch_moved" and len(pr.net.urls) == 1
    pr.set_net(exc=TimeoutError("slow"))
    assert verdict()["reason"] == "refs_unavailable"
    st2.close()


# ---------------------------------------------------------------- the words the agent sees
def test_hands_job_description_names_the_check():
    d = tools.HANDS_JOB["description"]
    assert "check_pr_ready" in d and "pr_ready" in d
    p = tools.HANDS_JOB["parameters"]["properties"]["check_pr_ready"]["description"]
    assert "network" in p.lower() or "reads" in p.lower()
    assert "succeeded" in p and "branch" in p


def test_recipe_step5_uses_the_plugin_check_and_the_pr_head():
    t = prompt.TEXT
    assert len(t) <= 4000
    step5 = t[t.index("\n5. "):]
    assert "check_pr_ready" in step5 and "ready" in step5
    assert step5.index("check_pr_ready") < step5.index("open the pull request")
    assert "head SHA" in step5 and "provenance.commit" in step5 and "close" in step5
    assert "git ls-remote" not in t


def test_tool_count_and_hooks_unchanged():
    assert sorted(tools.SPECS) == ["hands_find", "hands_job", "hands_screen_capture", "hands_system_info",
                                   "hands_xcode_build"]


# ---------------------------------------------------------------- review r1 (Review F1): state that changes mid-read
def _mid_read(pr, monkeypatch, change, tip=SHA_FIXED):
    """The real reader path (refs.fetch_refs -> refs._open), with `change()` run while the request is in flight;
    the advertisement then shows the branch at `tip` (the built commit unless told otherwise)."""
    calls = []

    def open_(url, host, ip, timeout, track=None):
        calls.append(url)
        change()
        return FakeResponse(advertisement([(f"refs/heads/{BRANCH}", tip)]))
    monkeypatch.setattr(refs, "_open", open_)
    return calls


def _set_job(pr, state, result):
    pr.store._conn().execute("UPDATE jobs SET state=?, result_json=? WHERE job_id=?",
                             (state, json.dumps(result) if result is not None else None, JID))


OTHER_FP = bhproto.repo_fingerprint("https://github.com/someone/else.git")
MID_READ = {
    # The review's four reproductions
    "mac_revoked": (lambda pr, clock: pr.store.revoke_helper(HID, now=3), "repo_not_granted"),
    "grant_expired": (lambda pr, clock: clock.update(now=20_000), "repo_not_granted"),
    "repo_changed": (lambda pr, clock: pr.store.set_caps(HID, caps([grant(repo_fingerprint=OTHER_FP)]), now=3),
                     "repo_not_granted"),
    "workspace_removed": (lambda pr, clock: pr.store.set_caps(HID, caps([grant(workspace_id="other")]), now=3),
                          "repo_not_granted"),
    # and the rest of the eligibility
    "mac_row_deleted": (lambda pr, clock: pr.store._conn().execute("DELETE FROM helpers WHERE helper_id=?", (HID,)),
                        "repo_not_granted"),
    "caps_cleared": (lambda pr, clock: pr.store._conn().execute("UPDATE helpers SET caps_json=NULL"),
                     "repo_not_granted"),
    "url_forgotten": (lambda pr, clock: pr.store._conn().execute("DELETE FROM meta WHERE k LIKE 'repo_url:%'"),
                      "repo_url_unknown"),
    "job_failed": (lambda pr, clock: _set_job(pr, "failed", FAILED_RESULT), "build_not_succeeded"),
    "job_result_failed": (lambda pr, clock: _set_job(pr, "succeeded", FAILED_RESULT), "build_not_succeeded"),
    "job_cancelled": (lambda pr, clock: _set_job(pr, "cancelled", None), "build_not_succeeded"),
    "job_gone": (lambda pr, clock: pr.store._conn().execute("DELETE FROM jobs WHERE job_id=?", (JID,)),
                 "unknown_job"),
    "branch_metadata_changed": (lambda pr, clock: pr.store._conn().execute(
        "UPDATE meta SET v=? WHERE k=?", (json.dumps({"branch": "bh/other", "commit_source": "latest on bh/other"}),
                                          f"job_source:{JID}")), "check_failed"),
}


@pytest.mark.parametrize("case", sorted(MID_READ))
def test_eligibility_that_changes_during_the_read_is_not_ready(pr, monkeypatch, case):
    """The grant, the remembered URL and the job are validated again, by the same function, after the read
    returns: a change while the request is in flight is not ready even though the tip equals the commit."""
    clock = {"now": 5_000}
    monkeypatch.setattr(jobs, "now_ms", lambda: clock["now"])
    pr.store.set_caps(HID, caps([grant(expires_at=10_000)]), now=1)        # live at 5000, expired at 20000
    add_job(pr)
    change, reason = MID_READ[case]
    calls = _mid_read(pr, monkeypatch, lambda: change(pr, clock))
    v = verdict()
    assert len(calls) == 1, "the read happened (the pre-read check passed)"
    assert v["ready"] is False and v["reason"] == reason, v
    assert "open_from" not in v and "pr_head_must_equal" not in v and "Do not open" in v["message"]


def test_nothing_changing_during_the_read_is_still_ready(pr, monkeypatch):
    clock = {"now": 5_000}
    monkeypatch.setattr(jobs, "now_ms", lambda: clock["now"])
    pr.store.set_caps(HID, caps([grant(expires_at=10_000)]), now=1)
    add_job(pr)
    calls = _mid_read(pr, monkeypatch, lambda: clock.update(now=9_999))
    v = verdict()
    assert len(calls) == 1 and v["ready"] is True and v["reason"] == "ready"
    assert v["checked_at_ms"] <= v["confirmed_at_ms"] and v["confirmed_at"] in v["message"]


def test_codex_scenario_revoked_while_the_branch_moves_back(pr, monkeypatch):
    """Review F1: the branch is at B when the check starts, the Mac is unpaired during the read, and the branch is
    back at A when the reply is read. The grant and the tip never held together, so not ready."""
    add_job(pr)
    pr.set_net(pairs=[(f"refs/heads/{BRANCH}", SHA_OTHER)])
    assert verdict()["reason"] == "branch_moved"                               # B: not ready
    _mid_read(pr, monkeypatch, lambda: pr.store.revoke_helper(HID, now=3), tip=SHA_FIXED)
    v = verdict()
    assert v["ready"] is False and v["reason"] == "repo_not_granted"


def test_pre_and_post_read_checks_are_one_function(pr, monkeypatch):
    seen = []
    real = jobs.JobService._pr_eligible

    def spy(self, job_id, v):
        seen.append(job_id)
        return real(self, job_id, v)
    monkeypatch.setattr(jobs.JobService, "_pr_eligible", spy)
    add_job(pr)
    assert verdict()["ready"] is True and seen == [JID, JID]


@pytest.mark.parametrize("state", [JobState.RECEIVED, JobState.MATERIALIZING])
def test_remaining_running_states_are_not_ready(pr, state):
    pr.set_net(exc=NO_NET)
    add_job(pr, state=state)
    v = verdict()
    assert v["reason"] == "build_not_succeeded" and v["state"] == state.value and pr.net.urls == []


def test_branch_moved_message_names_the_unbuilt_tip(pr):
    add_job(pr)
    pr.set_net(pairs=[(f"refs/heads/{BRANCH}", SHA_OTHER)])
    assert f"{SHA_OTHER} was not built" in verdict()["message"]


# ---------------------------------------------------------------- review r2 (Review F2): the grant itself is compared
# The job built scheme Demo, configuration Debug, destination MACOS (contract()). A grant for the same workspace and
# repository that no longer allows those is not enough, and a different grant swapped in during the read is not
# the grant that was checked before it.
SIM = "generic/platform=iOS Simulator"


@pytest.mark.parametrize("over", [{"schemes": ["Other"]}, {"configurations": ["Release"]}, {"destinations": [SIM]},
                                  {"schemes": []}, {"destinations": []}])
def test_a_grant_that_no_longer_allows_the_build_is_not_ready(pr, over):
    pr.set_net(exc=NO_NET)
    pr.store.set_caps(HID, caps([grant(**over)]), now=3)
    add_job(pr)
    v = verdict()
    assert v["ready"] is False and v["reason"] == "grant_disallows_build" and pr.net.urls == []
    assert "Do not open" in v["message"]


def test_a_wider_grant_that_still_allows_the_build_is_ready(pr):
    pr.store.set_caps(HID, caps([grant(schemes=["Demo", "Other"], configurations=["Debug", "Release"],
                                       destinations=[MACOS, SIM])]), now=3)
    add_job(pr)
    assert verdict()["ready"] is True


SWAPS = {
    # Review F2: a different, schema-valid grant for the same workspace and repository, set through set_caps
    "schemes": {"schemes": ["Demo", "Other"]},
    "configurations": {"configurations": ["Debug", "Release"]},
    "destinations": {"destinations": [MACOS, SIM]},
    "expiry_renewed": {"expires_at": 30_000},
    "expiry_removed": {"expires_at": None},
    "built_scheme_removed": {"schemes": ["Other"]},
}


@pytest.mark.parametrize("case", sorted(SWAPS))
def test_a_grant_replaced_during_the_read_is_not_ready(pr, monkeypatch, case):
    """Even a replacement that still allows the build differs from the grant checked before the read."""
    clock = {"now": 5_000}
    monkeypatch.setattr(jobs, "now_ms", lambda: clock["now"])
    pr.store.set_caps(HID, caps([grant(expires_at=10_000)]), now=1)

    def swap():
        pr.store.set_caps(HID, caps([grant(**{"expires_at": 10_000, **SWAPS[case]})]), now=3)
        if case == "expiry_renewed":
            clock["now"] = 20_000                                   # the original grant has expired by now
    add_job(pr)
    calls = _mid_read(pr, monkeypatch, swap)
    v = verdict()
    assert len(calls) == 1
    want = "grant_disallows_build" if case == "built_scheme_removed" else "check_failed"
    assert v["ready"] is False and v["reason"] == want, v
    assert "open_from" not in v and "pr_head_must_equal" not in v


def test_volatile_caps_and_reordering_during_the_read_stay_ready(pr, monkeypatch):
    """busy, queue depth, the Mac's name and the order of allowed values are not the grant's policy."""
    pr.store.set_caps(HID, caps([grant(schemes=["Demo", "Other"])]), now=1)

    def churn():
        c = caps([grant(schemes=["Other", "Demo"])], name="Renamed Mac")
        c.update(busy=True, queue_depth=3)
        pr.store.set_caps(HID, c, now=3)
    add_job(pr)
    _mid_read(pr, monkeypatch, churn)
    v = verdict()
    assert v["ready"] is True, v


def test_a_second_matching_grant_added_during_the_read_is_not_ready(pr, monkeypatch):
    add_job(pr)
    _mid_read(pr, monkeypatch, lambda: pr.store.set_caps(HID, caps([grant(), grant(schemes=["Other"])]), now=3))
    assert verdict()["reason"] == "check_failed"


@pytest.mark.parametrize("what", ["commit", "url"])
def test_a_changed_commit_or_url_during_the_read_fails_closed(pr, monkeypatch, what):
    """Review round 2 survivors: the commit and the URL are each compared, not only the branch."""
    add_job(pr)

    def change():
        if what == "commit":
            pr.store._conn().execute("UPDATE jobs SET contract=? WHERE job_id=?", (contract(commit=SHA_OTHER), JID))
        else:                                                    # same repository, another spelling of its URL
            assert pr.store.remember_repo_url(FP, "https://github.com/alext8900/bh-demo")
    _mid_read(pr, monkeypatch, change)
    v = verdict()
    assert v["ready"] is False and v["reason"] == "check_failed", v


def test_only_the_xcode_build_capability_grants(pr):
    """A workspace list under any other capability name (schema-invalid, written straight to the store) grants
    nothing."""
    pr.set_net(exc=NO_NET)
    bad = caps([grant()])
    bad["capabilities"][0]["name"] = "screen.capture"
    pr.store._conn().execute("UPDATE helpers SET caps_json=? WHERE helper_id=?", (json.dumps(bad), HID))
    add_job(pr)
    v = verdict()
    assert v["reason"] == "repo_not_granted" and pr.net.urls == []


# ---------------------------------------------------------------- review r3 (Review F3, F4)
def test_a_changed_pairing_time_during_the_read_fails_closed(pr, monkeypatch):
    """Defensive fault injection: the store never rewrites paired_at, but the snapshot includes it."""
    add_job(pr)
    _mid_read(pr, monkeypatch, lambda: pr.store._conn().execute("UPDATE helpers SET paired_at=2 WHERE helper_id=?",
                                                                (HID,)))
    v = verdict()
    assert v["ready"] is False and v["reason"] == "check_failed", v


def test_a_different_helper_identity_on_the_second_pass_fails_closed(pr, monkeypatch):
    """Defensive fault injection: the second lookup returns the same grants under another helper id."""
    import dataclasses
    real = pr.store.get_helper
    calls = []

    def get_helper(hid):
        calls.append(hid)
        h = real(hid)
        return dataclasses.replace(h, helper_id=HID2) if len(calls) > 1 and h is not None else h
    monkeypatch.setattr(pr.store, "get_helper", get_helper)
    add_job(pr)
    v = verdict()
    assert len(calls) == 2 and v["ready"] is False and v["reason"] == "check_failed", v


@pytest.mark.parametrize("start,during,expires,want,reads", [
    (10_000, 10_000, 10_000, "repo_not_granted", 0),     # expiry equal to now at entry: expired, nothing read
    (5_000, 10_000, 10_000, "repo_not_granted", 1),      # reaches exactly its expiry during the read
    (10_000, 10_000, 10_001, "ready", 1),                # one millisecond left: still granted
    (5_000, 9_999, 10_000, "ready", 1),
])
def test_grant_expiry_boundary_is_exclusive(pr, monkeypatch, start, during, expires, want, reads):
    """A grant is live while expires_at > now, matching the build path; expires_at == now is expired."""
    clock = {"now": start}
    monkeypatch.setattr(jobs, "now_ms", lambda: clock["now"])
    pr.store.set_caps(HID, caps([grant(expires_at=expires)]), now=1)
    add_job(pr)
    calls = _mid_read(pr, monkeypatch, lambda: clock.update(now=during))
    v = verdict()
    assert v["reason"] == want and len(calls) == reads, v


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("reason", ["repo_not_granted", "grant_disallows_build", "repo_url_unknown"])
def test_refusal_wording_is_true_before_and_after_the_read(pr, monkeypatch, phase, reason):
    """The same reason can come from the pass before the read or the one after it, so its message must not say
    whether the branches were read."""
    change = {"repo_not_granted": lambda: pr.store.revoke_helper(HID, now=3),
              "grant_disallows_build": lambda: pr.store.set_caps(HID, caps([grant(schemes=["Other"])]), now=3),
              "repo_url_unknown": lambda: pr.store._conn().execute("DELETE FROM meta WHERE k LIKE 'repo_url:%'")}
    add_job(pr)
    if phase == "before":
        change[reason]()
        calls = _mid_read(pr, monkeypatch, lambda: None)
    else:
        calls = _mid_read(pr, monkeypatch, change[reason])
    v = verdict()
    assert v["reason"] == reason and len(calls) == (0 if phase == "before" else 1), v
    msg = v["message"]
    assert "not read" not in msg and "were read" not in msg
    assert "not eligible for a pull request" in msg and "Do not open" in msg
