"""`bh selftest`: prove the loop on this Mac, one PASS/FAIL line per check, each FAIL with its fix (SPEC 8.4).

Checks, in order: Python and packages; key and config permissions (PROTOCOL 3.4); paired; Xcode; the
production sandbox install; `sudo -n` reaches the dispatcher; the server is reachable and proves its key;
each enabled capability is usable. `--build <workspace>` also runs one real local build of the tip of that
workspace's first `bh/*` branch through the full dispatch path and prints the diagnostics.

The server check never fights the running helper for the connection: if the daemon is up and connected, its
own verified handshake is the evidence; only without a daemon does selftest dial the server itself.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from importlib import metadata
from typing import Optional

from . import config, keyfile
from ._shared import bhproto
from .caps import RUNNER_XCODE_APP, SUDOERS_PATH, Probes
from .connection import Closed, ConnectFailed, Stop, handshake_auth
from .daemon import daemon_running, read_status
from .env import Env

SUDO_FIX = "sudo rule missing: run 'bh setup'"
PROOF_FIX = "server failed to prove its key; check the pairing string"
REPAIR_FIX = ("re-pair: run 'bh unpair', then 'bh setup' (it asks for a fresh string from /hands pair; "
              "grants are reset, so re-add them there)")
DAEMON_WAIT_S = 15


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def xcode_problem(env: Env) -> tuple[str, str]:
    """Why the build runner's Xcode is unusable, and the one command that fixes it."""
    st = env.xcode_status()
    if st.version is None and st.problem:
        return st.problem, st.fix
    return (f"Xcode at {RUNNER_XCODE_APP} is not usable",
            "install Xcode from the App Store and open it once")


def _sudo_rule(env: Env, policy: config.Policy) -> list[Check]:
    """Two separate facts: our file is there (a), and sudo itself permits each exact command (b). Sudo merely
    working through broader rights is not (a). Permitted is not proven password-free (see sudo_rule_permitted)."""
    ok, why = env.sudo_rule_file()
    refused = env.sudo_rule_permitted(policy)
    if ok:
        installed = Check("sudo rule: installed", True, f"{SUDOERS_PATH}, root:wheel 0440 (its contents are root-only; "
                                                        f"'bh setup' compares them exactly)")
    else:
        installed = Check("sudo rule: installed", False, why + (" (builds work now only because this account has "
                                                                "broader sudo rights)" if refused is None else ""),
                          "run 'bh setup'")
    if refused is None:
        permitted = Check("sudo rule: permitted", True, "sudo confirms each of the six build commands is allowed")
    else:
        permitted = Check("sudo rule: permitted", False, f"sudo does not permit '{refused}'" + (
            " (another sudo rule on this Mac overrides ours)" if ok else ""), "run 'bh setup'")
    return [installed, permitted]


def _versions() -> Check:
    if sys.version_info < (3, 11):
        return Check("python", False, f"Python {sys.version.split()[0]} is too old", "reinstall with Python 3.11+")
    parts = [f"Python {sys.version.split()[0]}"]
    for pkg in ("borrowed-hands-helper", "aiohttp", "cryptography"):
        try:
            parts.append(f"{pkg} {metadata.version(pkg)}")
        except metadata.PackageNotFoundError:
            return Check("python and packages", False, f"{pkg} is not installed",
                         "reinstall from your checkout: uv tool install --force ./helper")
    return Check("python and packages", True, ", ".join(parts))


def _storage(env: Env) -> tuple[Check, Optional[config.Config]]:
    name = "key and config permissions"
    if not os.path.lexists(env.home):           # never set up (or uninstalled): nothing to protect yet
        return Check(name, True, "nothing stored yet (this Mac is not set up)"), \
            config.Config.empty(config.default_device_name())
    try:
        keyfile.check_private_dir(env.home)
        cfg = config.load(env.home)
        if cfg.server is None:
            return Check(name, True, "config readable (not paired yet)"), cfg
        keyfile.load_device_key(env.home)
        return Check(name, True, "0700 directory, 0600 key and config"), cfg
    except keyfile.UnsafeKeyStorage as e:
        return Check(name, False, str(e), "fix the permissions shown, then run 'bh selftest' again"), None
    except config.ConfigError as e:
        return Check(name, False, f"config.toml: {e}", "fix config.toml, or start over with 'bh unpair' and "
                                                       "'bh setup'"), None


