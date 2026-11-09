"""SPEC 6 gate and 8.1 settings (pure functions: no Hermes needed)."""
import pytest

from borrowed_hands import config, gate


def getter(d):
    def get(key, default=None):
        node = d
        for seg in key.split("."):
            if not isinstance(node, dict) or seg not in node:
                return default
            node = node[seg]
        return node
    return get


def test_defaults():
    s = config.load(getter({}), {})
    assert s.listen_enabled is False and s.host == "0.0.0.0" and s.port == 8790
    assert s.public_url is None and s.owners == () and s.max_frame_bytes == config.MAX_FRAME


def test_settings_win_and_env_fallbacks():
    s = config.load(getter({"listen": {"enabled": True, "port": 9000}, "public_url": "wss://a.example/bh/v1/ws",
                            "owners": ["telegram:1"]}), {"BH_PORT": "1234", "BH_PUBLIC_URL": "wss://b.example/bh/v1/ws"})
    assert (s.listen_enabled, s.port, s.public_url, s.owners) == (True, 9000, "wss://a.example/bh/v1/ws", ("telegram:1",))
    s = config.load(getter({}), {"BH_LISTEN": "1", "BH_PORT": "1234", "BH_OWNERS": "telegram:1, slack:U2 ,",
                                 "RAILWAY_PUBLIC_DOMAIN": "x.up.railway.app"})
    assert (s.listen_enabled, s.port, s.owners) == (True, 1234, ("telegram:1", "slack:U2"))
    assert s.public_url == "wss://x.up.railway.app/bh/v1/ws"


def test_bh_listen_0_forces_off():
    assert config.load(getter({"listen": {"enabled": True}}), {"BH_LISTEN": "0"}).listen_enabled is False


@pytest.mark.parametrize("bad", [{"listen": {"port": "x"}}, {"listen": {"port": 70000}}, {"listen": {"enabled": "yes"}},
                                 {"public_url": "http://x/bh/v1/ws"}, {"public_url": "wss://x/other"},
                                 {"owners": ["nocolon"]}, {"owners": "telegram:1"}, {"max_frame_bytes": 10**9}])
def test_invalid_settings_degrade_never_raise(bad):
    s = config.load(getter(bad), {})
    assert s.port == 8790 and s.listen_enabled is False and s.public_url is None and s.owners == ()
    assert s.max_frame_bytes == config.MAX_FRAME
    assert s.problems


def test_is_owner():
    s = config.load(getter({"owners": ["telegram:1"]}), {})
    assert s.is_owner("telegram", "1") and not s.is_owner("telegram", "2") and not s.is_owner("slack", "1")
    assert not s.is_owner("telegram", None)


GW = (["/opt/hermes/.venv/bin/hermes", "gateway", "run"], {"gateway.run": object()})


@pytest.mark.parametrize("argv,mods,enabled,want", [
    (GW[0], GW[1], True, True),
    (GW[0], GW[1], False, False),
    (GW[0], {}, True, False),                                     # argv alone is not enough
    (["hermes", "plugins", "validate", "x"], GW[1], True, False),  # module alone is not enough
    (["hermes", "run", "gateway"], GW[1], True, False),
    (["python", "-m", "hermes_cli.main", "gateway", "run", "--quiet"], GW[1], True, True),
])
def test_gate(argv, mods, enabled, want):
    ok, why = gate.should_listen(enabled, argv, mods)
    assert ok is want and isinstance(why, str) and why
