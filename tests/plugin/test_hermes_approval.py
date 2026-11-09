"""PL-4 against real Hermes v0.22.7 approval machinery (in process):
the plugin's pre_tool_call asks for approval with the scoped rule_key, so a "session" answer covers a new commit SHA
but re-prompts for a different scheme, configuration, destination, workspace or helper.
"""
import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not os.environ.get("HERMES_SRC"), reason="needs the Hermes venv")
SESSION = "pl4-session"
BUILD = {"helper_id": "6a1e2b3c-4d5e-4f60-8172-839405a6b7c8", "workspace_id": "demo",
         "repo_url": "https://github.com/a/demo.git", "commit": "5" * 40, "scheme": "Demo",
         "configuration": "Debug", "destination": "platform=macOS,arch=arm64"}


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    import hermes_yaml as yaml
    home = tmp_path / "home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(ROOT / "borrowed-hands", home / "plugins" / "borrowed-hands",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["borrowed-hands"], "hook_callback_timeout": 2},
        "approvals": {"mode": "manual", "timeout": 1}}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SESSION_KEY", SESSION)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    for var in ("HERMES_GATEWAY_SESSION", "HERMES_EXEC_ASK", "HERMES_SESSION_PLATFORM", "HERMES_CRON_SESSION",
                "HERMES_SINGLE_QUERY_SESSION", "HERMES_YOLO_MODE", "BH_LISTEN"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import plugins as P
    P._reset_plugin_managers_for_tests()
    P.discover_plugins(force=True)
    mgr = P.get_plugin_manager()
    loaded = mgr._plugins.get("borrowed-hands")
    assert loaded is not None and loaded.enabled, (list(mgr._plugins), getattr(loaded, "error", None))
    from tools import approval
    with approval._lock:
        getattr(approval, "_session_approved", {}).pop(SESSION, None)
    yield mgr
    from tools.terminal_tool import set_approval_callback
    set_approval_callback(None)
    P._reset_plugin_managers_for_tests()


def answer(choice):
    from tools.terminal_tool import set_approval_callback
    seen = []

    def cb(command, description, **kw):
        seen.append(description)
        return choice
    set_approval_callback(cb)
    return seen


def call(args):
    from model_tools import handle_function_call
    return handle_function_call("hands_xcode_build", args, task_id="t", session_id=SESSION, tool_call_id="c",
                                turn_id="u")


def test_session_approval_grain(hermes):
    seen = answer("session")
    r = call(BUILD)
    assert seen and "Build demo (scheme Demo, Debug, macOS arm64) at commit 5555555" in seen[0]
    assert "helper_unavailable" in r          # approved, then the stub handler ran
    seen = answer("deny")
    r = call({**BUILD, "commit": "6" * 40})
    assert seen == [] and "helper_unavailable" in r, "a new SHA must not re-prompt after a session approval"
    for over in [{"scheme": "Other"}, {"configuration": "Release"}, {"destination": "generic/platform=iOS Simulator"},
                 {"workspace_id": "other"}, {"helper_id": "11111111-2222-4333-8444-555555555555"}]:
        seen = answer("deny")
        r = call({**BUILD, **over})
        assert len(seen) == 1, over
        assert "helper_unavailable" not in r, over


def _friendly_setup(hermes, monkeypatch, shas):
    """A paired Mac with one demo grant, the repo URL remembered, and a fake refs read whose bh/fixed tip is the
    next SHA from `shas` on every read. Returns the plugin module."""
    mod = hermes._plugins["borrowed-hands"].module
    rt = mod.runtime.RT
    fp = mod.bhproto.repo_fingerprint(BUILD["repo_url"])
    rt.store.add_helper(BUILD["helper_id"], b"k" * 32, "Macbook Pro M5 Max", now=1, paired_by="telegram:1")
    rt.store.set_caps(BUILD["helper_id"], {
        "helper_version": "0.1.0", "name": "Macbook Pro M5 Max", "os": "macos", "os_version": "26.2",
        "arch": "arm64", "busy": False, "queue_depth": 0,
        "capabilities": [{"name": "xcode.build", "v": 1, "xcode": "26.1", "xcode_build": "17B55", "workspaces": [
            {"workspace_id": "demo", "repo_fingerprint": fp, "schemes": ["Demo"], "configurations": ["Debug"],
             "destinations": ["platform=macOS,arch=arm64"], "expires_at": None}]}]}, now=1)
    rt.store.remember_repo_url(fp, BUILD["repo_url"])
    shas = iter(shas)

    def pkt(s):
        return b"%04x" % (len(s) + 4) + s.encode()

    class Resp:
        status, headers = 200, {"Content-Type": "application/x-git-upload-pack-advertisement"}

        def __init__(self):
            self.body = (pkt("# service=git-upload-pack\n") + b"0000"
                         + pkt(f"{next(shas)} refs/heads/bh/fixed\0caps\n") + b"0000")

        def read1(self, n=-1):
            out, self.body = self.body[:n], self.body[n:]
            return out

        def close(self):
            pass
    monkeypatch.setattr(mod.refs, "_getaddrinfo", lambda host: ["140.82.112.3"])
    monkeypatch.setattr(mod.refs, "_open", lambda url, host, ip, timeout, track=None: Resp())
    return mod