async def _server(env: Env, cfg: config.Config) -> Check:
    name = "server reachable and key proof valid"
    if daemon_running(env.home):
        st = read_status(env.home)
        for _ in range(DAEMON_WAIT_S * 4):           # a helper started a moment ago (bh setup) may still dial
            if st.get("state") in ("connected", "stopped"):
                break
            await asyncio.sleep(0.25)
            st = read_status(env.home)
        if st.get("state") == "connected":
            return Check(name, True, f"the running helper is connected (session {st.get('detail', '')})")
        return Check(name, False, f"the running helper is not connected: {st.get('detail') or st.get('state')}",
                     "see ~/Library/Logs/BorrowedHands/helper.log")
    try:
        priv = keyfile.load_device_key(env.home)
        t = await env.connect(cfg.server.url, env.timing)
    except ConnectFailed as e:
        host = re.sub(r"^wss?://([^/]+).*$", r"\1", cfg.server.url)
        return Check(name, False, f"cannot reach {host}: {e}", "check the network, or that the gateway is up")
    try:
        await handshake_auth(t, cfg.server, priv, env.timing)
        return Check(name, True, "server proved its pinned key")
    except Stop as s:
        if "prove" in s.message:
            return Check(name, False, PROOF_FIX, REPAIR_FIX)
        return Check(name, False, s.message, REPAIR_FIX)
    except Closed as c:
        if c.code == 4401:
            return Check(name, False, "server does not recognise this Mac", REPAIR_FIX)
        if c.code == 4403:
            return Check(name, False, "this Mac was unpaired by the server", REPAIR_FIX)
        return Check(name, False, f"the server closed the connection ({c.code})", "retry in a minute")
    except (bhproto.ProtocolError, asyncio.TimeoutError, OSError) as e:
        return Check(name, False, f"handshake failed: {e}", "retry; if it persists, re-pair")
    finally:
        await t.close(1000)


def _capabilities(env: Env, cfg: config.Config, p: Probes) -> list[Check]:
    out = []
    ex = env.get_executors()
    c = cfg.capabilities
    if not (c.system_info or c.xcode_build or c.screen_capture):
        out.append(Check("capabilities", True, "none enabled yet (pairing grants nothing)"))
    if c.system_info:
        out.append(Check("system.info", "system.info" in ex, "enabled" if "system.info" in ex else "not available"))
    if c.xcode_build:
        problems = []
        if "xcode.build" not in ex:
            problems.append("the xcode.build executor is not part of this build")
        if not cfg.workspaces:
            problems.append("no workspace is granted")
        if not (p.install.ok and p.sudo_ok and p.xcode):
            problems.append("needs Xcode, the production sandbox and the sudo rule (see above)")
        out.append(Check("xcode.build", not problems, "; ".join(problems) or
                         f"usable for {', '.join(w.id for w in cfg.workspaces)}",
                         "" if not problems else "bh grant add xcode.build ... / bh setup"))
    if c.screen_capture:
        ok = "screen.capture" in ex and p.screen_granted
        out.append(Check("screen.capture", ok, "Screen Recording granted" if ok else
                         "Screen Recording not granted to Terminal: System Settings > Privacy & Security > Screen Recording",
                         "" if ok else "allow it, then run 'bh selftest' again"))
    return out


