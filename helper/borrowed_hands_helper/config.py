"""Helper config: `~/Library/Application Support/BorrowedHands/config.toml` (SPEC 8.3).

The grants in this file are the authority for what may run on this Mac. Parsing is strict (unknown keys,
bad values and destinations the dispatcher does not accept are errors) so everything downstream can trust
a `Config`. Pairing grants nothing: `Config.empty` has every capability off and no workspaces.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import stat
import subprocess
import threading
import time
import tomllib
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import keyfile
from ._shared import bhproto

CONFIG_NAME = "config.toml"
LOCK_NAME = "config.lock"      # flock target; never config.toml itself, which is replaced by rename
LOCK_TIMEOUT_S = 10.0
DEFAULT_DIR = Path.home() / "Library" / "Application Support" / "BorrowedHands"

# The frozen NAME pattern (pair.json device_name, caps.json name, schemes, configurations).
NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9 ._+-]{0,63}\Z")
WORKSPACE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
PROJECT_RE = re.compile(r"[^\x00-\x1f]{1,255}\Z")
# The only two destinations the dispatcher accepts (PROTOCOL 4.2 step 9). Exact strings.
DESTINATIONS = ("platform=macOS,arch=arm64", "generic/platform=iOS Simulator")
MAX_TIMEOUT_S = 3600          # bh-runner MAX_TIMEOUT
MAX_DISK_MB = 100 * 1024      # bh-runner MAX_DISK_BUDGET_MB
MAX_MEM_MB = 64 * 1024        # bh-runner MAX_MEM_BUDGET_MB


class ConfigError(ValueError):
    pass


class GrantExists(ConfigError):
    """A different grant already uses this workspace id. Never silently broaden a grant: the caller shows
    `existing` and the owner picks another name, revokes it first, or replaces it explicitly."""

    def __init__(self, existing: "Workspace"):
        super().__init__(f"a grant named {existing.id!r} already exists")
        self.existing = existing


def sanitize_device_name(raw: str) -> str:
    """Fit a macOS computer name to the frozen NAME pattern: NFKC; drop ' and U+2019; every other
    disallowed character becomes a space; collapse spaces; trim; prefix "Mac " if the first character is
    not [A-Za-z0-9_]; cut to 64; "Mac" if nothing is left."""
    s = unicodedata.normalize("NFKC", raw or "")
    s = s.replace("'", "").replace("’", "")
    s = "".join(c if (c.isascii() and (c.isalnum() or c in " ._+-")) else " " for c in s)
    s = " ".join(s.split())
    if not s:
        return "Mac"
    if not (s[0].isascii() and (s[0].isalnum() or s[0] == "_")):
        s = "Mac " + s
    s = s[:64].rstrip()
    return s if NAME_RE.match(s) else "Mac"


def default_device_name() -> str:
    try:
        out = subprocess.run(["/usr/sbin/scutil", "--get", "ComputerName"], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    return sanitize_device_name(out)


@dataclass(frozen=True)
class ServerPin:
    url: str
    pubkey: bytes          # raw 32-byte Ed25519 public key
    key_id: str
    helper_id: str


@dataclass(frozen=True)
class Policy:
    build_timeout_s: int = 900
    disk_budget_mb: int = 20480
    mem_budget_mb: int = 6144


@dataclass(frozen=True)
class Capabilities:
    system_info: bool = False
    screen_capture: bool = False
    xcode_build: bool = False

    def enabled(self, action: str) -> bool:
        return {"system.info": self.system_info, "screen.capture": self.screen_capture,
                "xcode.build": self.xcode_build}[action]


ACTIONS = ("system.info", "screen.capture", "xcode.build")


@dataclass(frozen=True)
class Workspace:
    id: str
    remote_url: str
    project: str
    schemes: tuple[str, ...]
    configurations: tuple[str, ...]
    destinations: tuple[str, ...]
    confirm: bool = False
    expires_at: Optional[int] = None     # unix ms, None = no expiry

    @property
    def fingerprint(self) -> str:
        return bhproto.repo_fingerprint(self.remote_url)

    def expired(self, now_ms: int) -> bool:
        return self.expires_at is not None and now_ms >= self.expires_at


@dataclass(frozen=True)
class Config:
    server: Optional[ServerPin]
    device_name: str
    policy: Policy = field(default_factory=Policy)
    capabilities: Capabilities = field(default_factory=Capabilities)
    workspaces: tuple[Workspace, ...] = ()

    @classmethod
    def empty(cls, device_name: str) -> "Config":
        return cls(server=None, device_name=sanitize_device_name(device_name))

    def workspace(self, wid: str) -> Optional[Workspace]:
        return next((w for w in self.workspaces if w.id == wid), None)

    def with_workspace(self, ws: Workspace, *, replace_existing: bool = False) -> "Config":
        """Add a grant. Re-adding an identical grant (every field equal after validation) returns this same
        Config. A different grant with the same id raises GrantExists unless replace_existing."""
        ws = validate_workspace(ws)
        old = self.workspace(ws.id)
        if old == ws:
            return self
        if old is not None and not replace_existing:
            raise GrantExists(old)
        return replace(self, workspaces=tuple(w for w in self.workspaces if w.id != ws.id) + (ws,))

    def without_workspace(self, wid: str) -> "Config":
        return replace(self, workspaces=tuple(w for w in self.workspaces if w.id != wid))

    def with_capability(self, action: str, on: bool) -> "Config":
        key = {"system.info": "system_info", "screen.capture": "screen_capture", "xcode.build": "xcode_build"}
        if action not in key:
            raise ConfigError(f"unknown capability {action!r}; one of {', '.join(ACTIONS)}")
        return replace(self, capabilities=replace(self.capabilities, **{key[action]: on}))


# ---------------------------------------------------------------- validation
def _name_list(values, what: str, max_items: int) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, (list, tuple)) or not values:
        raise ConfigError(f"at least one {what} is required")
    if len(values) > max_items:
        raise ConfigError(f"at most {max_items} {what}s")
    out = []
    for v in values:
        if not isinstance(v, str) or not NAME_RE.match(v):
            raise ConfigError(f"{what} {v!r} is not allowed: use letters, digits, space, . _ + - "
                              f"(first character a letter, digit or _), at most 64 characters")
        out.append(v)
    return tuple(dict.fromkeys(out))


def validate_workspace(ws: Workspace) -> Workspace:
    if not isinstance(ws.id, str) or not WORKSPACE_ID_RE.match(ws.id):
        raise ConfigError(f"workspace id {ws.id!r} must be lowercase letters, digits, _ or - (max 64)")
    try:
        bhproto.normalize_repo_url(ws.remote_url)
    except bhproto.ProtocolError:
        raise ConfigError(f"repo URL must be a public https URL without credentials, port, query or "
                          f"fragment (got {ws.remote_url!r})") from None
    p = ws.project
    if (not isinstance(p, str) or not PROJECT_RE.match(p) or p.startswith("/") or ".." in p.split("/")
            or not (p.endswith(".xcodeproj") or p.endswith(".xcworkspace"))):
        raise ConfigError(f"project {p!r} must be a relative path to a .xcodeproj or .xcworkspace inside the repo")
    schemes = _name_list(ws.schemes, "scheme", 32)
    configs = _name_list(ws.configurations, "configuration", 8)
    if isinstance(ws.destinations, str) or not ws.destinations:
        raise ConfigError("at least one destination is required")
    for d in ws.destinations:
        if d not in DESTINATIONS:
            raise ConfigError(f"destination {d!r} is not accepted; use exactly one of: "
                              + " | ".join(DESTINATIONS))
    if type(ws.confirm) is not bool:
        raise ConfigError("confirm must be true or false")
    if ws.expires_at is not None and (type(ws.expires_at) is not int or ws.expires_at < 0):
        raise ConfigError("expires_at must be a time")
    # config.toml stores whole seconds: keep only those from here on, so what is compared is what is saved
    expires = None if ws.expires_at is None else ws.expires_at - ws.expires_at % 1000
    return replace(ws, schemes=schemes, configurations=configs, destinations=tuple(dict.fromkeys(ws.destinations)),
                   expires_at=expires)


def grant_changes(old: Workspace, new: Workspace, when=None) -> list[tuple[str, bool]]:
    """What replacing `old` with `new` changes: (sentence, more_permissive) per change, in field order.
    `when` formats a unix-ms time."""
    when = when or _ms_rfc3339
    out: list[tuple[str, bool]] = []
    if new.remote_url != old.remote_url:
        out.append((f"repo: {old.remote_url} -> {new.remote_url} (a different repository)", True))
    if new.project != old.project:
        out.append((f"project: {old.project} -> {new.project}", True))
    for label, a, b in (("scheme", old.schemes, new.schemes), ("configuration", old.configurations,
                                                                new.configurations),
                        ("destination", old.destinations, new.destinations)):
        added = [x for x in b if x not in a]
        removed = [x for x in a if x not in b]
        if added:
            out.append((f"{label}s added: {', '.join(added)}", True))
        if removed:
            out.append((f"{label}s removed: {', '.join(removed)}", False))
        if not added and not removed and a != b:
            out.append((f"{label} order: {', '.join(a)} -> {', '.join(b)}", False))
    if old.confirm != new.confirm:
        out.append((f"confirmation before every build turned {'on' if new.confirm else 'off'}", old.confirm))
    if old.expires_at != new.expires_at:
        if new.expires_at is None:
            out.append((f"expiry removed (was {when(old.expires_at)})", True))
        elif old.expires_at is None:
            out.append((f"expiry added: {when(new.expires_at)}", False))
        elif new.expires_at > old.expires_at:
            out.append((f"expiry extended: {when(old.expires_at)} -> {when(new.expires_at)}", True))
        else:
            out.append((f"expiry shortened: {when(old.expires_at)} -> {when(new.expires_at)}", False))
    return out


def parse_expires(text: str, now_ms: int) -> int:
    """`7d`, `12h`, `30m` or an RFC 3339 time -> unix ms."""
    m = re.fullmatch(r"([0-9]{1,4})([dhm])", text or "")
    if m:
        unit = {"d": 86400_000, "h": 3600_000, "m": 60_000}[m.group(2)]
        return now_ms + int(m.group(1)) * unit
    return _rfc3339_ms(text)


def _rfc3339_ms(text: str) -> int:
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ConfigError(f"expiry {text!r} must be like 7d, 12h, 30m or an RFC 3339 time") from None
    if dt.tzinfo is None:
        raise ConfigError(f"expiry {text!r} needs a time zone (for example a trailing Z)")
    return int(dt.timestamp() * 1000)


def _ms_rfc3339(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


ms_rfc3339 = _ms_rfc3339       # UTC, whole seconds, trailing Z: the stored form


# ---------------------------------------------------------------- TOML in and out
def _keys(table: dict, allowed: set[str], where: str) -> None:
    extra = set(table) - allowed
    if extra:
        raise ConfigError(f"unknown key {sorted(extra)[0]!r} in {where}")


def _int(table: dict, k: str, default: int, lo: int, hi: int) -> int:
    v = table.get(k, default)
    if type(v) is not int or not lo <= v <= hi:
        raise ConfigError(f"policy.{k} must be a whole number from {lo} to {hi}")
    return v


def _bool(table: dict, k: str) -> bool:
    v = table.get(k, False)
    if type(v) is not bool:
        raise ConfigError(f"{k} must be true or false")
    return v


def parse(text: str) -> Config:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config.toml is not valid TOML: {e}") from None
    _keys(doc, {"server", "device", "policy", "capabilities", "workspaces"}, "config.toml")
    server = None
    if "server" in doc:
        s = doc["server"]
        _keys(s, {"url", "pubkey", "key_id", "helper_id"}, "[server]")
        try:
            pub = bhproto.unb64u(s["pubkey"])
            if len(pub) != 32 or bhproto.key_id(pub) != s["key_id"]:
                raise ValueError
            if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                                s["helper_id"]):
                raise ValueError
            url = s["url"]
            bhproto._check_endpoint(url, allow_insecure_local=url.startswith("ws://"))
        except (KeyError, ValueError, TypeError):
            raise ConfigError("[server] is damaged; run 'bh pair' again") from None
        server = ServerPin(url=url, pubkey=pub, key_id=s["key_id"], helper_id=s["helper_id"])
    dev = doc.get("device", {})
    _keys(dev, {"name"}, "[device]")
    name = dev.get("name", "Mac")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ConfigError("device.name must match the allowed name format")
    pol = doc.get("policy", {})
    _keys(pol, {"build_timeout_s", "disk_budget_mb", "mem_budget_mb"}, "[policy]")
    policy = Policy(_int(pol, "build_timeout_s", 900, 60, MAX_TIMEOUT_S),
                    _int(pol, "disk_budget_mb", 20480, 1, MAX_DISK_MB),
                    _int(pol, "mem_budget_mb", 6144, 1, MAX_MEM_MB))
    cap = doc.get("capabilities", {})
    _keys(cap, {"system_info", "screen_capture", "xcode_build"}, "[capabilities]")
    caps = Capabilities(_bool(cap, "system_info"), _bool(cap, "screen_capture"), _bool(cap, "xcode_build"))
    wss = []
    seen = set()
    raw_ws = doc.get("workspaces", [])
    if not isinstance(raw_ws, list):
        raise ConfigError("workspaces must be [[workspaces]] tables")
    for w in raw_ws:
        _keys(w, {"id", "remote_url", "project", "schemes", "configurations", "destinations", "confirm",
                  "expires_at"}, "[[workspaces]]")
        try:
            exp = w.get("expires_at", "")
            ws = Workspace(id=w["id"], remote_url=w["remote_url"], project=w["project"],
                           schemes=tuple(w["schemes"]) if isinstance(w["schemes"], list) else w["schemes"],
                           configurations=tuple(w.get("configurations", ["Debug"])),
                           destinations=tuple(w.get("destinations", [DESTINATIONS[0]])),
                           confirm=w.get("confirm", False),
                           expires_at=None if exp == "" else _rfc3339_ms(exp))
        except KeyError as e:
            raise ConfigError(f"[[workspaces]] is missing {e.args[0]}") from None
        except TypeError:
            raise ConfigError("[[workspaces]] has a value of the wrong type") from None
        ws = validate_workspace(ws)
        if ws.id in seen:
            raise ConfigError(f"workspace {ws.id!r} appears twice")
        seen.add(ws.id)
        wss.append(ws)
    return Config(server=server, device_name=name, policy=policy, capabilities=caps, workspaces=tuple(wss))


def _q(s: str) -> str:
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    out = "".join(c if ord(c) >= 0x20 and c != "\x7f" else f"\\u{ord(c):04x}" for c in out)
    return f'"{out}"'


def _arr(vals) -> str:
    return "[" + ", ".join(_q(v) for v in vals) + "]"


def dump(cfg: Config) -> str:
    lines = []
    if cfg.server:
        s = cfg.server
        lines += ["[server]                      # written by `bh pair`; never edit by hand",
                  f"url = {_q(s.url)}", f"pubkey = {_q(bhproto.b64u(s.pubkey))}", f"key_id = {_q(s.key_id)}",
                  f"helper_id = {_q(s.helper_id)}", ""]
    p, c = cfg.policy, cfg.capabilities
    lines += ["[device]", f"name = {_q(cfg.device_name)}", "",
              "[policy]", f"build_timeout_s = {p.build_timeout_s}", f"disk_budget_mb = {p.disk_budget_mb}",
              f"mem_budget_mb = {p.mem_budget_mb}", "",
              "[capabilities]                # all false after pairing; `bh caps enable` flips them",
              f"system_info = {str(c.system_info).lower()}", f"screen_capture = {str(c.screen_capture).lower()}",
              f"xcode_build = {str(c.xcode_build).lower()}", ""]
    for w in cfg.workspaces:
        lines += ["[[workspaces]]                # one grant; written by `bh grant add`",
                  f"id = {_q(w.id)}", f"remote_url = {_q(w.remote_url)}", f"project = {_q(w.project)}",
                  f"schemes = {_arr(w.schemes)}", f"configurations = {_arr(w.configurations)}",
                  f"destinations = {_arr(w.destinations)}", f"confirm = {str(w.confirm).lower()}",
                  f"expires_at = {_q('' if w.expires_at is None else _ms_rfc3339(w.expires_at))}", ""]
    return "\n".join(lines)


def load(d: Path = DEFAULT_DIR) -> Config:
    """Read and validate config.toml (0600 in the 0700 dir). Missing file: an empty, unpaired config."""
    p = Path(d) / CONFIG_NAME
    if not os.path.lexists(p):
        keyfile.check_private_dir(Path(d))
        return Config.empty(default_device_name())
    try:
        text = keyfile.read_private_file(p).decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError("config.toml is not UTF-8") from None
    return parse(text)


class ConfigBusy(ConfigError):
    pass


_held = threading.local()


def _key(d: Path) -> str:
    return os.path.realpath(d)


def lock_held(d: Path = DEFAULT_DIR) -> bool:
    """True while this thread holds the config lock for `d`."""
    return _key(d) in getattr(_held, "dirs", set())


@contextlib.contextmanager
def locked(d: Path = DEFAULT_DIR):
    """The one exclusive lock for changing config.toml: flock on config.lock (0600, in the 0700 config dir).
    Waits up to LOCK_TIMEOUT_S, then raises ConfigBusy; never proceeds without the lock. Not reentrant."""
    d = Path(d)
    keyfile.check_private_dir(d)
    try:
        fd = os.open(d / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as e:
        raise ConfigError(f"cannot open {LOCK_NAME} in {d} ({e.strerror}); nothing was changed") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise ConfigError(f"{d / LOCK_NAME} is not a regular file owned by you; remove it and retry")
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ConfigBusy(f"another 'bh' command or the helper is changing config.toml and did not "
                                     f"finish within {LOCK_TIMEOUT_S:g} s; nothing was changed. Try again; if "
                                     f"this keeps happening, look for a stuck 'bh' process") from None
                time.sleep(0.05)
            except OSError as e:
                raise ConfigError(f"cannot lock {d / LOCK_NAME} ({e.strerror}); nothing was changed") from None
        dirs = getattr(_held, "dirs", None)
        if dirs is None:
            dirs = _held.dirs = set()
        dirs.add(_key(d))
        try:
            yield
        finally:
            dirs.discard(_key(d))
    finally:
        os.close(fd)        # releases the flock


def _write(d: Path, cfg: Config) -> None:
    if not lock_held(d):
        raise RuntimeError("config.toml written without the config lock")
    text = dump(cfg)
    parse(text)       # never write a file we could not read back
    keyfile.write_private_file(Path(d) / CONFIG_NAME, text.encode("utf-8"))


def update(d: Path, change) -> Config:
    """The way to change config.toml: under the lock, load what is on disk now, apply `change(cfg) -> Config`,
    and write the result unless `change` returned the same object. Returns the resulting Config. Errors raised
    by `change` leave the file untouched."""
    with locked(d):
        cfg = load(d)
        new = change(cfg)
        if new is not cfg:
            _write(d, new)
        return new


def save(d: Path, cfg: Config) -> None:
    """Write a whole new Config under the lock without reading the old one: only for starting fresh (pairing)
    and tests. Anything derived from a loaded Config goes through update()."""
    with locked(d):
        _write(d, cfg)


def mtime(d: Path = DEFAULT_DIR) -> Optional[float]:
    try:
        return os.stat(Path(d) / CONFIG_NAME).st_mtime_ns
    except FileNotFoundError:
        return None
