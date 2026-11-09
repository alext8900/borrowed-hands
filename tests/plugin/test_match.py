"""CA-7 matchmaking: strict requirements and signed helper state over localhost."""
import asyncio
import json

import pytest

from fake_helper import CAPS, FakeHelper, now_ms
from test_listener import Server, wait_for
from borrowed_hands import runtime, tools

MACOS = "platform=macOS,arch=arm64"
SIMULATOR = "generic/platform=iOS Simulator"


def build_caps(name="Build Mac", **changes):
    caps = {**CAPS, "name": name, "capabilities": [
        {"name": "system.info", "v": 1},
        {"name": "screen.capture", "v": 1, "displays": 1},
        {"name": "xcode.build", "v": 1, "xcode": "16.0.0", "xcode_build": "16A1",
         "workspaces": [{"workspace_id": "demo", "repo_fingerprint": "a" * 64,
                         "schemes": ["Demo"], "configurations": ["Debug"],
                         "destinations": [MACOS, SIMULATOR], "expires_at": None}]}]}
    caps.update(changes)
    return caps


@pytest.mark.parametrize("reqs,unmet", [
    ([], []),
    (["system.info", "screen.capture", "xcode.build"], []),
    (["os=MaCoS", "arch=ARM64"], []),
    (["os=linux", "arch=x86_64"], ["os=linux", "arch=x86_64"]),
    (["os_version>=26.2", "os_version=26.2.0.0"], []),
    (["os_version>=26.3", "os_version=26"], ["os_version>=26.3", "os_version=26"]),
    (["xcode>=16", "xcode=16.0", "xcode=16.0.0.0"], []),
    (["xcode>=16.0.1", "xcode=15.9"], ["xcode>=16.0.1", "xcode=15.9"]),
    (["xcode>=9.10", "os_version>=9.10"], []),
    ([f"destination={MACOS}", f"destination={SIMULATOR}"], []),
    (["xcode>=16", "os=linux"], ["os=linux"]),
    (["os_version=" + "0" * 49 + "26.2"], []),
])
def test_match_requirement_forms_and_integer_version_comparison(reqs, unmet):
    from borrowed_hands import match
    assert match.evaluate(match.parse(reqs), build_caps()) == unmet


@pytest.mark.parametrize("reqs", [
    None, "xcode>=16", ("xcode.build",), [None], [16], [""],
    ["xcode"], ["unknown.capability"], ["XCODE.BUILD"], ["OS=macos"],
    ["os>=macos"], ["arch>=arm64"], ["os=mac os"], ["arch="],
    ["xcode>16"], ["xcode<=16"], ["xcode==16"], ["xcode>=-1"],
    ["xcode>=16beta"], ["xcode>=16."], ["xcode>=1.2.3.4.5"],
    ["xcode>=١٦"], ["xcode>=16\n"], [" xcode.build"], ["xcode.build "],
    ["os_version>=15 || system.info"], ["destination=platform=iOS"],
    ["destination=generic/platform=iOS Simulator\n"],
    ["xcode.build"] * 9, ["os=" + "a" * 62],
])
def test_match_rejects_invalid_or_unbounded_requirements(reqs):
    from borrowed_hands import match
    with pytest.raises(match.InvalidRequirement) as exc:
        match.parse(reqs)
    assert exc.value.text is not None


def test_match_accepts_eight_requirements_and_64_char_boundary():
    from borrowed_hands import match
    reqs = ["system.info"] * 7 + ["os=" + "a" * 61]
    assert match.evaluate(match.parse(reqs), build_caps()) == [reqs[-1]]


@pytest.mark.parametrize("reqs", [
    ["system.info", "screen.capture", "xcode.build"],
    ["xcode>=16", "xcode=16"],
    [f"destination={SIMULATOR}"],
    ["os=macos", "arch=arm64", "os_version>=15"],
])
def test_match_missing_caps_never_match(reqs):
    from borrowed_hands import match
    assert match.evaluate(match.parse(reqs), {}) == reqs


def test_match_destination_requires_granted_workspace():
    from borrowed_hands import match
    caps = build_caps()
    caps["capabilities"][2]["workspaces"][0]["destinations"] = [MACOS]
    assert match.evaluate(match.parse([f"destination={SIMULATOR}"]), caps) == [f"destination={SIMULATOR}"]
    assert match.evaluate(match.parse([f"destination={MACOS}"]), caps) == []


@pytest.fixture
def match_server(data_dir, monkeypatch):
    server = Server(data_dir).start()
    rt = runtime.Runtime(server.settings, store=server.store, listener=server.lst, jobs=server.jobs)
    monkeypatch.setattr(runtime, "RT", rt)
    yield server
    server.stop()
    server.store.close()


def find(args=None):
    return json.loads(tools.HANDLERS["hands_find"](args or {}))


