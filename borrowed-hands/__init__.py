"""Borrowed Hands: borrow narrowly granted capabilities from a paired Mac. register(ctx) is wiring only (SPEC 4).

register() runs in the gateway and also in `hermes plugins validate`, doctor and CLI processes; it must return in
well under 3 s and never raise. The WSS listener starts only inside `hermes gateway run` (gate.py).
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from . import commands, config, gate, hooks, jobs, keys, listener, pairing, prompt, refs, runtime, tools
from .store import Store

log = logging.getLogger("borrowed_hands")
TOOLSET = "borrowed_hands"
SKILL = "xcode-build-pr"


def _resolver(rt: runtime.Runtime):
    """For the approval hook: the helper a call would use and, for a build, everything it left out (from cached
    caps), including an omitted commit resolved from the granted repository's bh/* branch (refs.py)."""
    cap_for = {"hands_xcode_build": "xcode.build", "hands_screen_capture": "screen.capture"}

    def resolve(tool_name, args):
        r = rt.resolve_helper(args.get("helper_id"), cap_for.get(tool_name, ""))
        h = r.get("helper")
        if h is None:
            if tool_name == "hands_xcode_build" and any(not args.get(k) for k in tools.BUILD_REQUIRED):
                code, msg, detail = r["error"]
                raise hooks.Refusal(tools.error(code, msg, **detail))
            return None, None, args
        if tool_name == "hands_xcode_build":
            args, _ = tools.fill_build(rt, h, args, resolve=refs.resolve)
        return h.helper_id, h.name, args
    return resolve


def register(ctx) -> None:
    t0 = time.monotonic()
    settings = config.load(ctx.get_config, os.environ)
    rt = runtime.Runtime(settings=settings, ctx=ctx, pid=os.getpid())
    old = runtime.RT
    try:
        rt.data_dir = Path(ctx.state.data_dir)          # resolved once, here; never from the listener thread
        rt.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(rt.data_dir, 0o700)
        rt.store = Store(rt.data_dir / "bh.sqlite3")
        rt.key = keys.load_or_create(rt.data_dir)
        rt.notifier = pairing.Notifier(rt.store, allowed=tools.USABLE_CAPABILITIES)
    except Exception as exc:
        rt.degraded = f"{type(exc).__name__}: {exc}"
        log.warning("borrowed_hands: degraded, tools will answer helper_unavailable (%s)", rt.degraded)

    for spec in tools.SPECS.values():
        ctx.register_tool(name=spec["name"], toolset=TOOLSET, schema=spec, handler=tools.HANDLERS[spec["name"]],
                          description=spec["description"])
    hooks.configure(_resolver(rt))
    ctx.register_hook("pre_tool_call", hooks.pre_tool_call)
    ctx.register_hook("post_tool_call", hooks.post_tool_call)
    ctx.register_command("hands", commands.handle, description="Borrowed Hands: pair, status, unpair",
                         args_hint="pair|status|unpair <helper>")
    ctx.register_system_prompt_section("borrowed-hands", prompt.TEXT)
    # Read-only skill with the build, edit-build-PR and new-app procedures. Optional: a host without
    # register_skill, or a failure here, must not cost the tools.
    if callable(getattr(ctx, "register_skill", None)):
        try:
            ctx.register_skill(SKILL, Path(__file__).parent / "skills" / SKILL / "SKILL.md",
                               description="Build on a paired Mac with Borrowed Hands: plain build, "
                                           "edit-build-fix-PR, new app.")
        except Exception as exc:
            log.warning("borrowed_hands: skill %s not registered (%s: %s)", SKILL, type(exc).__name__, exc)

    ok, rt.gate_reason = gate.should_listen(settings.listen_enabled, sys.argv, sys.modules)
    if not rt.degraded:
        if ok and old is not None and old.pid == os.getpid() and old.listener is not None and old.listener.running:
            # Defensive: reload-plugins calls on_unload first, so this should not happen; never run two.
            log.warning("borrowed_hands: a listener is already running in this process; not starting another")
            ok, rt.gate_reason = False, "another listener already runs in this process"
        lst = None
        if ok:
            lst = listener.Listener(settings, rt.store, rt.key, rt.notifier)
            rt.listener = lst
        rt.jobs = jobs.JobService(rt.store, lst, listener_error=None if ok else rt.gate_reason)
        if lst is not None:
            lst.jobs = rt.jobs
            rt.jobs.on_startup()
            lst.start(wait_s=max(0.5, 2.5 - (time.monotonic() - t0)))
    runtime.RT = rt

    def _unload():
        if rt.notifier is not None:
            rt.notifier.close()
        if rt.listener is not None:
            rt.listener.stop()
        if runtime.RT is rt:
            runtime.RT = None
        hooks.TICKETS.clear()
    ctx.on_unload(_unload)
    log.info("borrowed_hands: registered (v0.2.0, listener %s) in %.0f ms",
             "up" if rt.listener is not None and rt.listener.running else f"off: {rt.gate_reason}",
             (time.monotonic() - t0) * 1000)
