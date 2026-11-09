"""`bh setup`: the one guided path from a fresh `uv tool install` to a paired, granted, running helper whose
self-test passes (SPEC 8.4, 11.2). Every step checks first and skips what is already done, so re-running it is
safe. Every admin command is printed before it runs; only the production sandbox is ever installed.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from . import account, caps as capsmod, config, keyfile, selftest
from ._shared import SANDBOX
from .config import DESTINATIONS, ConfigError, Policy, Workspace
from .env import Env, show

SUDO_GUIDANCE = ("Borrowed Hands needs your admin password twice: once to create the hidden build user "
                 "'bh-builder' and install the build sandbox in /opt/borrowed-hands, once to add one sudo rule "
                 "that lets this account start a build. It never gets root for anything else.")
SCREEN_GUIDANCE = ("To let Hermes see your screen on request, allow {app} in System Settings > Privacy & Security > "
                   "Screen Recording. Nothing is captured unless a request you granted arrives.")
SUDOERS_PATH = capsmod.SUDOERS_PATH
SUDOERS_STAGING = "/etc/sudoers.d/.borrowed-hands.new"      # sudo ignores names containing '.'
DISPATCH = "/opt/borrowed-hands/bin/bh-dispatch"


class SetupFailed(Exception):
    def __init__(self, message: str, code: int = 1, partial: bool = False):
        super().__init__(message)
        self.code = code
        self.partial = partial      # an admin command of this step may already have changed the Mac


def sudoers_text(uid: int, policy: Policy) -> str:
    """The pinned rule: the owner's numeric uid and one exact dispatcher argv per timeout tier, with this Mac's
    budgets. No `*`, `?` or character classes: sudo's `*` also matches spaces, so a glob would admit extra
    flags, and the rule must not rely on the dispatcher refusing them. Changing the budgets later makes
    `bh selftest` report the rule missing; `bh setup` then rewrites it."""
    head = f"#{int(uid)} ALL=(root) NOPASSWD: "
    return "".join(head + cmd + "\n" for cmd in capsmod.rule_commands(policy))


def _yes(env: Env, question: str, default: bool) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    a = env.ask(f"{question} {hint} ").strip().lower()
    return default if not a else a in ("y", "yes")


def _admin(env: Env, argv: list[str], why: str) -> None:
    env.say(f"  {why}:")
    env.say(f"  $ {show(argv)}")
    rc = env.sudo(argv)
    if rc != 0:
        raise SetupFailed(f"that command failed (exit {rc})", partial=True)


def step_xcode(env: Env) -> None:
    v = env.xcode()
    if v is None:
        problem, fix = selftest.xcode_problem(env)
        raise SetupFailed(f"[1/7] Xcode: {problem}. Fix: {fix}, then run 'bh setup' again")
    env.say(f"[1/7] Xcode {v[0]} ({v[1]}) at {capsmod.RUNNER_XCODE_APP}: ok")
    note = env.xcode_status().note
    if note:
        env.say(f"  Note: {note}.")


def step_sandbox(env: Env) -> None:
    if env.install_status().ok:
        env.say("[2/7] Build sandbox: already installed (production)")
        return
    env.say("[2/7] Build sandbox")
    env.say(SUDO_GUIDANCE)
    _admin(env, ["bash", str(SANDBOX / "setup-bh-builder.sh")],
           "creating the hidden build user and installing the sandbox (production, no test profiles)")
    st = env.install_status()
    if not st.ok:
        raise SetupFailed("the build sandbox is still not a production install: " + "; ".join(st.problems),
                          partial=True)


def step_sudoers(env: Env) -> None:
    keyfile.ensure_private_dir(env.home)
    policy = config.load(env.home).policy
    text = sudoers_text(env.uid, policy)
    tmpdir = tempfile.mkdtemp(prefix="bh-setup-")
    try:
        src = Path(tmpdir) / "borrowed-hands"
        src.write_text(text)
        os.chmod(src, 0o644)
        # (a) Our own exact file, not just "sudo -n works": broader sudo rights (NOPASSWD: ALL) would otherwise hide
        # a missing or stale rule until they are removed. The file is root-only, so its bytes are compared with sudo.
        installed = False
        if env.sudo_rule_file()[0]:
            env.say("[3/7] Sudo rule: checking the installed rule is exactly the expected one:")
            env.say(f"  $ {show(['/usr/bin/cmp', '-s', str(src), SUDOERS_PATH])}")
            installed = env.sudo(["/usr/bin/cmp", "-s", str(src), SUDOERS_PATH]) == 0
        if installed:
            env.say(f"[3/7] Sudo rule: already installed ({SUDOERS_PATH})")
        else:
            env.say("[3/7] Sudo rule (exact commands only: lets this account start a build and nothing else):")
            if env.sudo_rule_ok(policy):
                env.say("  This account already has broader sudo rights; Borrowed Hands still installs its own exact "
                        "rule so builds keep working if those change.")
            for line in text.splitlines():
                env.say(f"    {line}")
            _admin(env, ["/usr/bin/install", "-o", "root", "-g", "wheel", "-m", "0440", str(src), SUDOERS_STAGING],
                   "copying it next to the sudo rules (inactive until moved into place)")
            _admin(env, ["/usr/sbin/visudo", "-cf", SUDOERS_STAGING], "checking it with visudo")
            _admin(env, ["/bin/mv", "-f", SUDOERS_STAGING, SUDOERS_PATH], "activating it")
            ok, why = env.sudo_rule_file()
            if not ok:
                raise SetupFailed(f"{selftest.SUDO_FIX} ({why})", partial=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    # (b) sudo itself confirms each exact command. If not, a rule elsewhere overrides ours; never edit it.
    refused = env.sudo_rule_permitted(policy)
    if refused is not None:
        raise SetupFailed(overridden_message(refused), partial=not installed)


def overridden_message(command: str) -> str:
    return (f"the exact Borrowed Hands sudo rule is installed, but another sudo rule on this Mac overrides it: sudo "
            f"does not confirm it may run '{command}'. Find the later rule in /etc/sudoers or /etc/sudoers.d "
            f"that matches /opt/borrowed-hands/bin/bh-dispatch and change it; Borrowed Hands does not edit other "
            f"sudo rules")


def step_pair(env: Env) -> None:
    keyfile.ensure_private_dir(env.home)
    cfg = config.load(env.home)
    if cfg.server is not None and keyfile.has_device_key(env.home):
        env.say(f"[4/7] Pairing: already paired as '{cfg.device_name}' (to re-pair: bh unpair, then bh setup)")
        return
    env.say("[4/7] Pairing. In your DM with Hermes, send /hands pair now (the string is valid for 10 minutes). "
            "Paste only the part that starts with bh1:, without 'bh pair' or the quotes.")
    s = env.ask("Paste the pairing string: ").strip()
    if not s:
        raise SetupFailed("no pairing string given; run 'bh setup' again when you have it", 2)
    rc = asyncio.run(account.pair(env.home, s, insecure_local=env.pair_insecure_local, connect=env.connect,
                                  timing=env.timing, host=env.host, out=env.say))
    if rc != 0:
        raise SetupFailed("pairing did not complete", rc)


def ask_workspace(env: Env) -> Workspace:
    for _ in range(3):
        wid = env.ask("  Short name for this project (lowercase, e.g. demo): ").strip()
        repo = env.ask("  Public https URL of its git repo: ").strip()
        project = env.ask("  Project inside the repo (e.g. App.xcodeproj or App.xcworkspace): ").strip()
        schemes = [s.strip() for s in env.ask("  Scheme(s) Hermes may build, comma-separated: ").split(",") if s.strip()]
        configs = [s.strip() for s in env.ask("  Configuration(s) [Debug]: ").split(",") if s.strip()] or ["Debug"]
        d = env.ask("  Destination: 1) macOS on Apple silicon  2) iOS Simulator (compile only)  3) both [1]: ").strip()
        dests = {"": [DESTINATIONS[0]], "1": [DESTINATIONS[0]], "2": [DESTINATIONS[1]],
                 "3": list(DESTINATIONS)}.get(d, [d])
        confirm = _yes(env, "  Ask on this Mac before every build?", False)
        try:
            return config.validate_workspace(Workspace(id=wid, remote_url=repo, project=project,
                                                       schemes=tuple(schemes), configurations=tuple(configs),
                                                       destinations=tuple(dests), confirm=confirm))
        except ConfigError as e:
            env.err(f"  {e}. Let's try that again.")
    raise SetupFailed("could not add the workspace; use 'bh grant add xcode.build ...' later", 2)


def step_capabilities(env: Env) -> None:
    env.say("[5/7] What Hermes may do on this Mac (everything is off until you say yes)")
    cfg = config.load(env.home)
    if _yes(env, "Let Hermes see basic facts about this Mac (macOS, Xcode version, chip, memory, free disk)?", True):
        cfg = cfg.with_capability("system.info", True)
    if _yes(env, "Let Hermes build Xcode projects you choose, in the build sandbox?", True):
        cfg = cfg.with_capability("xcode.build", True)
        if not cfg.workspaces:
            env.say("  Which project? Hermes can only build commits on bh/* branches of this repo.")
            cfg = cfg.with_workspace(ask_workspace(env))
    config.save(env.home, cfg)
    if "screen.capture" in env.get_executors():
        if _yes(env, "Let Hermes take a screenshot when you ask it to?", False):
            env.say(SCREEN_GUIDANCE.format(app="Terminal"))
            config.save(env.home, config.load(env.home).with_capability("screen.capture", True))
    else:
        env.say("  Screen capture is not available in this version; skipping.")


def step_service(env: Env) -> None:
    ok, msg = env.svc().install()
    env.say(f"[6/7] Background helper: {msg}")
    if not ok:
        raise SetupFailed(msg)


STEPS = (("[1/7] Xcode check", step_xcode), ("[2/7] build sandbox", step_sandbox), ("[3/7] sudo rule", step_sudoers),
         ("[4/7] pairing", step_pair), ("[5/7] capabilities", step_capabilities),
         ("[6/7] background helper", step_service))


def _stopped(label: str, e: SetupFailed, done: list[str]) -> str:
    """No rollback happens, so say honestly what is already in place and how to continue or undo."""
    finished = ", ".join(d.split("] ", 1)[1] for d in done) or "nothing"
    return (f"Setup stopped at {label}: {e}. Already done: {finished}. This step may be partly applied; nothing "
            f"was rolled back. Fix the problem above, then run 'bh setup' again: it skips finished steps and "
            f"continues here. To remove everything instead, run 'bh uninstall'.")


def run(env: Env) -> int:
    env.say("Borrowed Hands setup")
    done: list[str] = []
    try:
        for label, step in STEPS:
            try:
                step(env)
            except SetupFailed as e:
                if e.partial:
                    raise SetupFailed(_stopped(label, e, done), e.code) from None
                raise
            done.append(label)
    except SetupFailed as e:
        env.err(str(e))
        return e.code
    except (keyfile.UnsafeKeyStorage, ConfigError) as e:
        env.err(str(e))
        return 1
    env.say("[7/7] Self-test")
    rc = selftest.run(env)
    if rc == 0:
        ws = config.load(env.home).workspaces
        env.say(f"This Mac is ready. In your chat with Hermes, ask Hermes: \"Build my {ws[0].id} project on my Mac\"."
                if ws else "This Mac is ready. Add a project to build with 'bh grant add xcode.build ...'.")
    return rc