async def connect(server, caps=None, name="Build Mac", startup=True):
    helper = FakeHelper.from_pairing(server.pair_string(), name=name)
    assert (await helper.pair())["type"] == "pair_ok"
    if startup:
        await helper.startup(caps or build_caps(name))
    return helper


def test_match_nothing_paired_and_revoked_hint(match_server):
    result = find()
    assert result["ok"] is True and result["helpers"] == [] and result["unmatched"] == []
    assert "/hands pair" in result["hint"]

    async def go():
        helper = await connect(match_server)
        try:
            match_server.store.revoke_helper(helper.helper_id, now=now_ms())
            assert "/hands pair" in find()["hint"]
            assert find()["helpers"] == []
        finally:
            await helper.close()
    asyncio.run(go())


def test_match_signed_caps_results_are_narrow_and_create_no_jobs(match_server):
    async def go():
        helper = await connect(match_server)
        try:
            result = find({"requires": ["os=MACOS", "xcode>=16", f"destination={SIMULATOR}"]})
            assert result["ok"] is True and result["unmatched"] == []
            row, = result["helpers"]
            assert row["helper_id"] == helper.helper_id and row["online"] is True
            assert row["status"] == "ready" and row["xcode"] == "16.0.0"
            assert row["capabilities"] == ["system.info", "screen.capture", "xcode.build"]
            assert row["workspaces"] == [{"workspace_id": "demo", "repo_fingerprint": "a" * 64,
                                          "schemes": ["Demo"], "configurations": ["Debug"],
                                          "destinations": [MACOS, SIMULATOR]}]
            assert set(row) == {"helper_id", "name", "online", "busy", "queue_depth", "status", "os",
                                "os_version", "arch", "xcode", "capabilities", "workspaces"}
            assert match_server.store.jobs_nonterminal() == []
            assert "job_created" not in match_server.store.audit_events()
        finally:
            await helper.close()
    asyncio.run(go())


def test_match_latest_signed_caps_replaces_match_with_clear_unmet_reason(match_server):
    async def go():
        helper = await connect(match_server)
        try:
            assert len(find({"requires": ["xcode>=16"]})["helpers"]) == 1
            caps = build_caps()
            caps["capabilities"][2]["xcode"] = "15.4"
            await helper.send("caps", caps)
            assert (await helper.recv())["type"] == "ack"
            result = find({"requires": ["xcode>=16", "arch=x86_64"]})
            assert result["helpers"] == []
            row, = result["unmatched"]
            assert row["helper_id"] == helper.helper_id and row["online"] is True
            assert row["unmet"] == ["xcode>=16", "arch=x86_64"]
        finally:
            await helper.close()
    asyncio.run(go())


def test_match_paired_but_no_caps_hint_and_waiting_for_caps_are_distinct(match_server):
    async def go():
        helper = await connect(match_server, startup=False)
        try:
            row, = find({"requires": ["xcode.build"]})["unmatched"]
            assert row["status"] == "caps_pending" and "grant" not in row.get("hint", "")
            await helper.startup({**CAPS, "name": helper.name, "capabilities": []})
            result = find({"requires": ["xcode.build"]})
            row, = result["unmatched"]
            assert row["status"] == "needs_grant" and row["unmet"] == ["xcode.build"]
            assert "Nothing is enabled on Build Mac" in row["hint"]
            assert "bh caps enable xcode.build" in row["hint"] and "bh grant add" in row["hint"]
            assert "pair" in row["hint"].lower()
            row, = find()["helpers"]
            assert row["status"] == "needs_grant" and "Nothing is enabled" in row["hint"]
        finally:
            await helper.close()
    asyncio.run(go())


def test_match_offline_excluded_unless_requested_and_never_ranked_ready(match_server):
    async def go():
        helper = await connect(match_server)
        await helper.close()
        assert wait_for(lambda: not match_server.lst.is_online(helper.helper_id))
        result = find({"requires": ["xcode>=16"]})
        assert result["helpers"] == [] and result["unmatched"] == []
        assert "offline" in result["hint"].lower() and "/hands pair" not in result["hint"]
        row, = find({"requires": ["xcode>=16"], "include_offline": True})["helpers"]
        assert row["online"] is False and row["status"] == "offline"
    asyncio.run(go())


def test_match_rank_idle_then_busy_then_full_queue_then_offline(match_server):
    async def go():
        helpers = []
        try:
            for name, busy, depth in [("Full", True, 3), ("Busy", True, 1), ("Idle", False, 0),
                                      ("Offline", False, 0), ("Also Idle", False, 0)]:
                helpers.append(await connect(match_server, build_caps(name, busy=busy, queue_depth=depth), name))
            await helpers[3].close()
            assert wait_for(lambda: not match_server.lst.is_online(helpers[3].helper_id))
            rows = find({"requires": ["xcode.build"], "include_offline": True})["helpers"]
            assert [row["name"] for row in rows] == ["Also Idle", "Idle", "Busy", "Full", "Offline"]
            assert [row["status"] for row in rows] == ["ready", "ready", "busy", "helper_busy", "offline"]
            assert rows[3]["reason"] == "queue_full"
            # A build queue cannot block system.info or screen.capture (PROTOCOL 4.3).
            rows = find({"requires": ["system.info"]})["helpers"]
            assert all(row["status"] == "ready" for row in rows)
        finally:
            for helper in helpers:
                await helper.close()
    asyncio.run(go())


