"""Owner readiness replies.

/hands pair captures the owner's original DM route before posting the secret. pair_ok edits the secret into
"waiting for the Mac to advertise capabilities"; when the Mac advertises a usable grant, the owner gets a NEW reply
in that DM. Usable means this plugin can execute it (tools.USABLE_CAPABILITIES), never a stubbed tool.
Routes are memory-only, bounded and expire; stale sessions, revoke and unload silence late work.
"""
import asyncio
import contextvars
import os
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import pytest

import bhproto as bp
from fake_helper import CAPS, FakeHelper
from test_wiring import ctx_factory, src  # noqa: F401  (ctx_factory is a fixture)

from borrowed_hands import commands, config, keys, listener, match, pairing, runtime, tools
from borrowed_hands.store import Store


def caps(workspace="smoke", expiry=None):
    return {"name": "Build Mac", "capabilities": [{"name": "xcode.build", "v": 1, "workspaces": [
        {"workspace_id": workspace, "schemes": ["Demo"], "configurations": ["Debug"],
         "destinations": ["platform=macOS,arch=arm64"], "expires_at": expiry}]}]}


def wire_caps(*capabilities):
    """A schema-valid caps body for a real helper connection."""
    return {**CAPS, "name": "Build Mac", "capabilities": list(capabilities)}


XCODE = {"name": "xcode.build", "v": 1, "xcode": "26.1", "xcode_build": "17B55", "workspaces": [
    {"workspace_id": "smoke", "repo_fingerprint": "0" * 64, "schemes": ["Demo"], "configurations": ["Debug"],
     "destinations": ["platform=macOS,arch=arm64"], "expires_at": None}]}
SCREEN = {"name": "screen.capture", "v": 1, "displays": 1}
SYSINFO = {"name": "system.info", "v": 1}


def now():
    return int(time.time() * 1000)


class Handle:
    ok = True

    def __init__(self, block=False, ok=True):
        self.edits, self.entered, self.release = [], threading.Event(), threading.Event()
        self.block, self.edit_ok = block, ok

    def edit(self, text):
        self.entered.set()
        if self.block:
            assert self.release.wait(3)
        self.edits.append(text)
        return {"ok": self.edit_ok}


class Rig:
    def __init__(self, data_dir):
        self.store = Store(data_dir / "bh.sqlite3")
        self.n = pairing.Notifier(self.store, allowed=tools.USABLE_CAPABILITIES)
        self.posts = []

    def idle(self):
        assert self.n.wait_idle(3)

    def post(self, text):
        self.posts.append(text)
        return Handle()

    def pair(self, helper="h1", code="code", handle=None, post=None):
        handle = handle or Handle()
        self.store.add_helper(helper, b"x" * 32, "Build Mac", now=1, paired_by="telegram:1")
        self.n.remember(code, handle, post or self.post)
        self.n.associate(code, helper, "Paired; waiting for Mac to advertise capabilities.", now())
        self.n.connected(helper, "s1")
        return handle

    def advertise(self, body=None, helper="h1", sid="s1"):
        body = body if body is not None else caps()
        self.store.set_caps(helper, body, now=now())
        self.n.caps(helper, sid, body)


@pytest.fixture
def rig(data_dir):
    r = Rig(data_dir)
    yield r
    r.n.close()


def pair_payload(settings, server_key, store):
    secret, code_hash = pairing.new_code(store, settings, bp.pub_raw(server_key), "telegram:1", now())
    _, code, _ = bp.parse_pairing(secret, allow_insecure_local=True)
    device = bp.new_private_key()
    pub, nonce = bp.b64u(bp.pub_raw(device)), bp.b64u(os.urandom(32))
    signed = bp.canonical({"client_nonce": nonce, "code_hash": code_hash, "device_pubkey": pub,
                           "endpoint": settings.public_url, "server_nonce": "server"})
    return code_hash, {"id": "pair-request", "body": {
        "code": bp.b64u(code), "device_pubkey": pub, "device_name": "Build Mac", "client_nonce": nonce,
        "helper_version": "0.1.0", "os": "macos", "os_version": "26.2", "arch": "arm64",
        "pair_sig": bp.sign(device, bp.CTX_PAIR, signed)}}


SETTINGS = config.Settings(public_url="wss://example.com/bh/v1/ws")


