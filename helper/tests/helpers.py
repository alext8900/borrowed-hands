"""Shared test builders: a server key, a paired config, signed contracts."""
from __future__ import annotations

import hashlib
import os
import uuid

from borrowed_hands_helper import config
from borrowed_hands_helper._shared import bhproto
from borrowed_hands_helper.config import Config, ServerPin, Workspace

SERVER_SEED = bytes(range(32))
SERVER_PRIV = bhproto.priv_from_seed(SERVER_SEED)
SERVER_PUB = bhproto.pub_raw(SERVER_PRIV)
SERVER_KID = bhproto.key_id(SERVER_PUB)
OTHER_PRIV = bhproto.priv_from_seed(bytes(range(1, 33)))
HELPER_ID = "11111111-2222-4333-8444-555555555555"
REMOTE = "https://github.com/alext8900/bh-demo.git"
NOW = 1_800_000_000_000


def workspace(**kw) -> Workspace:
    base = dict(id="demo", remote_url=REMOTE, project="Demo.xcodeproj", schemes=("Demo",),
                configurations=("Debug",), destinations=("platform=macOS,arch=arm64",), confirm=False,
                expires_at=None)
    base.update(kw)
    return Workspace(**base)


def paired_config(url="ws://127.0.0.1:1/bh/v1/ws", ws=True, caps=("system.info", "xcode.build")) -> Config:
    cfg = Config.empty("Test Mac")
    cfg = Config(server=ServerPin(url, SERVER_PUB, SERVER_KID, HELPER_ID), device_name=cfg.device_name)
    for a in caps:
        cfg = cfg.with_capability(a, True)
    if ws:
        cfg = cfg.with_workspace(workspace())
    return cfg


def build_params(**kw) -> dict:
    p = {"workspace_id": "demo", "repo_fingerprint": bhproto.repo_fingerprint(REMOTE), "commit": "a" * 40,
         "scheme": "Demo", "configuration": "Debug", "destination": "platform=macOS,arch=arm64"}
    p.update(kw)
    return p


def contract(action="xcode.build", params=None, now=NOW, **kw) -> dict:
    c = {"v": 1, "job_id": str(uuid.uuid4()), "nonce": bhproto.b64u(os.urandom(16)), "helper_id": HELPER_ID,
         "server_key_id": SERVER_KID, "action": action,
         "params": (build_params() if action == "xcode.build" else {}) if params is None else params,
         "issued_at": now - 1000, "expires_at": now + 300_000, "approval_ref": "none"}
    c.update(kw)
    return c


def sign_contract(c: dict, priv=SERVER_PRIV) -> tuple[bytes, str]:
    raw = bhproto.canonical(c)
    return raw, bhproto.sign(priv, bhproto.CTX_JOB, raw)


def chash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