def test_omitted_commit_is_resolved_before_the_real_approval_prompt(hermes, monkeypatch):
    """Friendly build in real Hermes: no commit, the prompt names the resolved SHA and how it was chosen."""
    _friendly_setup(hermes, monkeypatch, ["34497174041223af2f6d51c17c85caa4bb6f61a7"])
    seen = answer("once")
    r = call({"helper_id": BUILD["helper_id"]})
    assert seen and ("Build demo (scheme Demo, Debug, macOS arm64) at commit 3449717 (latest on bh/fixed) "
                     "on Macbook Pro M5 Max") in seen[0]
    assert "helper_unavailable" in r          # approved, then the handler found no listener in this process


def test_real_hermes_gives_the_handler_its_invocation_id(hermes, monkeypatch):
    """The handler fails closed without a tool_call_id. Hermes v0.22.7 does not pass it as a handler kwarg
    (model_tools._execute_tool dispatch_kwargs), but binds it for the dispatch in tools.approval_context; this
    proves an approved call still submits through real Hermes."""
    from types import SimpleNamespace
    mod = _friendly_setup(hermes, monkeypatch, ["c" * 40])
    submitted = []
    monkeypatch.setattr(mod.runtime.RT, "jobs", SimpleNamespace(
        listener_unavailable=lambda: None,
        submit_and_wait=lambda hid, action, params, *a, **k: submitted.append(params) or {
            "ok": True, "state": "succeeded", "job_id": "3f2b8c1e-9a4d-4e6f-8b2a-1c3d5e7f9a0b", "helper_id": hid}))
    answer("once")
    r = call({"helper_id": BUILD["helper_id"]})
    assert '"ok": true' in r and [p["commit"] for p in submitted] == ["c" * 40], r


def test_a_hook_that_outlives_the_hermes_timeout_leaves_no_orphan(hermes, monkeypatch):
    """Review round 3 M1 in real Hermes (this fixture's hook_callback_timeout is 2 s): A's refs read takes 3 s, so
    Hermes blocks A, runs post_tool_call(blocked) and abandons the worker, which then reaches issue(). An identical
    call B afterwards must be approved and built, not refused as a duplicate of A."""
    import time
    from types import SimpleNamespace
    from model_tools import handle_function_call
    mod = _friendly_setup(hermes, monkeypatch, ["d" * 40, "d" * 40])
    fast_open = mod.refs._open

    def slow_open(*a, **k):
        time.sleep(3)
        return fast_open(*a, **k)
    monkeypatch.setattr(mod.refs, "_open", slow_open)
    monkeypatch.setattr(hermes, "_hook_timeout_suppression_seconds", 0.0, raising=False)
    submitted = []
    monkeypatch.setattr(mod.runtime.RT, "jobs", SimpleNamespace(
        listener_unavailable=lambda: None,
        submit_and_wait=lambda hid, action, params, *a, **k: submitted.append(params) or {
            "ok": True, "state": "succeeded", "job_id": "3f2b8c1e-9a4d-4e6f-8b2a-1c3d5e7f9a0b", "helper_id": hid}))
    answer("once")
    args = {"helper_id": BUILD["helper_id"]}
    ra = handle_function_call("hands_xcode_build", dict(args), task_id="t", session_id=SESSION, tool_call_id="A",
                              turn_id="u")
    assert "timed out" in ra and not submitted
    deadline = time.monotonic() + 5
    while any(t.name.startswith("hermes-hook") and t.is_alive() for t in __import__("threading").enumerate()) \
            and time.monotonic() < deadline:
        time.sleep(0.05)
    assert mod.hooks.TICKETS._t == {}, "the abandoned worker must not leave a ticket behind"
    monkeypatch.setattr(mod.refs, "_open", fast_open)
    rb = handle_function_call("hands_xcode_build", dict(args), task_id="t", session_id=SESSION, tool_call_id="B",
                              turn_id="u")
    assert "an identical Borrowed Hands request" not in rb
    assert [p["commit"] for p in submitted] == ["d" * 40], rb