# ---------------------------------------------------------------- the usable set
def test_usable_capabilities_are_what_this_build_can_execute():
    assert tools.USABLE_CAPABILITIES == ("system.info", "xcode.build")
    for cap in match.CAPABILITIES:
        handler = tools.HANDLERS["hands_" + cap.replace(".", "_")]
        assert (cap in tools.USABLE_CAPABILITIES) == (not getattr(handler, "stub", False)), cap
    assert getattr(tools.HANDLERS["hands_screen_capture"], "stub", False)


def test_notifier_without_an_allowed_set_announces_nothing(data_dir):
    store = Store(data_dir / "bh.sqlite3")
    n = pairing.Notifier(store)
    try:
        posts = []
        store.add_helper("h1", b"x" * 32, "Build Mac", now=1, paired_by="telegram:1")
        n.remember("code", Handle(), lambda t: posts.append(t) or Handle())
        n.associate("code", "h1", "Paired", now())
        n.connected("h1", "s1")
        n.caps("h1", "s1", {"capabilities": [SYSINFO]})
        assert n.wait_idle(3) and posts == []
    finally:
        n.close()


def test_screen_capture_advert_is_never_listed_as_usable(rig):
    rig.pair()
    rig.advertise({"name": "Build Mac", "capabilities": [SCREEN]})
    rig.idle()
    assert rig.posts == []
    rig.advertise({"name": "Build Mac", "capabilities": [SCREEN, SYSINFO]})
    rig.idle()
    assert rig.posts == ["Build Mac is ready. System information."]
    assert "Screen" not in rig.posts[0]


def test_register_wires_the_derived_set_end_to_end(ctx_factory):
    c = ctx_factory(listen=True, force_gate=True)
    assert runtime.RT.notifier.allowed == tools.USABLE_CAPABILITIES
    assert c.commands["hands"]("pair", source=src()) is None
    s = c.posted[0].split("bh pair '")[1].split("'")[0]

    async def go():
        h = FakeHelper.from_pairing(s, name="Build Mac")
        assert (await h.pair())["type"] == "pair_ok"
        await h.startup(caps=wire_caps(SCREEN))
        assert runtime.RT.notifier.wait_idle(3)
        await h.send("caps", wire_caps(SCREEN, XCODE))
        assert (await h.recv())["type"] == "ack"
        assert await asyncio.to_thread(runtime.RT.notifier.wait_idle, 3)
        await h.close()
    asyncio.run(go())
    edit = c.handle.edits[0]
    assert edit.startswith("Paired: Build Mac (") and "waiting for Mac to advertise capabilities" in edit
    assert "Pairing alone grants nothing." in edit and s not in edit
    assert c.posted[1:] == ["Build Mac is ready. Xcode builds: smoke / Demo / Debug / macOS arm64."]


# ---------------------------------------------------------------- readiness replies
def test_empty_initial_caps_then_actual_ready_is_new_reply(rig):
    h = rig.pair()
    rig.advertise({"capabilities": []})
    rig.idle()
    assert rig.posts == [] and "waiting" in h.edits[0]
    rig.advertise()
    rig.idle()
    assert rig.posts == ["Build Mac is ready. Xcode builds: smoke / Demo / Debug / macOS arm64."]
    assert len(h.edits) == 1


def test_duplicates_busy_and_reconnect_are_silent(rig):
    rig.pair()
    rig.advertise()
    rig.idle()
    for _ in range(5):
        rig.advertise(caps() | {"busy": True, "queue_depth": 2})
    rig.n.connected("h1", "s2")
    rig.advertise(sid="s2")
    rig.idle()
    assert len(rig.posts) == 1


def test_changes_are_bounded_and_expired_empty_or_incomplete_grants_are_not_ready(rig):
    rig.pair()
    incomplete = caps()
    incomplete["capabilities"][0]["workspaces"][0]["schemes"] = []
    for body in (caps(expiry=1), {"capabilities": []}, incomplete):
        rig.advertise(body)
        rig.idle()
    assert rig.posts == []
    for i in range(12):
        rig.advertise(caps(workspace=f"w{i}"))
        rig.idle()
    assert len(rig.posts) == rig.n.MAX_ANNOUNCEMENTS
    assert max(map(len, rig.posts)) <= rig.n.MAX_TEXT


