"""/hands pair | status | unpair <helper> | rotate-key | help (SPEC 8.2).

Owner gate: the local CLI (source None) or `platform:user_id` in settings.owners. `pair` also needs a DM, because
the pairing string is a secret for 10 minutes.
"""
from __future__ import annotations

import contextvars
import logging
import time
from typing import Any, Optional

from . import pairing, runtime

log = logging.getLogger("borrowed_hands")

USAGE = ("Borrowed Hands: `/hands pair` (in a DM) gives a one-time pairing string for your Mac; `/hands status` "
         "shows what is connected; `/hands unpair <name or id>` removes a Mac; `/hands rotate-key` is not "
         "available in 0.1.")
NOT_OWNER = "Only the Borrowed Hands owner can do that."


def _now() -> int:
    return int(time.time() * 1000)


def _platform(source) -> Optional[str]:
    p = getattr(source, "platform", None)
    return getattr(p, "value", p)


def handle(raw_args: str, source: Any = None, **_) -> Optional[str]:
    rt = runtime.RT
    parts = (raw_args or "").split()
    sub = parts[0].lower() if parts else "help"
    if sub in ("help", "-h", "--help"):
        return USAGE
    if rt is None:
        return "Borrowed Hands did not load; check the gateway log for borrowed_hands lines."
    if source is not None and not rt.settings.is_owner(_platform(source), getattr(source, "user_id", None)):
        return NOT_OWNER
    if sub == "pair":
        return _pair(rt, source)
    if sub == "status":
        return status_text(rt)
    if sub == "unpair":
        return _unpair(rt, " ".join(parts[1:]).strip())
    if sub == "rotate-key":
        return ("Server key rotation is not available in 0.1. If you think the server key leaked: run `bh unpair` "
                "on every Mac first, then stop the gateway, rename this plugin's directory under "
                "$HERMES_HOME/plugin-data (that keeps the audit log), start the gateway, and pair each Mac again "
                "with /hands pair and `bh setup`.")
    return USAGE


def _pair(rt, source) -> Optional[str]:
    if source is not None and getattr(source, "chat_type", None) != "dm":
        return "Run /hands pair in a direct message with me: the pairing string is a secret for 10 minutes."
    if rt.degraded or rt.store is None:
        return f"Borrowed Hands cannot pair: {rt.degraded or 'storage unavailable'}."
    if not rt.settings.public_url:
        return ("Set public_url (or BH_PUBLIC_URL) to this server's wss://<host>/bh/v1/ws address and restart, "
                "then run /hands pair again.")
    issued_to = f"{_platform(source)}:{source.user_id}" if source is not None else "local:cli"
    handle, post = None, None
    if rt.ctx is not None and source is not None:
        captured = contextvars.copy_context()
        ctx = rt.ctx
        post = lambda text: captured.copy().run(ctx.post_message, text)
    s, code_hash = pairing.new_code(rt.store, rt.settings, rt.listener.pub if rt.listener else _pub(rt), issued_to,
                                    _now())
    # Capture the route before publishing the secret. A helper may use it before the post returns its handle.
    rt.notifier.remember(code_hash, None, post, posting=post is not None)
    log.info("borrowed_hands: pairing code issued to %s", issued_to)
    text = (f"Pair your Mac: run this on it (valid 10 minutes, single use):\n```\nbh pair '{s}'\n```")
    if rt.listener is None or not rt.listener.running:
        text += "\nNote: the listener is not running, so the Mac cannot connect yet; see /hands status."
    if post is not None:
        try:
            handle = rt.ctx.post_message(text)
        except Exception:
            handle = None
        finally:
            rt.notifier.attach_handle(code_hash, handle)
    if handle is not None and getattr(handle, "ok", False):
        return None
    return text


def _pub(rt):
    from . import bhproto
    return bhproto.pub_raw(rt.key)


def _unpair(rt, ref: str) -> str:
    if not ref:
        return "Usage: /hands unpair <name or helper_id>"
    if rt.store is None:
        return "Borrowed Hands storage is unavailable."
    h = rt.store.find_helper(ref)
    if h is None:
        return f"No paired Mac matches {ref!r}. /hands status lists them."
    if rt.listener is not None and rt.listener.running:
        rt.listener.call(rt.listener.unpair(h.helper_id, "unpaired by the owner"), timeout=10)
    else:
        rt.listener.revoke(h.helper_id, "unpaired by the owner") if rt.listener else rt.store.revoke_helper(
            h.helper_id, now=_now())
    return f"Unpaired {h.name} ({h.helper_id[:8]}). It can never connect again with its current key."


def _ago(ms: Optional[int]) -> str:
    if not ms:
        return "never"
    s = max(0, (_now() - ms) // 1000)
    return f"{s} s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago"


def status_text(rt) -> str:
    lines = []
    for p in rt.settings.problems:
        lines.append(f"Setting problem: {p}. Fix it in plugins.entries.borrowed-hands.settings and restart.")
    if rt.degraded:
        lines.append(f"Not working: {rt.degraded}. Check the plugin data dir permissions and restart the gateway.")
        return "\n".join(lines)
    lst = rt.listener
    if lst is None:
        lines.append(f"Listener: off ({rt.gate_reason}). Set listen.enabled: true (or BH_LISTEN=1) and restart "
                     "`hermes gateway run`.")
    elif not lst.running:
        lines.append(f"Listener: not running ({lst.error}). Free port {rt.settings.port} or change listen.port, "
                     "then restart the gateway.")
    else:
        lines.append(f"Listener: up on port {rt.settings.port}.")
    lines.append(f"Public URL: {rt.settings.public_url}" if rt.settings.public_url else
                 "Public URL: not set, so pairing is impossible. Set public_url or BH_PUBLIC_URL.")
    if not rt.settings.owners:
        lines.append("Owners: none configured, so nobody can pair from chat. Set owners (or BH_OWNERS) to "
                     "platform:user_id.")
    helpers = [h for h in rt.store.list_helpers()]
    if not helpers:
        lines.append("Macs: none paired. Run /hands pair in a DM, then `bh pair '<string>'` on the Mac.")
    for h in helpers:
        if h.revoked_at is not None:
            lines.append(f"- {h.name} ({h.helper_id[:8]}): unpaired; it can no longer connect.")
            continue
        online = rt.online(h.helper_id)
        caps = rt.helper_caps(h)
        state = "online" if online else f"offline (last seen {_ago(h.last_seen)}); start `bh run` or the service"
        cap_txt = ", ".join(caps) if caps else ("nothing enabled yet; on the Mac run `bh caps enable ...` and "
                                                "`bh grant add ...`")
        busy = " busy" if (h.caps or {}).get("busy") else ""
        lines.append(f"- {h.name} ({h.helper_id[:8]}): {state}{busy}; capabilities: {cap_txt}.")
    active = rt.store.jobs_nonterminal()
    if active:
        lines.append("Active jobs: " + ", ".join(f"{j.job_id[:8]} {j.action} {j.state.value}" for j in active[:10]))
    notes = rt.store.take_notices(now=_now())
    if notes:
        lines.append("Notices: " + " | ".join(notes))
    return "\n".join(lines)