async def _build(env: Env, cfg: config.Config, wid: str) -> list[Check]:
    name = f"local build of {wid}"
    ws = cfg.workspace(wid)
    if ws is None:
        return [Check(name, False, f"no grant for workspace {wid!r}", f"bh grant add xcode.build --workspace {wid} ...")]
    ex = env.get_executors().get("xcode.build")
    if ex is None:
        return [Check(name, False, "the xcode.build executor is not part of this build yet",
                      "update borrowed-hands-helper")]
    rc, out = env.run(["/usr/bin/git", "ls-remote", "--heads", ws.remote_url, "refs/heads/bh/*"])
    tips = sorted(line.split("\t") for line in out.splitlines() if re.fullmatch(r"[0-9a-f]{40}\trefs/heads/bh/\S+", line))
    if rc != 0 or not tips:
        return [Check(name, False, f"no bh/* branch found on {bhproto.normalize_repo_url(ws.remote_url)}",
                      "push a branch named bh/<something> to that repo")]
    commit, ref = tips[0][0], tips[0][1]
    from .executors import AcceptedJob
    params = {"workspace_id": ws.id, "repo_fingerprint": ws.fingerprint, "commit": commit, "scheme": ws.schemes[0],
              "configuration": ws.configurations[0], "destination": ws.destinations[0]}
    job = AcceptedJob(job_id=str(uuid.uuid4()), action="xcode.build", params=params, contract={"params": params},
                      contract_hash="selftest", expires_at=0, approval_ref="none", workspace=ws, policy=cfg.policy)

    async def emit(state):
        env.say(f"      ... {state}")
    env.say(f"      building {ref} @ {commit[:12]} ({params['scheme']}, {params['destination']})")
    outcome = await ex.run(job, emit)
    if outcome == "busy":
        return [Check(name, False, "another build is already running", "retry when it finishes")]
    env.say(json.dumps({"state": outcome.state, "result": outcome.result, "error": outcome.error}, indent=2))
    ok = outcome.state in ("succeeded", "failed") and outcome.error is None
    return [Check(name, ok, f"{outcome.state} (diagnostics above)" if ok else f"{outcome.state}: {outcome.error}",
                  "" if ok else "see the error above")]


def install_fix(st) -> str:
    """The fix line for the sandbox check: with the build user gone, setup refuses until uninstall has finished."""
    if st.ok:
        return ""
    from .caps import BUILDER_MISSING
    return "run 'bh uninstall', then 'bh setup'" if BUILDER_MISSING in st.problems else "run 'bh setup'"


async def collect(env: Env, build: Optional[str] = None) -> list[Check]:
    checks = [_versions()]
    st, cfg = _storage(env)
    checks.append(st)
    if cfg is not None:
        checks.append(Check("paired", cfg.server is not None,
                            f"as '{cfg.device_name}' (helper {cfg.server.helper_id[:8]})" if cfg.server else "not paired",
                            "" if cfg.server else "run 'bh setup' (it asks for the string from /hands pair)"))
    p = env.probes(cfg.policy if cfg is not None else config.Policy())
    if p.xcode:
        note = env.xcode_status().note
        checks.append(Check("Xcode", True, f"Xcode {p.xcode[0]} ({p.xcode[1]}) at {RUNNER_XCODE_APP}"
                            + (f" (note: {note})" if note else "")))
    else:
        problem, fix = xcode_problem(env)
        checks.append(Check("Xcode", False, problem, fix))
    checks.append(Check("production build sandbox", p.install.ok, "root-owned, no TEST_MODE, no test profiles"
                        if p.install.ok else "; ".join(p.install.problems), install_fix(p.install)))
    checks += _sudo_rule(env, cfg.policy if cfg is not None else config.Policy())
    if cfg is not None and cfg.server is not None and st.ok:
        checks.append(await _server(env, cfg))
        checks += _capabilities(env, cfg, p)
        if build:
            checks += await _build(env, cfg, build)
    return checks


def run(env: Env, build: Optional[str] = None) -> int:
    checks = asyncio.run(collect(env, build))
    env.say("Self-test")
    for c in checks:
        env.say(f"  {'PASS' if c.ok else 'FAIL'}  {c.name}: {c.detail}")
        if not c.ok and c.fix:
            env.say(f"        fix: {c.fix}")
    failed = [c for c in checks if not c.ok]
    env.say("All checks passed." if not failed else f"{len(failed)} check(s) failed.")
    return 0 if not failed else 1
