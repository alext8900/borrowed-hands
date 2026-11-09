"""Helper config (SPEC 8.3), device-name sanitizing (orchestrator rule for the frozen NAME pattern), grants."""
from __future__ import annotations

import os
import re
import stat

import pytest

from borrowed_hands_helper import config, keyfile
from borrowed_hands_helper.config import ConfigError, Workspace

NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9 ._+-]{0,63}\Z")


@pytest.mark.parametrize("raw,want", [
    ("Alex’s MacBook Pro", "Alexs MacBook Pro"),
    ("Alex's Mac mini", "Alexs Mac mini"),
    ("  Studio Mac  ", "Studio Mac"),
    ("Mac \U0001F680 rocket", "Mac rocket"),
    ("!!!", "Mac"),
    ("\U0001F680\U0001F680", "Mac"),
    ("", "Mac"),
    (".hidden mac", "Mac .hidden mac"),
    ("-dash", "Mac -dash"),
    ("x" * 100, "x" * 64),
    ("Ｍａｃ", "Mac"),          # NFKC folds full-width letters
    ("a/b\\c:d", "a b c d"),
])
def test_sanitize_device_name(raw, want):
    got = config.sanitize_device_name(raw)
    assert got == want
    assert NAME_RE.match(got)


def test_sanitize_always_matches_pattern_for_odd_inputs():
    for raw in ["’’", " ' ", "_ok", "é accents ü", "\x00\x1f ctrl", "a" * 63 + "’" + "b" * 10]:
        assert NAME_RE.match(config.sanitize_device_name(raw)), raw


def test_default_config_grants_nothing():
    cfg = config.Config.empty("Mac")
    assert cfg.server is None and cfg.workspaces == ()
    assert not (cfg.capabilities.system_info or cfg.capabilities.screen_capture or cfg.capabilities.xcode_build)


def _ws(**kw):
    base = dict(id="demo", remote_url="https://github.com/alext8900/bh-demo.git", project="Demo.xcodeproj",
                schemes=("Demo",), configurations=("Debug",), destinations=("platform=macOS,arch=arm64",),
                confirm=False, expires_at=None)
    base.update(kw)
    return Workspace(**base)


def test_roundtrip(tmp_path):
    d = tmp_path / "BH"
    keyfile.ensure_private_dir(d)
    cfg = config.Config.empty("Alexs Mac mini").with_workspace(_ws(confirm=True, expires_at=1893456000000))
    config.save(d, cfg)
    st = os.lstat(d / config.CONFIG_NAME)
    assert stat.S_IMODE(st.st_mode) == 0o600
    back = config.load(d)
    assert back == cfg


def test_parse_rejects_unknown_keys_and_bad_values():
    good = config.dump(config.Config.empty("Mac").with_workspace(_ws()))
    config.parse(good)
    with pytest.raises(ConfigError):
        config.parse(good + "\n[extra]\nx = 1\n")
    with pytest.raises(ConfigError, match="destination"):
        config.parse(good.replace("platform=macOS,arch=arm64", "platform=iOS Simulator,name=iPhone 16"))
    with pytest.raises(ConfigError):
        config.parse(good.replace("https://github.com", "http://github.com"))
    with pytest.raises(ConfigError):
        config.parse(good.replace("build_timeout_s = 900", "build_timeout_s = 99999"))


@pytest.mark.parametrize("kw,match", [
    (dict(remote_url="http://github.com/a/b"), "https"),
    (dict(remote_url="https://user:pw@github.com/a/b"), "https"),
    (dict(remote_url="https://github.com:8443/a/b"), "https"),
    (dict(project="../Evil.xcodeproj"), "project"),
    (dict(project="/abs/Demo.xcodeproj"), "project"),
    (dict(project="Demo.txt"), "project"),
    (dict(schemes=("Demo's",)), "scheme"),
    (dict(schemes=()), "scheme"),
    (dict(configurations=("Debug\n",)), "configuration"),
    (dict(destinations=("generic/platform=iOS",)), "destination"),
    (dict(id="Demo"), "workspace id"),
])
def test_grant_validation_rejects_with_clear_error(kw, match):
    with pytest.raises(ConfigError, match=match):
        config.validate_workspace(_ws(**kw))


def test_scheme_names_are_never_rewritten():
    ws = config.validate_workspace(_ws(schemes=("My App", "Demo_2")))
    assert ws.schemes == ("My App", "Demo_2")


def test_with_workspace_replaces_same_id_and_revoke():
    cfg = config.Config.empty("Mac").with_workspace(_ws()).with_workspace(_ws(schemes=("Other",)))
    assert len(cfg.workspaces) == 1 and cfg.workspace("demo").schemes == ("Other",)
    assert cfg.without_workspace("demo").workspaces == ()


def test_parse_expires():
    now = 1_000_000_000_000
    assert config.parse_expires("7d", now) == now + 7 * 86400_000
    assert config.parse_expires("12h", now) == now + 12 * 3600_000
    assert config.parse_expires("2030-01-01T00:00:00Z", now) == 1893456000000
    with pytest.raises(ConfigError):
        config.parse_expires("soon", now)


def test_workspace_expired():
    ws = _ws(expires_at=1000)
    assert ws.expired(1000) and not ws.expired(999)
    assert not _ws().expired(10**15)
