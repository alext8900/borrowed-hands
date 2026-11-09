"""pre_tool_call approval and approval tickets (SPEC 5.6).

Hermes approval is UX and an audit link, not a security boundary: the Mac's grants decide. The hook asks Hermes
for approval with a rule_key at the scope grain (helper + workspace + scheme + configuration + destination, never the
SHA) and leaves a ticket; the tool handler must pop a matching ticket or refuse with denied/approval_not_observed,
which catches callers that skip hooks (ctx.dispatch_tool, registry.dispatch). post_tool_call drops the ticket of a
call Hermes blocked, so a denied approval cannot be reused by a later hook-less call.

The hook never raises (a raise fails closed and would look like a denial). Its only I/O beyond the cached caps is
the bounded read of the granted repository's branch list when a build names no commit (refs.py). A call that cannot
be resolved (needs input, no bh/* branch, network failure) gets no approval prompt: the hook leaves a refusal
ticket and the handler returns that refusal without building anything.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import threading
import time
import unicodedata
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from . import bhproto

log = logging.getLogger("borrowed_hands")

GATED = ("hands_xcode_build", "hands_screen_capture")
TICKET_TTL_S = 900
REFUSAL_TTL_S = 60        # a refusal is read back by the handler at once; it must not hold store room for long
MAX_TOMBSTONES = 1024
PENDING_MARK_TTL_S = 120  # how long a "finished before its hook ran" mark waits for that late hook run
MAX_TICKETS = 256

# Resolver installed by register(): (tool_name, args) -> (helper_id or None, helper name or None, args with
# defaults filled from the helper's caps; for a build with no commit, also the resolved "commit" and how it was
# chosen as "commit_source"). It raises Refusal (carrying the tool result) when a value it must fill cannot be.
Resolver = Callable[[str, dict], Tuple[Optional[str], Optional[str], dict]]


class Refusal(Exception):
    """A build that cannot be resolved; `result` is the tool result to return. No approval, no job."""

    def __init__(self, result: Dict[str, Any]):
        super().__init__("refused")
        self.result = result


def _plain(args: dict) -> Resolver:
    return lambda tool, a: (a.get("helper_id"), None, dict(a))


_resolver: Resolver = _plain({})


def configure(resolver: Resolver) -> None:
    global _resolver
    _resolver = resolver


def _int(v: Any) -> Any:
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return v


def security_fields(tool_name: str, args: dict) -> Dict[str, Any]:
    """The arguments that define what would run, with defaults applied. Hermes coerces argument types between
    the hook and the handler, so numbers are normalized and non-security fields (wait_seconds) are excluded."""
    if tool_name == "hands_xcode_build":
        keys = ("helper_id", "workspace_id", "repo_url", "commit", "branch", "scheme", "configuration",
                "destination")
        out = {k: args.get(k) for k in keys}
        if out["configuration"] is None:
            out["configuration"] = "Debug"
        return out
    out = {"helper_id": args.get("helper_id"), "display": _int(args.get("display", 1)),
           "max_px": _int(args.get("max_px", 1600))}
    return out


def args_hash(tool_name: str, fields: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([tool_name, fields], sort_keys=True, default=str).encode()).hexdigest()


def rule_key(tool_name: str, fields: Dict[str, Any]) -> str:
    """`<tool>:<hash>` over the canonical JSON of the whole approval scope, so no two scopes can collide through a
    delimiter inside a value (a scheme "A:B" with configuration "C" vs scheme "A" with "B:C")."""
    if tool_name == "hands_xcode_build":
        scope = {k: fields.get(k) for k in ("workspace_id", "scheme", "configuration", "destination")}
    else:
        scope = {"display": fields.get("display")}
    scope.update(tool=tool_name, helper_id=fields.get("helper_id") or "auto")
    return f"{tool_name}:{hashlib.sha256(bhproto.canonical(scope)).hexdigest()[:32]}"


# The approval reason is built partly from model-supplied values. Only short single-token values in the grant
# character set are shown bare; anything else is shown quoted, as one visibly separate value: control, format and
# separator characters (newlines, bidi overrides) and markdown/HTML punctuation removed, length capped. The
# executed values are unaffected (the contract carries the exact strings and the Mac enforces its grant).
_PLAIN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._+-]{0,31}")
_WORKSPACE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_HEX40 = re.compile(r"[0-9a-f]{40}")
_SOURCE = re.compile(r"latest on bh/[A-Za-z0-9._/-]{1,60}")
_MARKUP = set('`*_[]<>|#~"()' + chr(92))
SHOWN_MAX = 40


_MAC_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9 ._+-]{0,63}")   # the caps/pair schema's name pattern
_ID_PREFIX = re.compile(r"[0-9a-f-]{1,8}")


def _small_int(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 <= value < 100000
    return isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 5


def label(value: Any) -> str:
    """A value shown to a person: a known destination or a plain name bare, anything else via _shown."""
    if isinstance(value, str) and (value in DESTINATION_LABELS or _plain(value)):
        return value
    return _shown("value", value)


def _plain(value: Any) -> bool:
    return isinstance(value, str) and bool(_PLAIN.fullmatch(value))


def _shown(field: str, value: Any) -> str:
    # line and paragraph breaks become spaces (so words stay apart); other control, format and separator
    # characters (bidi overrides, zero-width) are dropped
    text = "".join(" " if c in "\n\r\t\v\f\u2028\u2029\x85" else c for c in str(value))
    text = "".join(c for c in text if unicodedata.category(c)[0] not in "CZ" or c == " ")
    text = " ".join("".join(c for c in text if c not in _MARKUP).split())
    if len(text) > SHOWN_MAX:
        text = text[:SHOWN_MAX] + "..."
    return f'"{text}"' if text else f'"(empty {field})"'


DESTINATION_LABELS = {"platform=macOS,arch=arm64": "macOS arm64", "generic/platform=iOS Simulator": "iOS Simulator"}


def describe(tool_name: str, fields: Dict[str, Any], helper_name: Optional[str],
             commit_source: Optional[str] = None) -> str:
    """The approval reason a person reads, e.g. "Build demo (scheme Demo, Debug, macOS arm64) at commit 3449717
    (latest on bh/fixed) on Macbook Pro M5 Max". Hermes shows it beside its own fixed plugin-rule label."""
    helper_ref = fields.get("helper_id")
    if helper_name:
        where = f" on {helper_name}" if isinstance(helper_name, str) and _MAC_NAME.fullmatch(helper_name) else \
            f" on Mac {_shown('Mac name', helper_name)}"
    elif helper_ref:
        prefix = str(helper_ref)[:8]
        where = f" on Mac {prefix}" if _ID_PREFIX.fullmatch(prefix) else f" on Mac {_shown('Mac', helper_ref)}"
    else:
        where = ""
    if tool_name == "hands_xcode_build":
        dest = fields.get("destination")
        dest = DESTINATION_LABELS.get(dest) if isinstance(dest, str) else None
        if dest is None:
            dest = f"destination {_shown('destination', fields.get('destination'))}" \
                if fields.get("destination") else "default destination"
        commit = fields.get("commit")
        if isinstance(commit, str) and _HEX40.fullmatch(commit):
            at = f"at commit {commit[:7]}"
        else:
            at = f"at commit {_shown('commit', commit)}" if commit else "at an unresolved commit"
        how = f" ({commit_source})" if isinstance(commit_source, str) and _SOURCE.fullmatch(commit_source) else ""
        workspace = fields.get("workspace_id")
        if not (isinstance(workspace, str) and _WORKSPACE.fullmatch(workspace)):
            workspace = f"workspace {_shown('workspace', workspace)}"
        scheme, configuration = fields.get("scheme"), fields.get("configuration")
        scheme = f"scheme {scheme}" if _plain(scheme) else f"scheme {_shown('scheme', scheme)}"
        if not _plain(configuration):
            configuration = f"configuration {_shown('configuration', configuration)}"
        return f"Build {workspace} ({scheme}, {configuration}, {dest}) {at}{how}{where}"
    display, max_px = fields.get("display"), fields.get("max_px")
    display = display if _small_int(display) else _shown("display", display)
    max_px = max_px if _small_int(max_px) else _shown("size", max_px)
    return f"Capture display {display} (max {max_px} px){where}"