@pytest.mark.parametrize("pause_at", ["before_registration", "key_computation"])
def test_a_hook_timed_out_at_any_point_leaves_no_orphan(hermes, monkeypatch, pause_at):
    """Review round 4 M1: Hermes times the hook out (100 ms budget) while the worker is paused
    either before it has registered at all, or at the argument-key computation; Hermes then runs its normal post
    hook. When the worker resumes it must not issue a ticket, and a later identical call is not refused."""
    import threading
    import time
    from model_tools import handle_function_call
    import hermes_cli.plugins as P
    mod = _friendly_setup(hermes, monkeypatch, ["e" * 40, "e" * 40])
    entered, go = threading.Event(), threading.Event()
    if pause_at == "before_registration":
        original = mod.hooks.TICKETS.begin

        def paused(*a, **k):
            entered.set()
            assert go.wait(5)
            return original(*a, **k)
        monkeypatch.setattr(mod.hooks.TICKETS, "begin", paused)
    else:
        original = mod.hooks._ticket_key

        def paused(*a, **k):
            entered.set()
            assert go.wait(5)
            return original(*a, **k)
        monkeypatch.setattr(mod.hooks, "_ticket_key", paused)
    monkeypatch.setattr(P, "_resolve_hook_callback_timeout", lambda: 0.1)
    args = {"helper_id": BUILD["helper_id"]}
    try:
        ra = handle_function_call("hands_xcode_build", dict(args), task_id="t", session_id="prebegin",
                                  tool_call_id="A")
        assert entered.is_set() and "timed out" in ra
    finally:
        go.set()
    time.sleep(0.5)                                      # the abandoned worker resumes and finishes
    assert mod.hooks.TICKETS._t == {}, "no orphan ticket"
    if pause_at == "before_registration":
        monkeypatch.setattr(mod.hooks.TICKETS, "begin", original)
    else:
        monkeypatch.setattr(mod.hooks, "_ticket_key", original)
    b = mod.hooks.pre_tool_call(tool_name="hands_xcode_build", args=args, session_id="prebegin", tool_call_id="B")
    assert b["action"] == "approve", b


def test_an_idless_identical_call_does_not_remove_a_pending_ticket(hermes):
    """Review round 2 N3 in real Hermes: A holds a live ticket; an identical call without a tool_call_id is
    blocked, and its post hook must leave A's ticket alone."""
    from model_tools import handle_function_call
    plugin_hooks = hermes._plugins["borrowed-hands"].module.hooks
    args = {"helper_id": BUILD["helper_id"]}
    plugin_hooks.TICKETS.issue(plugin_hooks._ticket_key(SESSION, "hands_xcode_build", args), BUILD["helper_id"], "A")
    answer("deny")
    r = handle_function_call("hands_xcode_build", dict(args), task_id="t", session_id=SESSION)
    assert "BLOCKED" in r and "tool_call_id" in r
    assert plugin_hooks.take(SESSION, "hands_xcode_build", args, "A") is not None