def test_caps_before_pair_edit_and_removal_while_edit_blocked(rig):
    h = rig.pair(handle=Handle(block=True))
    assert h.entered.wait(2)
    rig.advertise()
    rig.advertise({"capabilities": []})
    h.release.set()
    rig.idle()
    assert rig.posts == []


def test_expiry_rechecked_after_pair_edit(rig):
    h = rig.pair(handle=Handle(block=True))
    assert h.entered.wait(2)
    rig.advertise(caps(expiry=now() + 10_000))
    with patch.object(pairing, "now_ms", return_value=now() + 20_000):
        h.release.set()
        rig.idle()
    assert rig.posts == []


def test_caps_arriving_before_edit_still_ready_after_edit(rig):
    order = []
    h = rig.pair(handle=Handle(block=True), post=lambda t: order.append(t) or Handle())
    assert h.entered.wait(2)
    rig.advertise()
    assert order == []
    h.release.set()
    rig.idle()
    assert h.edits and len(order) == 1


def test_late_caps_from_previous_sid_after_reconnect_is_silent(rig):
    rig.pair()
    rig.idle()
    rig.n.connected("h1", "s2")
    rig.n.caps("h1", "s1", caps())
    rig.idle()
    assert rig.posts == []
    rig.n.caps("h1", "s2", caps())
    rig.idle()
    assert len(rig.posts) == 1


@pytest.mark.parametrize("mode", ["stale", "revoked", "unload", "disconnected"])
def test_stale_connection_revoke_and_unload_cancel_late_work(rig, mode):
    h = rig.pair(handle=Handle(block=True))
    assert h.entered.wait(2)
    rig.advertise()
    if mode == "stale":
        rig.n.connected("h1", "s2")
    if mode == "revoked":
        rig.store.revoke_helper("h1", now=2)
        rig.n.forget("h1")
    if mode == "unload":
        rig.n.close()
    if mode == "disconnected":
        rig.n.disconnected("h1", "s1")
    h.release.set()
    rig.idle()
    assert rig.posts == []


def test_owner_helper_isolation_and_expiry(rig):
    a, b = [], []
    rig.pair(helper="a", post=lambda t: a.append(t) or Handle())
    rig.pair(helper="b", post=lambda t: b.append(t) or Handle())
    rig.advertise(caps("workspace-a"), helper="a")
    rig.advertise(caps("workspace-b"), helper="b")
    rig.idle()
    assert "workspace-a" in a[0] and "workspace-b" not in a[0] and "workspace-b" in b[0]
    with patch.object(pairing, "now_ms", return_value=now() + rig.n.ROUTE_TTL_MS + 1):
        rig.advertise(caps("later"), helper="a")
        rig.idle()
    assert len(a) == 1


def test_failed_send_queues_current_status_hint_once(rig):
    rig.pair(post=Mock(side_effect=RuntimeError("offline")))
    rig.advertise()
    rig.idle()
    rig.advertise()
    rig.idle()
    notices = rig.store.pending_notices()
    assert len(notices) == 1 and "/hands status" in notices[0] and "is ready" not in notices[0]


def test_pending_and_route_records_bounded(rig):
    for i in range(rig.n.MAX_ROUTES + 5):
        rig.pair(helper=str(i), code=str(i))
    rig.idle()
    assert len(rig.n._routes) <= rig.n.MAX_ROUTES and len(rig.n._handles) <= 1
    helper = str(rig.n.MAX_ROUTES + 4)
    rig.advertise(caps() | {"helper_version": "x" * 1_000_000}, helper=helper)
    rig.idle()
    assert "helper_version" not in rig.n._routes[helper].caps


def test_failed_edit_retained_but_late_failure_after_unload_suppressed(rig):
    rig.pair(handle=Handle(ok=False))
    rig.idle()
    assert "Paired" in rig.store.pending_notices()[0]
    h = rig.pair(helper="late", handle=Handle(block=True, ok=False))
    assert h.entered.wait(2)
    rig.n.close()
    h.release.set()
    rig.idle()
    assert len(rig.store.pending_notices()) == 1


