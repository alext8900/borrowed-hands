"""The executor interface that capabilities implement (xcode.build, system.info; screen.capture is not built).

An executor only ever receives an `AcceptedJob`: a contract that passed every check of PROTOCOL 4.2
(signature, schema, helper, time, replay, capability, grant, allowlists, fingerprint, confirm, capacity),
paired with this Mac's own workspace config. Executors never see Hermes-supplied URLs or paths: `remote`
and `project` come from `workspace`, never from `params`.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Mapping, Optional, Protocol

from ..config import Policy, Workspace

log = logging.getLogger("borrowed_hands.helper")

Action = Literal["system.info", "screen.capture", "xcode.build"]
TerminalState = Literal["succeeded", "failed", "cancelled"]


@dataclass(frozen=True)
class AcceptedJob:
    job_id: str
    action: Action
    params: Mapping              # the verified contract's params (schema-checked)
    contract: Mapping            # the whole verified contract
    contract_hash: str
    expires_at: int              # unix ms
    approval_ref: str
    workspace: Optional[Workspace]   # the matching local grant for xcode.build, else None
    policy: Optional[Policy] = None
    xcode_version: str = ""


@dataclass(frozen=True)
class JobOutcome:
    state: TerminalState
    result: Optional[dict] = None    # must match job-result.json's result shapes
    error: Optional[dict] = None     # an error.json body, or None

    @classmethod
    def internal_error(cls, reason: str, message: str = "internal error on the Mac") -> "JobOutcome":
        return cls("failed", None, {"code": "internal_error", "message": message, "detail": {"reason": reason}})


EmitState = Callable[[str], Awaitable[None]]


class Executor(Protocol):
    """Contract for capability executors (xcode, screen):
    - `run` is called only after the daemon's final authorization gate; `job.workspace` is the CURRENT grant.
    - `cancel(job_id)` must be effective whenever it is called: before `run` has spawned anything (remember it and
      make `run` return `cancelled` without starting a process), while it runs (SIGTERM the `sudo -n bh-dispatch`
      child; the dispatcher turns that into a clean cancel), and after it finished (no-op).
    - `run` must also stop its child if the awaiting task is cancelled (helper shutdown).
    - xcode: invoke `["/usr/bin/sudo", "-n", *caps.dispatch_argv(policy)]` verbatim; the sudoers lines match it."""

    async def run(self, job: AcceptedJob, emit_state: EmitState) -> JobOutcome: ...

    async def cancel(self, job_id: str) -> None: ...


# Capability lanes register here; a missing module just means the capability is not built yet.
_MODULES = {"system.info": "system_info", "xcode.build": "xcode", "screen.capture": "screen"}


def load_executors(**kwargs) -> dict[str, Executor]:
    """One executor per action whose module exists. Each module exposes `make_executor(**kwargs)`."""
    out: dict[str, Executor] = {}
    for action, mod in _MODULES.items():
        try:
            m = importlib.import_module(f"{__name__}.{mod}")
        except ModuleNotFoundError as e:
            if e.name != f"{__name__}.{mod}":
                raise
            continue
        out[action] = m.make_executor(**kwargs)
    return out