def test_match_expired_grants_not_advertised_and_destination_cannot_match(match_server):
    async def go():
        caps = build_caps()
        caps["capabilities"][2]["workspaces"][0]["expires_at"] = now_ms() - 1
        helper = await connect(match_server, caps)
        try:
            result = find({"requires": [f"destination={SIMULATOR}"]})
            row, = result["unmatched"]
            assert row["unmet"] == [f"destination={SIMULATOR}"]
            assert row["status"] == "needs_grant" and "bh grant add" in row["hint"]
            row, = find({"requires": ["xcode>=16"]})["helpers"]
            assert row["workspaces"] == [] and row["status"] == "needs_grant"
            assert match_server.store.get_helper(helper.helper_id).caps == caps
        finally:
            await helper.close()
    asyncio.run(go())


@pytest.mark.parametrize("text", ["not.a.cap", "xcode>=16beta", "xcode>=1.2.3.4.5"])
def test_match_tool_invalid_requirements_identify_offending_string(match_server, text):
    result = find({"requires": [text]})
    assert result["error"]["code"] == "invalid_arguments"
    assert result["error"]["detail"]["requirement"] == text


def test_match_listener_failure_never_exposes_internal_host_details(match_server):
    match_server.lst.stop()
    match_server.lst.error = "Cannot open /Users/private-owner/secret-state/socket"
    result = find()
    assert result["error"]["code"] == "helper_unavailable"
    assert result["error"]["detail"]["reason"] == "listener_not_running"
    assert "/Users/private-owner" not in json.dumps(result)


# Review round 1: regressions.
def test_match_huge_version_in_caps_is_unmet_not_a_crash():
    from borrowed_hands import match
    caps = build_caps(os_version="9" * 5000)
    assert match.evaluate(match.parse(["os_version>=15"]), caps) == ["os_version>=15"]


@pytest.mark.parametrize("first_new", [False, True])
def test_match_duplicate_capability_uses_the_first_entry_for_match_and_row(match_server, first_new):
    from borrowed_hands import match
    caps = build_caps()
    xc = next(c for c in caps["capabilities"] if c["name"] == "xcode.build")
    old = {**xc, "xcode": "15.0"}
    new = {**xc, "xcode": "27.10"}
    caps["capabilities"] = [c for c in caps["capabilities"] if c["name"] != "xcode.build"] + (
        [new, old] if first_new else [old, new])
    unmet = match.evaluate(match.parse(["xcode>=16"]), caps)
    assert (unmet == []) == first_new
    row, _ = tools._find_snapshot(type("H", (), {"caps": caps, "helper_id": "h1", "name": "M"})(), True, True,
                                  now_ms())
    assert row["xcode"] == ("27.10" if first_new else "15.0")


def test_match_oversized_requirement_is_not_echoed(match_server):
    result = find({"requires": ["x" * 200000]})
    assert result["error"]["code"] == "invalid_arguments"
    assert len(json.dumps(result)) < 1000
    assert result["error"]["detail"]["requirement"] == "<200000 chars>"


def test_match_offline_count_hinted_when_online_macs_do_not_match_and_unmatched_not_ready(match_server):
    async def go():
        mini = await connect(match_server, build_caps("Mini"), "Mini")
        await mini.close()
        assert wait_for(lambda: not match_server.lst.is_online(mini.helper_id))
        laptop_caps = build_caps("Laptop", capabilities=[{"name": "system.info", "v": 1}])
        laptop = await connect(match_server, laptop_caps, "Laptop")
        try:
            result = find({"requires": ["xcode>=16"]})
            assert result["helpers"] == []
            row, = result["unmatched"]
            assert row["name"] == "Laptop" and row["status"] != "ready"
            assert "1 paired Mac is offline" in result["hint"] and "include_offline" in result["hint"]
            assert "Mini" not in json.dumps({k: result[k] for k in ("helpers", "unmatched", "hint")})
        finally:
            await laptop.close()
    asyncio.run(go())


@pytest.mark.parametrize("req,have,unmet", [
    ("os_version=1000000000", "1000000000", []),
    ("os_version=" + "1" * 53, "1" * 53, []),
    ("os_version>=15", "9999999999", []),
    ("os_version>=15", "0" * 5000 + "15", []),
    ("os_version>=15", "9" * 5000, ["os_version>=15"]),
])
def test_match_large_but_valid_versions_compare_by_significant_digits(req, have, unmet):
    from borrowed_hands import match
    assert match.evaluate(match.parse([req]), build_caps(os_version=have)) == unmet