def test_unknown_or_unusable_capabilities_are_not_ready(rig):
    rig.pair()
    private = caps()
    private["capabilities"][0]["workspaces"][0]["destinations"] = ["/internal/private/path"]
    for body in ({"capabilities": [SCREEN]}, {"capabilities": [{"name": "unknown"}]}, private):
        rig.advertise(body)
        rig.idle()
    assert rig.posts == []
    rig.advertise({"capabilities": [SYSINFO]})
    rig.idle()
    assert rig.posts == ["Build Mac is ready. System information."]


def test_shadowed_duplicate_capability_and_workspace_do_not_invent_readiness(rig):
    rig.pair()
    body = caps()
    original = body["capabilities"][0]
    body["capabilities"] = [original | {"workspaces": []}, original]
    rig.advertise(body)
    rig.idle()
    assert not rig.posts
    body = caps()
    workspace = body["capabilities"][0]["workspaces"][0]
    body["capabilities"][0]["workspaces"] = [workspace | {"expires_at": 1}, workspace]
    rig.advertise(body)
    rig.idle()
    assert not rig.posts


def test_summary_large_actual_grants_fits_and_does_not_show_paths():
    from borrowed_hands import readiness
    paths = ["/Users/owner/Secret.xcodeproj", "../../etc/passwd", "~/Library/Keychains", "C:\\build\\x",
             "file:///private/var/db", "Demo/Sub"]
    body = caps()
    ws = body["capabilities"][0]["workspaces"][0]
    ws["schemes"] = [f"Scheme{i}" + "x" * 50 for i in range(32)] + paths
    ws["configurations"] = ["Debug"] + paths
    ws["destinations"] = ["platform=macOS,arch=arm64"] + paths
    body["capabilities"][0]["workspaces"] = [ws | {"workspace_id": f"w{i}"} for i in range(32)] + [
        ws | {"workspace_id": path} for path in paths]
    text = readiness.summary("Build Mac", body, 1, ("xcode.build",))
    assert "w0" in text and "more" in text and len(text) <= readiness.MAX_TEXT
    for path in paths:
        assert path not in text, path
    assert "\\" not in text and "~" not in text, text
    assert readiness.summary(paths[0], {"capabilities": [SYSINFO]}, 1, ("system.info",)) == \
        "Your Mac is ready. System information."


def test_summary_escapes_markdown_in_helper_labels():
    from borrowed_hands import readiness
    body = caps("my_app")
    body["capabilities"][0]["workspaces"][0]["schemes"] = ["__Demo__"]
    text = readiness.summary("__Build__ Mac", body, 1, ("xcode.build",))
    assert text == r"\_\_Build\_\_ Mac is ready. Xcode builds: my\_app / \_\_Demo\_\_ / Debug / macOS arm64."
    assert readiness.summary("1. Mac", {"capabilities": [SYSINFO]}, 1, ("system.info",)) == \
        r"1\. Mac is ready. System information."
    markdown = pytest.importorskip("markdown")
    for rendered in (markdown.markdown(text), markdown.markdown(readiness.summary(
            "1. Mac", {"capabilities": [SYSINFO]}, 1, ("system.info",)))):
        assert not any(tag in rendered for tag in ("<em>", "<strong>", "<ol>", "<ul>")), rendered
    assert "__Build__ Mac is ready." in markdown.markdown(text)


# ---------------------------------------------------------------- /hands pair captures the original route
def test_command_captures_original_context_before_posting(rig):
    route = contextvars.ContextVar("test-owner-route", default="unbound")
    route.set("owner-original-dm")
    received = []

    def post(text):
        received.append((route.get(), text))
        return Handle()
    rt = NS(settings=SETTINGS, degraded=None, store=rig.store, key=keys.load_or_create(rig.store.path.parent),
            ctx=NS(post_message=post), listener=None, notifier=rig.n)
    assert commands._pair(rt, NS(platform="telegram", user_id="1", chat_type="dm")) is None
    code = rig.store._conn().execute("SELECT code_hash FROM pair_codes").fetchone()[0]
    rig.store.add_helper("h1", b"x" * 32, "Build Mac", now=1, paired_by="telegram:1")
    rig.n.associate(code, "h1", "Paired", now())
    rig.n.connected("h1", "s1")
    route.set("another-chat")
    rig.advertise()
    rig.idle()
    assert [r for r, _ in received] == ["owner-original-dm", "owner-original-dm"]


