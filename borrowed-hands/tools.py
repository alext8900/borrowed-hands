"""The five hands_* tools (SPEC 5). Static and always registered, whether or not the listener runs: availability
is reported in results (helper_unavailable), never by adding or removing tools (runtime registration let Hermes fuzzy-repair a stale call to a sibling tool).

Arguments are parsed strictly at this boundary against the tool's own JSON schema (unknown keys rejected).
hands_find reads registered state without submitting a job or consuming approval. The action handlers use jobs.submit_and_wait(...) and hooks.consume_ticket(...).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict

from . import bhproto, hooks, match, refs, runtime, schema

log = logging.getLogger("borrowed_hands")

UUID4 = "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
HELPER = {"type": "string", "maxLength": 64, "description": "helper_id or exact name; omit when exactly one Mac fits"}

HANDS_FIND = {
    "name": "hands_find",
    "description": "Find a paired Mac that has what you need (for example Xcode) before calling other hands_* "
                   "tools. Read-only; no approval needed.",
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "requires": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 64},
                     "description": "All must hold. Forms: capability names (xcode.build, screen.capture, "
                                    "system.info), os=macos, arch=arm64, os_version>=15.0, xcode>=16, "
                                    "destination=platform=macOS,arch=arm64 or "
                                    "destination=generic/platform=iOS Simulator."},
        "include_offline": {"type": "boolean", "default": False}}}}

HANDS_SYSTEM_INFO = {
    "name": "hands_system_info",
    "description": "OS version, architecture, CPU, memory, Xcode version and free disk of a paired Mac. "
                   "Read-only; no approval needed.",
    "parameters": {"type": "object", "additionalProperties": False, "properties": {"helper_id": HELPER}}}

HANDS_XCODE_BUILD = {
    "name": "hands_xcode_build",
    "description": "Build the user's Xcode project on a paired Mac. Every argument is optional: pass only what "
                   "the user said (\"build my demo project\" needs none). Omitted values come from the Mac's "
                   "grant when it allows exactly one choice, and an omitted commit is the latest commit on the "
                   "repository's bh/* branch. Never ask the user for a commit SHA. If something is ambiguous the "
                   "result has reason needs_input with options: ask the user, then call again with that field. "
                   "The Mac only builds what its owner granted. Waits up to wait_seconds, then returns a job_id "
                   "for hands_job.",
    "parameters": {"type": "object", "additionalProperties": False,
                   "properties": {
                       "helper_id": HELPER,
                       "workspace_id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$",
                                        "description": "Granted workspace (see hands_find); omit when the Mac "
                                                       "grants one"},
                       "repo_url": {"type": "string", "maxLength": 300,
                                    "description": "https URL of the granted repository; omit to reuse the one "
                                                   "remembered for this workspace. Checked against the Mac's own "
                                                   "remote, never used to widen a grant"},
                       "commit": {"type": "string", "pattern": "^[0-9a-f]{40}$",
                                  "description": "Exact 40-hex SHA, only if the user gave one. Do not ask the "
                                                 "user for it: omit it to build the latest commit on bh/*"},
                       "branch": {"type": "string", "maxLength": 200, "pattern": refs.BRANCH_PATTERN,
                                  "description": "bh/<name> branch to build the latest commit of, only when the "
                                                 "user named one or the result asked for it"},
                       "scheme": {"type": "string", "maxLength": 64,
                                  "description": "Omit when the grant has one scheme"},
                       "configuration": {"type": "string", "maxLength": 64,
                                         "description": "Omit for the grant's only configuration, or Debug"},
                       "destination": {"type": "string", "maxLength": 200,
                                       "description": "One of the workspace's granted destinations; omit when "
                                                      "the grant has one"},
                       "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 300, "default": 240}}}}

HANDS_JOB = {
    "name": "hands_job",
    "description": "Status, wait or cancel for a Borrowed Hands job_id. Cancel only removes work.",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["job_id"], "properties": {
        "job_id": {"type": "string", "pattern": UUID4},
        "action": {"enum": ["status", "wait", "cancel"], "default": "status"},
        "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 300, "default": 120}}}}

HANDS_SCREEN_CAPTURE = {
    "name": "hands_screen_capture",
    "description": "Take a screenshot of a paired Mac's display, if its owner enabled screen capture. "
                   "Needs approval.",
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "helper_id": HELPER,
        "display": {"type": "integer", "minimum": 1, "maximum": 16, "default": 1},
        "max_px": {"type": "integer", "minimum": 256, "maximum": 2000, "default": 1600}}}}

SYSTEM_INFO_WAIT_S = 30


def error(code: str, message: str, **detail: Any) -> Dict[str, Any]:
    e: Dict[str, Any] = {"code": code, "message": message}
    if detail:
        e["detail"] = detail
    return {"ok": False, "error": e}


def finish(d: Dict[str, Any]) -> str:
    """JSON result, with queued owner notices prepended (SPEC 5)."""
    rt = runtime.RT
    if rt is not None and rt.store is not None:
        try:
            notes = rt.store.take_notices(now=int(time.time() * 1000))
            if notes:
                d = {"notices": notes, **d}
        except Exception:
            pass
    return json.dumps(d, ensure_ascii=False)


def parse(spec: dict, args: Any) -> Dict[str, Any] | None:
    """None if `args` match the tool schema, else an invalid_arguments result (no values echoed)."""
    problems = schema.check(spec["parameters"], args if isinstance(args, dict) else None) \
        if isinstance(args, dict) else ["$: expected object"]
    if problems:
        return error("invalid_arguments", "Arguments do not match the tool schema: " + "; ".join(problems[:5]))
    return None


def _ready() -> Dict[str, Any] | None:
    rt = runtime.RT
    if rt is None or rt.degraded or rt.jobs is None:
        return error("helper_unavailable", "Borrowed Hands is not set up on this Hermes. The owner can check "
                     "/hands status.", reason="plugin_degraded")
    return rt.jobs.listener_unavailable()


def _stub(name: str):
    def handler(args, session_id="", **_):
        bad = parse(SPECS[name], args)
        if bad is not None:
            return finish(bad)
        return finish(_ready() or error("helper_unavailable", f"{name} is not available in this build yet.",
                                        reason="not_available"))
    handler.__name__ = name
    handler.stub = True
    return handler


def _find_snapshot(h, online: bool, build_requested: bool, now: int):
    """Project only discovery fields; never return a stored helper row or raw caps."""
    caps = dict(h.caps or {})
    capabilities = []
    for cap in caps.get("capabilities", []):
        if cap["name"] == "xcode.build":
            cap = {**cap, "workspaces": [ws for ws in cap["workspaces"]
                                        if ws["expires_at"] is None or ws["expires_at"] > now]}
        capabilities.append(cap)
    caps["capabilities"] = capabilities
    xcode = next((cap for cap in capabilities if cap["name"] == "xcode.build"), None)
    row = {"helper_id": h.helper_id, "name": caps.get("name", h.name), "online": online,
           "busy": caps.get("busy", False), "queue_depth": caps.get("queue_depth", 0),
           "os": caps.get("os"), "os_version": caps.get("os_version"), "arch": caps.get("arch"),
           "xcode": xcode["xcode"] if xcode else None,
           "capabilities": [cap["name"] for cap in capabilities],
           "workspaces": [{key: ws[key] for key in ("workspace_id", "repo_fingerprint", "schemes",
                                                    "configurations", "destinations")}
                          for ws in xcode["workspaces"]] if xcode else []}
    if not online:
        row["status"] = "offline"
    elif h.caps is None:
        row["status"] = "caps_pending"
    elif not capabilities:
        row["status"] = "needs_grant"
        row["hint"] = (f"Nothing is enabled on {row['name']}; pairing grants nothing. On the Mac run "
                       "`bh caps enable xcode.build` and `bh grant add ...`.")
    elif build_requested and xcode and not row["workspaces"]:
        row["status"] = "needs_grant"
        row["hint"] = ("No unexpired Xcode workspace grant is advertised. The owner can run "
                       "`bh grant add ...` on the Mac.")
    elif build_requested and xcode and row["queue_depth"] >= 3:
        row["status"], row["reason"] = "helper_busy", "queue_full"
    elif build_requested and xcode and (row["busy"] or row["queue_depth"]):
        row["status"] = "busy"
    else:
        row["status"] = "ready"
    return row, caps


def hands_find(args, session_id="", **_):
    # Requirement errors identify the offending string, including the schema's length/count bounds.
    try:
        reqs = match.parse(args.get("requires", [])) if isinstance(args, dict) else []
    except match.InvalidRequirement as exc:
        return finish(error("invalid_arguments", "Invalid hands_find requirement.", requirement=exc.text))
    bad = parse(HANDS_FIND, args)
    if bad is not None:
        return finish(bad)
    down = _ready()
    if down is not None:
        # The shared listener diagnostic may contain paths; discovery returns only its stable reason.
        down["error"].get("detail", {}).pop("error", None)
        return finish(down)
    rt = runtime.RT
    paired = [h for h in rt.store.list_helpers() if h.revoked_at is None]
    result = {"ok": True, "helpers": [], "unmatched": []}
    if not paired:
        result["hint"] = "No Mac is paired. The owner can run /hands pair."
        return finish(result)
    build_requested = not reqs or any(req.key in ("xcode", "destination") or
                                     (req.key == "capability" and req.value == "xcode.build") for req in reqs)
    now = int(time.time() * 1000)
    hidden_offline = 0
    for h in paired:
        online = rt.online(h.helper_id)
        if not online and not args.get("include_offline", False):
            hidden_offline += 1
            continue
        row, caps = _find_snapshot(h, online, build_requested, now)
        unmet = match.evaluate(reqs, caps)
        if unmet:
            miss = {key: row[key] for key in ("helper_id", "name", "online", "status")}
            if miss["status"] == "ready":
                miss["status"] = "unmatched"  # online and usable, but lacks what was asked for
            miss["unmet"] = unmet
            if "hint" in row:
                miss["hint"] = row["hint"]
            if "reason" in row:
                miss["reason"] = row["reason"]
            result["unmatched"].append(miss)
        else:
            result["helpers"].append(row)
    # Stable ranking: connected and usable first, then queue availability, then name/id as tie breakers.
    order = {"ready": 0, "busy": 1, "helper_busy": 2, "needs_grant": 3, "caps_pending": 4, "offline": 5}
    result["helpers"].sort(key=lambda row: (order[row["status"]], row["queue_depth"],
                                            row["name"].casefold(), row["helper_id"]))
    result["unmatched"].sort(key=lambda row: (not row["online"], len(row["unmet"]),
                                              row["name"].casefold(), row["helper_id"]))
    if not result["helpers"]:
        if result["unmatched"]:
            result["hint"] = "No Mac matches these requirements; see unmatched for reasons."
            if hidden_offline:
                result["hint"] += (f" {hidden_offline} paired Mac{' is' if hidden_offline == 1 else 's are'} "
                                   "offline; set include_offline=true to see them.")
        else:
            result["hint"] = "Paired Macs are offline. Retry when connected or set include_offline=true."
    return finish(result)


def hands_system_info(args, session_id="", **_):
    bad = parse(HANDS_SYSTEM_INFO, args)
    if bad is not None:
        return finish(bad)
    down = _ready()
    if down is not None:
        return finish(down)
    rt = runtime.RT
    r = rt.resolve_helper(args.get("helper_id"), "system.info")
    if "error" in r:
        code, msg, detail = r["error"]
        return finish(error(code, msg, **detail))
    h = r["helper"]
    v = rt.jobs.submit_and_wait(h.helper_id, "system.info", {}, SYSTEM_INFO_WAIT_S, "none", session_id or "")
    if v.get("state") == "succeeded" and "result" in v:
        return finish({"ok": True, "helper_id": h.helper_id, "name": h.name, "job_id": v["job_id"],
                       "note": v["note"], "info": v["result"]})
    if v.get("ok") and v.get("state") not in ("failed", "cancelled", "expired"):
        v["message"] = v.get("message") or "The Mac has not answered yet; call hands_job with action=wait."
    return finish(v)


BUILD_REQUIRED = ("workspace_id", "repo_url", "commit", "scheme")   # what a fully explicit call names


def _xcode_grants(h) -> list:
    return [w for c in (h.caps or {}).get("capabilities", []) if c.get("name") == "xcode.build"
            for w in c.get("workspaces", []) or []]


def _needs_input(field: str, options, message: str) -> hooks.Refusal:
    """`options` stay exact (they are what the caller sends back); the message shows each one as a label that a
    Mac-advertised string cannot turn into instructions: known destinations and plain names bare, anything else
    quoted, shortened and stripped of control characters and markup (hooks.label)."""
    exact = [str(o) for o in options][:32]
    labels = ", ".join(hooks.label(o) for o in exact)
    return hooks.Refusal(error("invalid_arguments", f"{message} Ask the user which one, then call again with "
                               f"{field} set: {labels}.", reason="needs_input", field=field, options=exact))


def fill_build(rt, h, args: Dict[str, Any], *, resolve=None, approved=None):
    """Fill what a build request left out, from `h`'s advertised grant (and the repository URL remembered for
    it), then the commit: `approved` (sha, how) from the approval ticket, else `resolve(repo_url, branch)`
    (refs.resolve, network) when given, else left out. Explicit values are never replaced or checked here: the
    Mac enforces its grants. Returns (args with defaults, whether anything was filled). Raises hooks.Refusal
    with a user-readable result when a missing value cannot be filled."""
    a, filled = dict(args), False
    grants = _xcode_grants(h)
    if not a.get("workspace_id"):
        now = int(time.time() * 1000)
        ids = sorted({w.get("workspace_id") for w in grants
                      if w.get("expires_at") is None or w["expires_at"] > now})
        if not ids:
            raise hooks.Refusal(error("needs_grant", f"{h.name} has not granted any Xcode workspace. Its owner "
                                      "can run `bh grant add ...` on the Mac.", reason="no_workspace"))
        if len(ids) > 1:
            raise _needs_input("workspace_id", ids, f"{h.name} grants {len(ids)} workspaces.")
        a["workspace_id"], filled = ids[0], True
    ws_id = a["workspace_id"]
    ws = next((w for w in grants if w.get("workspace_id") == ws_id), None)
    if ws is None:
        if any(not a.get(k) for k in BUILD_REQUIRED):
            raise hooks.Refusal(error("needs_grant", "The Mac has not granted this workspace.", workspace_id=ws_id))
        a.setdefault("configuration", "Debug")
        return a, filled                                   # explicit call: the handler and the Mac decide

    def pick(field, values, label, fallback=None):
        nonlocal filled
        if a.get(field):
            return
        vals = list(dict.fromkeys(values or []))
        if len(vals) == 1:
            a[field] = vals[0]
        elif fallback is not None and (fallback in vals or not vals):
            a[field] = fallback
        elif not vals:
            raise hooks.Refusal(error("needs_grant", f"{h.name} grants no {label} for workspace {ws_id}.",
                                      workspace_id=ws_id))
        else:
            raise _needs_input(field, vals, f"{h.name} grants {len(vals)} {label}s for workspace {ws_id}.")
        filled = True

    pick("scheme", ws.get("schemes"), "scheme")
    pick("configuration", ws.get("configurations"), "configuration", fallback="Debug")
    pick("destination", ws.get("destinations"), "destination")
    fingerprint = ws.get("repo_fingerprint")
    if not a.get("repo_url"):
        store = getattr(rt, "store", None)
        url = store.repo_url_for(fingerprint) if store is not None and fingerprint else None
        if not url:
            raise hooks.Refusal(error(
                "invalid_arguments", f"Which repository is workspace {ws_id}? The Mac shares only a fingerprint "
                "of it, so ask the user once for its public https URL (for example "
                "https://github.com/<owner>/<repo>.git) and call again with repo_url. It is remembered after that.",
                reason="needs_input", field="repo_url", options=[]))
        a["repo_url"], filled = url, True
    if not a.get("commit"):
        branch = a.get("branch")
        if branch is not None and not refs.valid_branch(branch):
            raise hooks.Refusal(error("invalid_arguments", "branch must be bh/<name>, a valid git branch name.",
                                      reason="bad_branch"))
        try:
            granted = bhproto.repo_fingerprint(a["repo_url"]) == fingerprint
        except Exception:
            granted = False
        if not granted:   # never read the branches of a repository the Mac did not grant
            raise hooks.Refusal(error(
                "needs_resolution", f"That repository is not the one {h.name} granted for workspace {ws_id}, so "
                "its branches were not read and nothing was built. Omit repo_url to use the granted repository.",
                reason="repo_mismatch"))
        if approved is not None:
            a["commit"], a["commit_source"] = approved
        elif resolve is not None:
            try:
                sha, _branch, how = resolve(a["repo_url"], branch)
            except refs.RefsError as exc:
                raise hooks.Refusal(error(exc.code, exc.message, reason=exc.reason, **exc.detail))
            a["commit"], a["commit_source"] = sha, how
        filled = True
    return a, filled


def hands_xcode_build(args, session_id="", tool_call_id="", **_):
    started = time.monotonic()
    # Take this call's ticket first, so a call that fails below never leaves one behind to block the next.
    # Only this invocation's own ticket is taken; without an invocation id there is none (fail closed).
    ticket = hooks.take(session_id, "hands_xcode_build", args, tool_call_id) if isinstance(args, dict) else None
    bad = parse(HANDS_XCODE_BUILD, args)
    if bad is not None:
        return finish(bad)
    down = _ready()
    if down is not None:
        return finish(down)
    rt = runtime.RT
    if ticket is not None and ticket.refusal is not None:
        return finish(ticket.refusal)              # the hook could not resolve this call; nothing was approved
    resolved = rt.resolve_helper(args.get("helper_id"), "xcode.build")
    if "error" in resolved:
        code, msg, detail = resolved["error"]
        return finish(error(code, msg, **detail))
    h = resolved["helper"]
    approved = None
    if ticket is not None and not args.get("commit") and ticket.scope.get("commit"):
        approved = (ticket.scope["commit"], ticket.commit_source)  # build what was approved; never re-read refs
    try:
        a, filled = fill_build(rt, h, args, approved=approved)
    except hooks.Refusal as r:
        return finish(r.result)
    workspaces = [w for w in _xcode_grants(h) if w.get("workspace_id") == a["workspace_id"]]
    if not workspaces or not workspaces[0].get("destinations"):
        return finish(error("needs_grant", "The Mac has not granted this workspace.", workspace_id=a["workspace_id"]))
    scope = {k: v for k, v in hooks.security_fields("hands_xcode_build", a).items() if k != "helper_id"}
    ref = hooks.verify(ticket, "hands_xcode_build", args, helper_id=h.helper_id, scope=scope) \
        if a.get("commit") else None
    if ref is None:
        return finish(error("denied", "Approval was not observed for this build.", reason="approval_not_observed"))
    try:
        fingerprint = bhproto.repo_fingerprint(a["repo_url"])
    except ValueError:
        return finish(error("invalid_arguments", "Invalid repository URL."))
    params = {"workspace_id": a["workspace_id"], "repo_fingerprint": fingerprint, "commit": a["commit"],
              "scheme": a["scheme"], "configuration": a["configuration"], "destination": a["destination"]}
    store = getattr(rt, "store", None)
    if store is not None and args.get("repo_url") and fingerprint == workspaces[0].get("repo_fingerprint"):
        store.remember_repo_url(fingerprint, args["repo_url"])   # so the next request can omit it
    deadline = started + args.get("wait_seconds", 240)
    remaining = max(0.0, deadline - time.monotonic())
    result = rt.jobs.submit_and_wait(h.helper_id, "xcode.build", params, remaining, ref, session_id or "",
                                     deadline=deadline)
    if filled:
        result["resolved"] = {"workspace_id": a["workspace_id"], "scheme": a["scheme"],
                              "configuration": a["configuration"], "destination": a["destination"],
                              "commit": a["commit"], "commit_source": a.get("commit_source")}
    if result.get("ok") and result.get("state") not in ("succeeded", "failed", "cancelled", "expired"):
        result["message"] = "Still building; call hands_job with action=wait."
    return finish(result)


def hands_job(args, session_id="", **_):
    bad = parse(HANDS_JOB, args)
    if bad is not None:
        return finish(bad)
    rt = runtime.RT
    if rt is None or rt.jobs is None:
        return finish(_ready())
    action = args.get("action", "status")
    if action == "status":
        return finish(rt.jobs.get(args["job_id"]))
    if action == "wait":
        return finish(rt.jobs.wait(args["job_id"], args.get("wait_seconds", 120)))
    return finish(rt.jobs.cancel(args["job_id"]))


SPECS = {s["name"]: s for s in (HANDS_FIND, HANDS_SYSTEM_INFO, HANDS_XCODE_BUILD, HANDS_JOB, HANDS_SCREEN_CAPTURE)}
HANDLERS = {
    "hands_find": hands_find,
    "hands_system_info": hands_system_info,
    "hands_xcode_build": hands_xcode_build,
    "hands_job": hands_job,
    "hands_screen_capture": _stub("hands_screen_capture"),
}
# Capabilities this build can execute: the capability's hands_* tool has a real handler, not a _stub. Owner
# readiness replies list only these, so an advertised but unexecutable capability is never called usable.
USABLE_CAPABILITIES = tuple(cap for cap in match.CAPABILITIES
                            if (h := HANDLERS.get("hands_" + cap.replace(".", "_")))
                            and not getattr(h, "stub", False))
