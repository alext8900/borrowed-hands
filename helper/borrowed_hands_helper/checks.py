"""PROTOCOL 4.2 checks 2 to 12, in order; the first failure wins and nothing executes before they all pass.

Step 1 (frame envelope, signature, sid, seq, exp) is the connection's job; step 13 (mark queued, reply
`job.accepted`) is the daemon's, right after this returns, with no await in between so the capacity answer
of step 12 still holds. Everything here is decided from the signed contract bytes and this Mac's config:
nothing depends on a model or on Hermes approval settings. Pure apart from the store transaction (step 6),
the rejection bookkeeping after it, and the injected confirm dialog (step 11).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Optional, Union

from . import schema
from ._shared import bhproto
from .config import DESTINATIONS, Config
from .executors import AcceptedJob
from .jobstore import Busy, Duplicate, Inserted, JobStore, Replay

log = logging.getLogger("borrowed_hands.helper")

CLOCK_SKEW_MS = 60_000
MAX_CONTRACT_WINDOW_MS = 600_000
MAX_CONTRACT_BYTES = 16 * 1024      # a real contract is under 1 KiB; nothing larger is parsed or stored
CONFIRM_BUSY = "busy"               # what the confirm function returns while another dialog is open


@dataclass(frozen=True)
class Rejected:
    code: str
    message: str
    detail: dict = field(default_factory=dict)
    retry_after_s: Optional[int] = None

    def body(self) -> dict:
        b = {"code": self.code, "message": self.message, "detail": dict(self.detail)}
        if self.retry_after_s is not None:
            b["retry_after_s"] = self.retry_after_s
        return b


Outcome = Union[AcceptedJob, Rejected, Duplicate]
# confirm(summary) -> True (allowed), False (declined), None (no answer within 120 s) or CONFIRM_BUSY (another
# dialog is already open: at most one at a time)
ConfirmFn = Callable[[dict], Awaitable[Optional[bool]]]


def grant_hint(workspace_id: str) -> str:
    return (f"bh grant add xcode.build --workspace {workspace_id} --repo <https URL> --project <App.xcodeproj> "
            f"--scheme <Scheme>")


async def verify_contract(contract_bytes: bytes, sig: str, *, cfg: Config, keys: Mapping[str, bytes],
                          retired_keys: Mapping[str, bytes], store: JobStore, now_ms: int,
                          enabled: frozenset, has_room: Callable[[str], bool], confirm: ConfirmFn,
                          retry_after_s: Callable[[], int], why_disabled: Mapping[str, str] = {}) -> Outcome:
    """`keys`: the pinned server key(s) by key id. `retired_keys`: keys rotated out less than 15 minutes ago,
    honoured only for an idempotent resend of an already-accepted job (PROTOCOL 3.6). `enabled`: the actions
    that are both enabled in config and usable on this Mac (production install etc.)."""
    assert cfg.server is not None, "verify_contract needs a paired config"

    # 2. signature over the received bytes, with a pinned key whose id the contract names
    signer = None
    for kid, pub in list(keys.items()) + list(retired_keys.items()):
        if bhproto.verify(pub, bhproto.CTX_JOB, contract_bytes, sig):
            signer = kid
            break
    if signer is None:
        log.error("contract signature did not verify against the pinned server key; refusing")
        return Rejected("denied", "the request is not signed by the paired server", {"reason": "bad_signature"})

    # 3. size, parse and schema (the action's params included; unknown keys rejected)
    if len(contract_bytes) > MAX_CONTRACT_BYTES:
        return Rejected("invalid_arguments", "contract too large", {"reason": "too_large"})
    try:
        c = bhproto.load_strict(contract_bytes)
        schema.validate("contract", c)
    except (bhproto.ProtocolError, schema.SchemaError) as e:
        return Rejected("invalid_arguments", f"malformed contract: {e}", {"reason": "schema"})
    if c["server_key_id"] != signer:
        log.error("contract names key %s but was signed by %s; refusing", c["server_key_id"], signer)
        return Rejected("denied", "the request is not signed by the paired server", {"reason": "bad_signature"})

    # 4. addressed to this Mac
    if c["helper_id"] != cfg.server.helper_id:
        return Rejected("invalid_arguments", "the request is for a different Mac", {"reason": "wrong_helper"})

    # 5. time
    issued, expires = c["issued_at"], c["expires_at"]
    if not (issued - CLOCK_SKEW_MS <= now_ms <= expires and issued <= expires
            and expires - issued <= MAX_CONTRACT_WINDOW_MS):
        return Rejected("expired", "the request expired (or its time window is invalid)", {"reason": "expired"})

    # 6. replay, in one transaction
    action = c["action"]
    params = c["params"]
    chash = hashlib.sha256(contract_bytes).hexdigest()
    claim = store.claim(job_id=c["job_id"], nonce=c["nonce"], contract_hash=chash, contract=bytes(contract_bytes),
                        action=action, workspace_id=params.get("workspace_id"), expires_at=expires, now=now_ms)
    if isinstance(claim, Duplicate):
        return claim
    if isinstance(claim, Replay):
        return Rejected("replay", "this job id or nonce was already used for a different request",
                        {"reason": "replay"})
    if isinstance(claim, Busy):
        return Rejected("helper_busy", "too many refused requests recently; retry later",
                        {"reason": "rate_limited"}, retry_after_s=60)
    assert isinstance(claim, Inserted)
    if signer in retired_keys and signer not in keys:
        return _fail(store, c, now_ms, Rejected("denied", "the request is signed by a rotated-out server key",
                                                {"reason": "bad_signature"}))

    # 7. capability enabled locally (and usable)
    if action not in enabled:
        return _fail(store, c, now_ms, Rejected(
            "helper_unavailable", f"{action} is turned off on this Mac (enable it with 'bh caps enable {action}')",
            {"reason": why_disabled.get(action, "capability_disabled")}))

    ws = None
    if action == "xcode.build":
        # 8. grant
        ws = cfg.workspace(params["workspace_id"])
        if ws is None or ws.expired(now_ms):
            return _fail(store, c, now_ms, Rejected(
                "needs_grant", f"no current grant on this Mac for workspace {params['workspace_id']!r}",
                {"hint": grant_hint(params["workspace_id"]), "workspace_id": params["workspace_id"]}))
        # 9. allowlists: exact strings, no wildcards, prefixes or defaults
        for fld, allowed in (("scheme", ws.schemes), ("configuration", ws.configurations),
                             ("destination", tuple(d for d in ws.destinations if d in DESTINATIONS))):
            if params[fld] not in allowed:
                return _fail(store, c, now_ms, Rejected(
                    "needs_grant", f"{fld} {params[fld]!r} is not allowed by this Mac's grant for {ws.id!r}",
                    {"field": fld, "hint": f"bh grant add xcode.build --workspace {ws.id} ... --{fld} "
                                           f"'{params[fld]}'"}))
        # 10. the repo is the one this Mac granted
        if params["repo_fingerprint"] != ws.fingerprint:
            return _fail(store, c, now_ms, Rejected(
                "needs_resolution", "the repository does not match the one granted for this workspace",
                {"reason": "repo_mismatch"}))
        # 11. per-workspace device confirm; a timeout is a deny
        if ws.confirm:
            answer = await confirm({"job_id": c["job_id"], "action": action, "workspace": ws.id, "scheme": params["scheme"],
                                    "configuration": params["configuration"],
                                    "destination": params["destination"], "commit": params["commit"],
                                    "approval_ref": c["approval_ref"]})
            if answer == CONFIRM_BUSY:
                return _fail(store, c, now_ms, Rejected(
                    "helper_busy", "another build is waiting for confirmation on the Mac; retry later",
                    {"reason": "confirm_pending"}, retry_after_s=120))
            if answer is None:
                return _fail(store, c, now_ms, Rejected("denied", "nobody confirmed the build on the Mac in time",
                                                        {"reason": "device_confirm_timeout"}))
            if answer is not True:
                return _fail(store, c, now_ms, Rejected("denied", "the build was declined on the Mac",
                                                        {"reason": "device_declined"}))

    # 12. capacity: one active, 3 queued, else helper_busy
    if not has_room(action):
        return _fail(store, c, now_ms, Rejected(
            "helper_busy", "the Mac is busy (one job running and the queue is full); retry later",
            {"reason": "queue_full"}, retry_after_s=max(0, min(3600, int(retry_after_s())))))

    return AcceptedJob(job_id=c["job_id"], action=action, params=params, contract=c, contract_hash=chash,
                       expires_at=expires, approval_ref=c["approval_ref"], workspace=ws)


def _fail(store: JobStore, c: dict, now_ms: int, r: Rejected) -> Rejected:
    """A rejection after step 6: the row is kept, as failed with the error (PROTOCOL 4.3)."""
    store.finish_rejected(c["job_id"], r.body(), now_ms)
    return r