def test_real_public_post_reads_captured_session_and_live_route(rig):
    from hermes_cli import plugin_messages as PM
    from gateway import plugin_messages as GM, session_context as SC
    seen = []

    async def post_for(ctx, text, session_key, route):
        seen.append((session_key, route, text))
        return PM.PluginMessage("borrowed-hands", object(), {"ok": True})
    original = object()
    ctx = NS(plugin_id="borrowed-hands")
    ctx.post_message = lambda text: PM.post(ctx, text)
    rt = NS(settings=SETTINGS, degraded=None, store=rig.store, key=keys.load_or_create(rig.store.path.parent),
            ctx=ctx, listener=None, notifier=rig.n)
    with patch.object(PM, "_post_for", side_effect=post_for), \
            patch.object(PM, "_run_on_gateway_loop", side_effect=lambda coro, handle: asyncio.run(coro)):
        try:
            with SC.scoped_session_key("owner-session"), GM.bind_live_source(original):
                commands._pair(rt, NS(platform="telegram", user_id="1", chat_type="dm"))
            code = rig.store._conn().execute("SELECT code_hash FROM pair_codes").fetchone()[0]
            rig.store.add_helper("h1", b"x" * 32, "Build Mac", now=1, paired_by="telegram:1")
            rig.n.associate(code, "h1", "Paired", now())
            rig.n.connected("h1", "s1")
            rig.advertise()
            rig.idle()
        finally:
            SC.reset_session_vars()
    assert [(key, route) for key, route, _ in seen] == [("owner-session", original)] * 2


def test_pair_while_initial_post_returns_attaches_handle_before_ready(rig):
    seen, edited = [], Handle()

    def post(text):
        seen.append(text)
        if len(seen) == 1:
            code = rig.store._conn().execute("SELECT code_hash FROM pair_codes").fetchone()[0]
            rig.store.add_helper("h1", b"x" * 32, "Build Mac", now=1, paired_by="telegram:1")
            rig.n.associate(code, "h1", "Paired; waiting", now())
            rig.n.connected("h1", "s1")
            rig.advertise()
            assert not edited.edits and not rig.n.wait_idle(0.05)
            return edited
        assert edited.edits
        return Handle()
    rt = NS(settings=SETTINGS, degraded=None, store=rig.store, key=keys.load_or_create(rig.store.path.parent),
            ctx=NS(post_message=post), listener=None, notifier=rig.n)
    commands._pair(rt, NS(platform="telegram", user_id="1", chat_type="dm"))
    rig.idle()
    assert len(seen) == 2 and edited.edits


def test_superseded_code_late_handle_cannot_steal_new_route(rig):
    rig.n.remember("old", None, lambda t: pytest.fail("old post"), posting=True)
    new, new_posts = Handle(), []
    rig.store.add_helper("h1", b"x" * 32, "Build Mac", now=1, paired_by="telegram:1")
    rig.n.remember("new", new, lambda t: new_posts.append(t) or Handle())
    rig.n.attach_handle("old", Handle())
    rig.n.associate("old", "old-helper", "Paired old", now())
    rig.n.associate("new", "h1", "Paired new", now())
    rig.n.connected("h1", "s1")
    rig.advertise()
    rig.idle()
    assert new.edits == ["Paired new"] and len(new_posts) == 1


# ---------------------------------------------------------------- listener integration
def test_listener_stale_caps_cannot_replace_current_or_notify(rig):
    async def go():
        rig.pair()
        lst = listener.Listener(config.Settings(), rig.store, bp.new_private_key(), rig.n)
        current, stale = NS(helper_id="h1", sid="s1"), NS(helper_id="h1", sid="old")
        lst._sessions["h1"] = current
        lst._send = AsyncMock()
        await lst._h_caps(current, caps(), {"id": "one"})
        await lst._h_caps(stale, caps("stale-workspace"), {"id": "two"})
        assert rig.store.get_helper("h1").caps == caps()
        lst._send.assert_awaited_once()
    asyncio.run(go())
    rig.idle()
    assert len(rig.posts) == 1 and "stale-workspace" not in rig.posts[0]


