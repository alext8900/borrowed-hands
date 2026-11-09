"""Plugin settings (SPEC 8.1), parsed once at register() into an immutable Settings. Never raises: a bad value is
recorded in `problems` (shown by /hands status) and the safe default is used. None of these are secrets."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Tuple

from . import bhproto

DEFAULT_PORT = 8790
PROTOCOL_MAX_FRAME = 16777216        # PROTOCOL 1: the most any frame may be
MAX_FRAME = PROTOCOL_MAX_FRAME      # screen results need the full v1 wire budget
TRUSTED_PROXIES = ("railway",)
_OWNER_RE = re.compile(r"[a-z0-9_]{1,32}:[^\s,]{1,128}\Z")


@dataclass(frozen=True)
class Settings:
    listen_enabled: bool = False
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    public_url: Optional[str] = None
    owners: Tuple[str, ...] = ()
    max_frame_bytes: int = MAX_FRAME
    trusted_proxy: Optional[str] = None
    problems: Tuple[str, ...] = field(default=())

    def is_owner(self, platform: Optional[str], user_id: Optional[str]) -> bool:
        if not platform or not user_id:
            return False
        return f"{platform}:{user_id}" in self.owners


def _bool(v: Any) -> Optional[bool]:
    return v if isinstance(v, bool) else None


def load(get: Callable[[str, Any], Any], env: Mapping[str, str]) -> Settings:
    problems = []
    _unset = object()

    def setting(key: str) -> Any:
        try:
            return get(key, _unset)
        except Exception:  # a rejected key or unreadable config: treat as unset
            problems.append(f"cannot read setting {key}")
            return _unset

    # listen.enabled: setting, else BH_LISTEN=1; BH_LISTEN=0 always wins.
    raw = setting("listen.enabled")
    enabled = False
    if raw is not _unset and _bool(raw) is None:
        problems.append("listen.enabled must be true or false")
    elif raw is not _unset:
        enabled = raw
    elif env.get("BH_LISTEN") == "1":
        enabled = True
    if env.get("BH_LISTEN") == "0":
        enabled = False

    host = setting("listen.host")
    if host is _unset:
        host = "0.0.0.0"
    elif not isinstance(host, str) or not host or len(host) > 255:
        problems.append("listen.host must be a host name or address")
        host = "0.0.0.0"

    port: Any = setting("listen.port")
    if port is _unset:
        port = env.get("BH_PORT", DEFAULT_PORT)
    if isinstance(port, str) and port.isdigit():
        port = int(port)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        problems.append("listen.port must be an integer 1-65535")
        port = DEFAULT_PORT
        enabled = False  # never listen on a port the owner did not ask for

    url = setting("public_url")
    if url is _unset:
        url = env.get("BH_PUBLIC_URL") or (
            f"wss://{env['RAILWAY_PUBLIC_DOMAIN']}/bh/v1/ws" if env.get("RAILWAY_PUBLIC_DOMAIN") else None)
    if url is not None:
        try:
            if not isinstance(url, str):
                raise bhproto.ProtocolError("not a string")
            # ws://localhost is accepted for local tests only; the helper refuses it without --insecure-local.
            bhproto._check_endpoint(url, allow_insecure_local=True)
        except bhproto.ProtocolError:
            problems.append("public_url must be wss://<host>/bh/v1/ws")
            url = None

    owners_raw = setting("owners")
    if owners_raw is _unset:
        owners_raw = [o.strip() for o in env.get("BH_OWNERS", "").split(",") if o.strip()]
    owners: Tuple[str, ...] = ()
    if not isinstance(owners_raw, list) or not all(isinstance(o, str) and _OWNER_RE.match(o) for o in owners_raw):
        problems.append("owners must be a list of platform:user_id")
    else:
        owners = tuple(owners_raw)

    mfb = setting("max_frame_bytes")
    if mfb is _unset:
        mfb = MAX_FRAME
    if isinstance(mfb, bool) or not isinstance(mfb, int) or not 65536 <= mfb <= PROTOCOL_MAX_FRAME:
        problems.append(f"max_frame_bytes must be an integer from 65536 to {PROTOCOL_MAX_FRAME}")
        mfb = MAX_FRAME

    # X-Forwarded-For is trusted only behind a named proxy that appends the real client address (Railway's edge).
    proxy = setting("trusted_proxy")
    if proxy is _unset:
        proxy = env.get("BH_TRUSTED_PROXY") or None
    if proxy is not None and proxy not in TRUSTED_PROXIES:
        problems.append(f"trusted_proxy must be one of {', '.join(TRUSTED_PROXIES)} or unset")
        proxy = None

    return Settings(listen_enabled=enabled, host=host, port=port, public_url=url, owners=owners,
                    max_frame_bytes=mfb, trusted_proxy=proxy, problems=tuple(problems))