@dataclass
class Ticket:
    ticket_id: str
    helper_id: Optional[str]
    tool_call_id: str
    at: float
    scope: Dict[str, Any]          # the resolved scope Hermes showed and approved
    commit_source: Optional[str] = None       # how an omitted commit was chosen ("latest on bh/fixed")
    refusal: Optional[Dict[str, Any]] = None  # the hook could not resolve the call: return this, never build


class TicketStoreFull(Exception):
    """MAX_TICKETS live tickets are held: a new call is refused rather than evicting someone's live ticket."""


class Tickets:
    """Approval tickets, one per (session, tool, call arguments), each owned by the tool_call_id that created it.

    Only expiry (TICKET_TTL_S) removes a ticket nobody asked for. Reads, failed takes and conflict checks never
    evict. At capacity a new key is refused (TicketStoreFull) instead of evicting the oldest live ticket: eviction
    would silently turn a call the user may be approving right now into a denial, while a refused newcomer gets a
    clear, retryable answer and cannot be used to flush another call's ticket."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._t: Dict[Tuple[str, str, str], Ticket] = {}
        # Hermes abandons a hook worker at plugins.hook_callback_timeout, blocks the call and runs post_tool_call,
        # but the worker can still reach issue() later, at any point of its run, even before it registered. Its
        # ticket would be an orphan refusing every identical call until it expired. So each hook run registers
        # first thing and gets its own token; a post hook kills the live runs of that invocation (by token, never a
        # permanent (session, id) mark, so a provider's deterministic id reused later is unaffected), or, if no
        # run of that invocation has started or completed yet, leaves a short pending mark that kills the run
        # that registers next with the same identity. An invocation is (session, tool_call_id, turn_id).
        self._runs: Dict[object, Tuple[str, str, str]] = {}     # live run token -> invocation
        self._dead: set = set()                                  # tokens of runs Hermes already finished
        self._pending: "OrderedDict[Tuple[str, str, str], float]" = OrderedDict()
        self._done: "OrderedDict[Tuple[str, str, str], float]" = OrderedDict()

    def _expire(self, now: float) -> None:
        for k in [k for k, v in self._t.items()
                  if now - v.at > (REFUSAL_TTL_S if v.refusal is not None else TICKET_TTL_S)]:
            del self._t[k]

    @staticmethod
    def _trim(marks: "OrderedDict", now: float) -> None:
        while marks and (len(marks) > MAX_TOMBSTONES or now - next(iter(marks.values())) > PENDING_MARK_TTL_S):
            marks.popitem(last=False)

    def begin(self, session_id: str, tool_call_id: str, turn_id: str = "") -> object:
        """Register one hook run before it computes anything; returns its token."""
        token = object()
        ident = (str(session_id or ""), str(tool_call_id or ""), str(turn_id or ""))
        with self._lock:
            self._runs[token] = ident
            self._trim(self._pending, time.monotonic())
            if self._pending.pop(ident, None) is not None:
                self._dead.add(token)                    # Hermes finished this invocation before it registered
        return token

    def end(self, token: object) -> None:
        with self._lock:
            ident = self._runs.pop(token, None)
            self._dead.discard(token)
            if ident is not None:
                now = time.monotonic()
                self._done[ident] = now
                self._done.move_to_end(ident)
                self._trim(self._done, now)

    def issue(self, key: Tuple[str, str, str], helper_id: Optional[str], tool_call_id: str = "",
              scope: Optional[Dict[str, Any]] = None, commit_source: Optional[str] = None,
              refusal: Optional[Dict[str, Any]] = None, token: object = None) -> Optional[Ticket]:
        """None, and nothing stored, without an owning tool_call_id or when another invocation already holds a
        live ticket for this key: an identical call must never replace the ticket (and so the approved commit)
        of one still in flight. Raises TicketStoreFull for a new key at capacity."""
        if not tool_call_id:
            return None
        with self._lock:
            now = time.monotonic()
            self._expire(now)
            if token is not None and (token in self._dead or token not in self._runs):
                return None                              # a run Hermes already finished: never an orphan
            held = self._t.get(key)
            if held is not None and held.tool_call_id != tool_call_id:
                return None
            if held is None and len(self._t) >= MAX_TICKETS:
                raise TicketStoreFull()
            t = Ticket(str(uuid.uuid4()), helper_id, tool_call_id, now, dict(scope or {}), commit_source, refusal)
            self._t[key] = t
            return t

    def take(self, key: Tuple[str, str, str], tool_call_id: str) -> Optional[Ticket]:
        """Pop the ticket at `key` only if `tool_call_id` (non-empty) is the invocation that left it. Any other
        caller gets None and the ticket stays for its owner."""
        with self._lock:
            self._expire(time.monotonic())
            held = self._t.get(key)
            if not tool_call_id or held is None or not held.tool_call_id or held.tool_call_id != tool_call_id:
                return None
            return self._t.pop(key)

    def held_by_other(self, key: Tuple[str, str, str], tool_call_id: str) -> bool:
        with self._lock:
            self._expire(time.monotonic())
            held = self._t.get(key)
            return held is not None and held.tool_call_id != tool_call_id

    def drop_invocation(self, session_id: str, tool_call_id: str, turn_id: str = "") -> int:
        """Drop the tickets `tool_call_id` left in this session, whatever key they are under (argument middleware
        may have changed the args since the hook ran). Never another session's, never an unowned drop. Also kill
        any live hook run of this invocation, or mark it pending if no run has started or completed yet."""
        if not tool_call_id:
            return 0
        with self._lock:
            ident = (str(session_id or ""), tool_call_id, str(turn_id or ""))
            live = [tok for tok, who in self._runs.items() if who == ident]
            if live:
                self._dead.update(live)
            elif ident not in self._done:
                now = time.monotonic()
                self._pending[ident] = now
                self._pending.move_to_end(ident)
                self._trim(self._pending, now)
            gone = [k for k, v in self._t.items() if k[0] == str(session_id or "") and v.tool_call_id == tool_call_id]
            for k in gone:
                del self._t[k]
            return len(gone)

    def clear(self) -> None:
        with self._lock:
            self._t.clear()
            self._runs.clear()
            self._dead.clear()
            self._pending.clear()
            self._done.clear()


TICKETS = Tickets()


def _ticket_key(session_id: str, tool_name: str, args: dict) -> Tuple[str, str, str]:
    """Depends only on the call as made (never on which Mac is auto-selected), so the hook, the handler and
    the cleanup always compute the same key."""
    return (str(session_id or ""), tool_name, args_hash(tool_name, security_fields(tool_name, dict(args or {}))))


def _resolved(tool_name: str, args: dict) -> Tuple[Optional[str], Optional[str], Dict[str, Any], Optional[str]]:
    helper_id, name, filled = _resolver(tool_name, dict(args or {}))
    fields = security_fields(tool_name, filled)
    fields["helper_id"] = helper_id or filled.get("helper_id")  # unresolved: keep what the caller named
    source = filled.get("commit_source")
    return helper_id, name, fields, source if isinstance(source, str) else None


CONFLICT = {"action": "block", "message": "BLOCKED: an identical Borrowed Hands request in this chat is still "
            "waiting for approval. Wait for it to finish, then ask again."}
BUSY = {"action": "block", "message": "BLOCKED: too many Borrowed Hands requests are waiting for approval right now. "
        "Answer or let some finish, then ask again."}


def pre_tool_call(tool_name=None, args=None, session_id="", tool_call_id="", turn_id="", **_):
    if tool_name not in GATED or not tool_call_id:
        return _pre_tool_call(tool_name, args, session_id, tool_call_id, None)
    token = TICKETS.begin(session_id, str(tool_call_id), turn_id)   # first, so a timeout at any later point counts
    try:
        return _pre_tool_call(tool_name, args, session_id, tool_call_id, token)
    finally:
        TICKETS.end(token)


def _pre_tool_call(tool_name, args, session_id, tool_call_id, token):
    try:
        if tool_name not in GATED:
            return None
        if not tool_call_id:
            return {"action": "block", "message": "BLOCKED: Borrowed Hands needs a tool_call_id for approval; "
                    "call this tool directly through the agent instead of through code execution."}
        args = args if isinstance(args, dict) else {}
        key = _ticket_key(session_id, tool_name, args)
        call_id = str(tool_call_id)
        if TICKETS.held_by_other(key, call_id):
            return CONFLICT
        try:
            try:
                helper_id, name, fields, source = _resolved(tool_name, args)
            except Refusal as r:
                # Nothing to approve: the handler returns this result and builds nothing.
                return None if TICKETS.issue(key, None, call_id, refusal=r.result, token=token) is not None \
                    else CONFLICT
            if TICKETS.issue(key, helper_id, call_id, scope=fields, commit_source=source, token=token) is None:
                return CONFLICT
        except TicketStoreFull:
            return BUSY
        return {"action": "approve", "message": describe(tool_name, fields, name, source),
                "rule_key": rule_key(tool_name, fields)}
    except Exception as exc:  # never raise: still ask, with the coarsest safe description
        log.warning("borrowed_hands: approval hook error (%s)", type(exc).__name__)
        return {"action": "approve", "message": f"Borrowed Hands: {tool_name}", "rule_key": f"{tool_name}:{uuid.uuid4()}"}


def post_tool_call(tool_name=None, args=None, session_id="", status=None, tool_call_id="", turn_id="", **_):
    """Drop the ticket of an invocation that did not complete normally (blocked, error, cancelled, timed out), by
    session and invocation id, so it cannot hold the key against the next identical call. A call without an id
    never had a ticket (the hook blocks it before issuing one) and drops nothing: dropping by argument key would
    remove another invocation's live ticket. A completed call's handler already took its ticket."""
    try:
        if tool_name in GATED and tool_call_id and status != "ok":
            TICKETS.drop_invocation(str(session_id or ""), str(tool_call_id), str(turn_id or ""))
    except Exception:
        pass
    return None


def invocation_id(explicit: Any = None) -> str:
    """The tool_call_id of the call being handled: `explicit` when the dispatcher passes one, else the id Hermes
    binds for the dispatch in tools.approval_context (v0.22.7 passes none to handlers: model_tools._execute_tool
    gives only task_id, session_id and user_task). "" when neither exists; callers then fail closed."""
    if explicit:
        return str(explicit)
    try:
        var = getattr(sys.modules.get("tools.approval_context"), "_approval_tool_call_id", None)
        value = var.get() if var is not None else ""
        return value if isinstance(value, str) else ""
    except Exception:
        return ""


def take(session_id: str, tool_name: str, args: dict, tool_call_id: Any = None) -> Optional[Ticket]:
    """Pop the ticket the hook left for exactly this call and invocation (single use): an approval or a refusal.
    No invocation id, or another invocation's ticket, gives None and consumes nothing."""
    return TICKETS.take(_ticket_key(session_id, tool_name, args or {}), invocation_id(tool_call_id))