def test_listener_revoked_helper_caps_are_ignored(rig):
    async def go():
        rig.pair()
        lst = listener.Listener(config.Settings(), rig.store, bp.new_private_key(), rig.n)
        sess = NS(helper_id="h1", sid="s1")
        lst._sessions["h1"] = sess
        lst._send = AsyncMock()
        lst.revoke("h1", "test")
        await lst._h_caps(sess, caps(), {"id": "one"})
        assert rig.store.get_helper("h1").caps is None
        lst._send.assert_not_awaited()
    asyncio.run(go())
    rig.idle()
    assert rig.posts == [] and "h1" not in rig.n._routes


def test_listener_revoke_after_session_check_cannot_write_or_ack_caps(rig):
    """A thread-safe revoke() completes between _h_caps's checks and its caps write (injected, no sleeps)."""
    async def go():
        rig.pair()
        rig.idle()
        lst = listener.Listener(SETTINGS, rig.store, bp.new_private_key(), rig.n)
        sess = NS(helper_id="h1", sid="s1")
        lst._sessions["h1"] = sess
        lst._send = AsyncMock()
        original = rig.store.set_caps

        def set_caps(helper_id, body, *, now):
            t = threading.Thread(target=lst.revoke, args=(helper_id, "owner"))
            t.start()
            t.join(2)
            assert not t.is_alive() and rig.store.get_helper(helper_id).revoked_at is not None
            return original(helper_id, body, now=now)
        rig.store.set_caps = set_caps
        await lst._h_caps(sess, caps("after-revocation"), {"id": "caps"})
        assert rig.store.get_helper("h1").caps is None
        lst._send.assert_not_awaited()
    asyncio.run(go())
    rig.idle()
    assert rig.posts == []


def test_listener_caps_ack_does_not_wait_for_message_operations(rig):
    async def go():
        h = rig.pair(handle=Handle(block=True))
        assert h.entered.wait(2)
        try:
            lst = listener.Listener(config.Settings(), rig.store, bp.new_private_key(), rig.n)
            sess = NS(helper_id="h1", sid="s1")
            lst._sessions["h1"] = sess
            lst._send = AsyncMock()
            await asyncio.wait_for(lst._h_caps(sess, caps(), {"id": "one"}), 0.25)
            lst._send.assert_awaited_once()
        finally:
            h.release.set()
    asyncio.run(go())
    rig.idle()


def test_listener_associates_after_pair_ack_before_first_caps(rig):
    async def go():
        key = bp.new_private_key()
        lst = listener.Listener(SETTINGS, rig.store, key, rig.n)
        code_hash, payload = pair_payload(SETTINGS, key, rig.store)
        edit = Handle(block=True)
        rig.n.remember(code_hash, edit, rig.post)

        async def handshake(ws, kind, body, re=None):
            assert kind == "pair_ok" and body["helper_id"] not in rig.n._routes
        lst._send_handshake = handshake
        sess = await lst._pair(NS(close=AsyncMock()), payload, "server", "127.0.0.1")
        assert edit.entered.wait(2)
        lst._send = AsyncMock()
        try:
            await lst._h_caps(sess, {"capabilities": []}, {"id": "empty"})
            await lst._h_caps(sess, caps(), {"id": "ready"})
            lst._send.assert_awaited()
            assert not rig.posts
        finally:
            edit.release.set()
        return edit
    edit = asyncio.run(go())
    rig.idle()
    assert len(rig.posts) == 1
    assert "waiting for Mac to advertise capabilities" in edit.edits[0]
    assert "Pairing alone grants nothing." in edit.edits[0]


def test_consumed_code_keeps_owner_route_when_newer_code_is_issued(rig):
    """Helper A consumes code A; while A's pair_ok send is in flight the owner runs /hands pair again (code B)."""
    key = bp.new_private_key()
    rt = NS(settings=SETTINGS, degraded=None, store=rig.store, key=key, listener=None, notifier=rig.n,
            ctx=NS(post_message=lambda text: Handle()))
    code_hash, payload = pair_payload(SETTINGS, key, rig.store)
    original = Handle()
    rig.n.remember(code_hash, original, rig.post)

    async def go():
        lst = listener.Listener(SETTINGS, rig.store, key, rig.n)

        async def handshake(ws, kind, body, re=None):
            assert kind == "pair_ok" and rig.store.get_helper(body["helper_id"]) is not None
            assert commands._pair(rt, NS(platform="telegram", user_id="1", chat_type="dm")) is None
        lst._send_handshake = handshake
        lst._send = AsyncMock()
        sess = await lst._pair(NS(close=AsyncMock()), payload, "server", "127.0.0.1")
        await lst._h_caps(sess, caps(), {"id": "caps"})
        return sess
    sess = asyncio.run(go())
    rig.idle()
    assert sess.helper_id in rig.n._routes
    assert len(original.edits) == 1 and original.edits[0].startswith("Paired: Build Mac (")
    assert rig.posts == ["Build Mac is ready. Xcode builds: smoke / Demo / Debug / macOS arm64."]
    assert rig.store.pending_notices() == []