def test_overlapping_friendly_builds_never_build_the_unapproved_sha(hermes, monkeypatch):
    """Review round 1 F1 in real Hermes: A ({} args) is shown aaaaaaa and approved once; identical call B, whose
    refs read would give bbbbbbb, arrives while A waits for approval and is denied. A must submit aaaa... or refuse,
    never bbbb...; B never reaches the handler."""
    import threading
    from types import SimpleNamespace
    from model_tools import handle_function_call
    from tools.terminal_tool import set_approval_callback
    mod = _friendly_setup(hermes, monkeypatch, ["a" * 40, "b" * 40])
    submitted = []
    monkeypatch.setattr(mod.runtime.RT, "jobs", SimpleNamespace(
        listener_unavailable=lambda: None,
        submit_and_wait=lambda hid, action, params, *a, **k: submitted.append(params) or {
            "ok": True, "state": "succeeded", "job_id": "3f2b8c1e-9a4d-4e6f-8b2a-1c3d5e7f9a0b", "helper_id": hid}))
    b_hooked, a_done = threading.Event(), threading.Event()

    def pause_b(tool_name=None, tool_call_id="", **kw):   # runs after the plugin's hook for B
        if tool_call_id == "B":
            b_hooked.set()
            a_done.wait(1.5)                              # under the fixture's 2 s hook timeout
        return None
    hermes._hooks["pre_tool_call"].append(pause_b)
    out, prompts = {}, {"A": [], "B": []}

    def run_b():
        set_approval_callback(lambda command, description, **kw: prompts["B"].append(description) or "deny")
        out["B"] = handle_function_call("hands_xcode_build", {"helper_id": BUILD["helper_id"]}, task_id="t",
                                        session_id=SESSION, tool_call_id="B", turn_id="u")
    tb = threading.Thread(target=run_b)

    def approve_a(command, description, **kw):
        prompts["A"].append(description)
        tb.start()
        assert b_hooked.wait(5), "B's hooks never ran"
        return "once"

    def run_a():
        set_approval_callback(approve_a)
        out["A"] = handle_function_call("hands_xcode_build", {"helper_id": BUILD["helper_id"]}, task_id="t",
                                        session_id=SESSION, tool_call_id="A", turn_id="u")
        a_done.set()
    ta = threading.Thread(target=run_a)
    ta.start()
    ta.join(15)
    tb.join(15)
    assert prompts["A"] and "aaaaaaa" in prompts["A"][0]
    assert [p["commit"] for p in submitted] == ["a" * 40], submitted
    assert "b" * 40 not in out["A"]
    assert "BLOCKED" in out["B"] and not any("bbbbbbb" in p for p in prompts["B"])


def test_denied_call_never_reaches_the_handler(hermes):
    answer("deny")
    r = call(BUILD)
    assert "helper_unavailable" not in r and ("BLOCKED" in r or "denied" in r.lower() or "not approved" in r.lower())


def test_session_approval_does_not_leak_across_a_delimiter_collision(hermes):
    """Review round 1 MEDIUM 5, against the real approval store."""
    seen = answer("session")
    assert "helper_unavailable" in call({**BUILD, "scheme": "A:B", "configuration": "C"})
    assert len(seen) == 1
    seen = answer("deny")
    r = call({**BUILD, "scheme": "A", "configuration": "B:C"})
    assert len(seen) == 1 and "helper_unavailable" not in r


def test_denied_call_without_call_id_and_a_modifying_hook_leaves_no_ticket(hermes):
    """Review round 2 MEDIUM 5 in real Hermes: no tool_call_id, another hook modifies the args, user denies."""
    from model_tools import handle_function_call
    plugin_hooks = hermes._plugins["borrowed-hands"].module.hooks
    args = {"helper_id": BUILD["helper_id"], "display": 1}
    hermes._hooks["pre_tool_call"].append(lambda **kw: {"action": "modify", "args": {"display": 2}})
    answer("deny")
    r = handle_function_call("hands_screen_capture", dict(args), session_id="s-no-id")
    assert "BLOCKED" in r
    assert plugin_hooks.consume_ticket("s-no-id", "hands_screen_capture", args) is None


@pytest.mark.parametrize("choice", ["once", "deny"])
def test_idless_calls_are_refused_before_approval_or_handler(hermes, choice):
    from model_tools import handle_function_call
    plugin_hooks = hermes._plugins["borrowed-hands"].module.hooks
    args = {"helper_id": BUILD["helper_id"], "display": 1}
    seen = answer(choice)
    r = handle_function_call("hands_screen_capture", dict(args), session_id="s-idless")
    assert "BLOCKED" in r and "tool_call_id" in r and "helper_unavailable" not in r
    assert not seen
    assert plugin_hooks.consume_ticket("s-idless", "hands_screen_capture", args) is None


def test_denial_with_id_and_modified_args_removes_original_ticket(hermes):
    from model_tools import handle_function_call
    plugin_hooks = hermes._plugins["borrowed-hands"].module.hooks
    args = {"helper_id": BUILD["helper_id"], "display": 1}
    hermes._hooks["pre_tool_call"].append(lambda **kw: {"action": "modify", "args": {"display": 2}})
    seen = answer("deny")
    r = handle_function_call("hands_screen_capture", dict(args), session_id="s-modified", tool_call_id="c-modified")
    assert "BLOCKED" in r and len(seen) == 1
    assert plugin_hooks.consume_ticket("s-modified", "hands_screen_capture", args) is None