def verify(t: Optional[Ticket], tool_name: str, args: dict, helper_id: Optional[str] = None,
           scope: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """`hermes:<ticket uuid>` if `t` approved exactly the scope that will execute now, else None (see
    consume_ticket). A build approved with a resolved commit is re-resolved with that commit, never re-read."""
    if t is None or t.refusal is not None:
        return None
    probe = dict(args or {})
    if tool_name == "hands_xcode_build" and not probe.get("commit") and t.scope.get("commit"):
        probe["commit"] = t.scope["commit"]
    try:
        _, _, now_fields, _ = _resolved(tool_name, probe)
    except Refusal:
        return None
    if scope:
        now_fields.update(scope)
    if helper_id is not None:
        now_fields["helper_id"] = helper_id
        if t.helper_id != helper_id:
            return None
    if now_fields != t.scope:
        return None
    return f"hermes:{t.ticket_id}"


def consume_ticket(session_id: str, tool_name: str, args: dict, helper_id: Optional[str] = None,
                   scope: Optional[Dict[str, Any]] = None, tool_call_id: Any = None) -> Optional[str]:
    """Pop the ticket the hook left for exactly this call and invocation (see take: no id, no ticket). Returns
    `hermes:<ticket uuid>` for the contract's approval_ref, or None (the handler then refuses with
    denied/approval_not_observed).

    The ticket holds the scope Hermes approved (helper, workspace, scheme, configuration, destination, or
    helper and display), resolved when it was shown. The scope that will execute must be identical: it is
    re-resolved now, overridden by `scope` (what the handler is about to put in the contract) and, if given,
    `helper_id`. A default that drifted since the approval (e.g. a different default destination) refuses."""
    return verify(take(session_id, tool_name, args, tool_call_id), tool_name, args, helper_id=helper_id,
                  scope=scope)