def test_superseded_unconsumed_code_cannot_pair_or_notify(rig):
    key = bp.new_private_key()
    old_hash, payload = pair_payload(SETTINGS, key, rig.store)
    old = Handle()
    rig.n.remember(old_hash, old, lambda t: pytest.fail("old route"))
    _, new_hash = pairing.new_code(rig.store, SETTINGS, bp.pub_raw(key), "telegram:1", now())
    new = Handle()
    rig.n.remember(new_hash, new, lambda t: pytest.fail("new route"))

    async def go():
        lst = listener.Listener(SETTINGS, rig.store, key, rig.n)
        lst._send_handshake = AsyncMock()
        with pytest.raises(listener._Close):
            await lst._pair(NS(close=AsyncMock()), payload, "server", "127.0.0.1")
        assert lst._send_handshake.call_args.args[1] == "error"
    asyncio.run(go())
    rig.idle()
    assert not rig.n._routes and not old.edits and not new.edits
    assert rig.store.pending_notices() == []


def test_pair_ack_send_failure_never_confirms_or_notifies_owner(rig):
    async def go():
        key = bp.new_private_key()
        lst = listener.Listener(SETTINGS, rig.store, key, rig.n)
        code_hash, payload = pair_payload(SETTINGS, key, rig.store)
        edit = Handle()
        rig.n.remember(code_hash, edit, rig.post)
        lst._send_handshake = AsyncMock(side_effect=ConnectionResetError())
        with pytest.raises(ConnectionResetError):
            await lst._pair(NS(close=AsyncMock()), payload, "server", "127.0.0.1")
        rig.idle()
        assert edit.edits == [] and rig.posts == [] and rig.store.pending_notices() == []
        assert not lst._sessions and not rig.n._routes
    asyncio.run(go())


def test_failed_ready_post_does_not_break_caps_auth_or_job_sync(rig):
    async def go():
        post = Mock(side_effect=RuntimeError("message service unavailable"))
        helper_id = "11111111-1111-4111-8111-111111111111"
        rig.pair(helper=helper_id, post=post)
        device = bp.new_private_key()
        pub = bp.pub_raw(device)
        rig.store.set_device_pubkey(helper_id, pub)
        lst = listener.Listener(SETTINGS, rig.store, bp.new_private_key(), rig.n)
        sess = await lst._register(NS(close=AsyncMock()), helper_id, pub, "s1", "Build Mac")
        lst._send, lst._send_handshake = AsyncMock(), AsyncMock()
        await lst._h_caps(sess, caps(), {"id": "caps-one"})
        rig.idle()
        assert post.call_count == 1
        await lst._h_jobs_sync(sess, {"active": [], "results": []}, {"id": "sync"})
        nonce, server_nonce = bp.b64u(b"n" * 32), bp.b64u(b"s" * 32)
        signed = bp.canonical({"client_nonce": nonce, "endpoint": SETTINGS.public_url, "helper_id": helper_id,
                               "server_key_id": lst.key_id, "server_nonce": server_nonce})
        reauth = await lst._auth(NS(close=AsyncMock()), {"id": "auth", "body": {
            "helper_id": helper_id, "client_nonce": nonce,
            "device_sig": bp.sign(device, bp.CTX_AUTH_DEVICE, signed)}}, server_nonce)
        await lst._h_caps(reauth, caps(), {"id": "caps-two"})
        rig.idle()
        assert post.call_count == 1 and lst._send.await_count == 3
        assert lst._send_handshake.call_args.args[1] == "auth_ok"
    asyncio.run(go())


def test_unload_closes_the_notifier(ctx_factory):
    c = ctx_factory()
    n = runtime.RT.notifier
    for cb in c.unload:
        cb()
    c.unload.clear()
    assert n._closed
