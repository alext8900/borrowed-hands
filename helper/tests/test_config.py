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


def test_with_workspace_refuses_a_different_grant_with_the_same_id():
    cfg = config.Config.empty("Mac").with_workspace(_ws())
    with pytest.raises(config.GrantExists) as e:
        cfg.with_workspace(_ws(schemes=("Other",)))
    assert e.value.existing == cfg.workspace("demo") and isinstance(e.value, ConfigError)


def test_with_workspace_identical_is_a_no_op():
    cfg = config.Config.empty("Mac").with_workspace(_ws(confirm=True, expires_at=1893456000000))
    assert cfg.with_workspace(_ws(confirm=True, expires_at=1893456000000)) is cfg
    # duplicates collapse in validation, so they are still the same grant
    assert cfg.with_workspace(_ws(schemes=("Demo", "Demo"), confirm=True, expires_at=1893456000000)) is cfg


@pytest.mark.parametrize("change", [
    dict(remote_url="https://github.com/a/other"), dict(project="Other.xcodeproj"), dict(schemes=("Demo",)),
    dict(schemes=("B", "Demo")), dict(configurations=("Debug", "Release")), dict(destinations=config.DESTINATIONS),
    dict(confirm=False), dict(expires_at=None), dict(expires_at=1893456001000),
])
def test_identical_means_every_field_equal(change):
    base = dict(confirm=True, expires_at=1893456000000, schemes=("Demo", "B"))
    cfg = config.Config.empty("Mac").with_workspace(_ws(**base))
    with pytest.raises(config.GrantExists):
        cfg.with_workspace(_ws(**{**base, **change}))


def test_with_workspace_replace_existing_and_revoke():
    cfg = config.Config.empty("Mac").with_workspace(_ws())
    cfg = cfg.with_workspace(_ws(schemes=("Other",)), replace_existing=True)
    assert len(cfg.workspaces) == 1 and cfg.workspace("demo").schemes == ("Other",)
    assert cfg.without_workspace("demo").workspaces == ()


def _changes(old, new):
    return config.grant_changes(config.validate_workspace(old), config.validate_workspace(new), when=str)


def test_grant_changes_flags_every_widening():
    old = _ws(schemes=("Demo",), confirm=True, expires_at=100_000)
    assert _changes(old, old) == []
    assert _changes(old, _ws(remote_url="https://github.com/a/other", schemes=("Demo",), confirm=True,
                             expires_at=100_000)) == [
        ("repo: https://github.com/alext8900/bh-demo.git -> https://github.com/a/other (a different repository)",
         True)]
    assert _changes(old, _ws(project="B.xcodeproj", schemes=("Demo",), confirm=True, expires_at=100_000)) == [
        ("project: Demo.xcodeproj -> B.xcodeproj", True)]
    assert _changes(old, _ws(schemes=("Demo", "B"), configurations=("Debug", "Release"),
                             destinations=config.DESTINATIONS, confirm=True, expires_at=100_000)) == [
        ("schemes added: B", True), ("configurations added: Release", True),
        ("destinations added: generic/platform=iOS Simulator", True)]
    assert _changes(old, _ws(schemes=("Demo",), confirm=False, expires_at=100_000)) == [
        ("confirmation before every build turned off", True)]
    assert _changes(old, _ws(schemes=("Demo",), confirm=True, expires_at=None)) == [
        ("expiry removed (was 100000)", True)]
    assert _changes(old, _ws(schemes=("Demo",), confirm=True, expires_at=200_000)) == [("expiry extended: 100000 -> 200000", True)]


def test_grant_changes_narrowing_is_not_flagged():
    old = _ws(schemes=("Demo", "B"), confirm=False, expires_at=200_000)
    new = _ws(schemes=("Demo",), confirm=True, expires_at=100_000)
    assert _changes(old, new) == [("schemes removed: B", False),
                                  ("confirmation before every build turned on", False),
                                  ("expiry shortened: 200000 -> 100000", False)]
    assert _changes(_ws(), _ws(expires_at=100_000)) == [("expiry added: 100000", False)]
    assert _changes(_ws(schemes=("A", "B")), _ws(schemes=("B", "A"))) == [("scheme order: A, B -> B, A", False)]


def test_expiry_is_whole_seconds_from_validation_on():
    ws = config.validate_workspace(_ws(expires_at=1893456000999))
    assert ws.expires_at == 1893456000000
    cfg = config.Config.empty("Mac").with_workspace(_ws(expires_at=1893456000123))
    assert cfg.with_workspace(_ws(expires_at=1893456000999)) is cfg
    assert config.parse(config.dump(cfg)) == cfg


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
