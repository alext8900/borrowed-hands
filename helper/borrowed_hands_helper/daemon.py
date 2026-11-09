"""`bh run`: the helper daemon. One asyncio loop holding the connection, the build lane, the light lane
(system.info, screen.capture) and the config watcher; at most one dispatcher child (the xcode executor).

Job lifecycle on this Mac (PROTOCOL 4.3):
    received --(checks 7-12 pass)--> queued --> building --> succeeded | failed | cancelled
    received --(a check fails)--> failed (error reply)        queued --(dequeue re-check)--> expired | cancelled
Every transition commits to the job store before the frame announcing it is sent; results are re-sent from
the store (jobs.sync) until the server acks them, so a disconnect never loses one.
"""
from __future__ import annotations

import asyncio
import dataclasses
import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Awaitable, Callable, Optional

from . import caps as capsmod
from . import checks, config, keyfile, schema
from ._shared import bhproto
from .config import Config, ConfigError
from .connection import (BadBody, Closed, Session, Stop, Timing, Transport, UnknownType, now_ms,
                         run_forever, AiohttpTransport)
from .executors import AcceptedJob, Executor, JobOutcome
from .jobstore import JobStore, TERMINAL, restarted_error
from .queue import Lane

log = logging.getLogger("borrowed_hands.helper")

LOCK_NAME = "bh.lock"
DB_NAME = "jobs.sqlite3"
STATUS_NAME = "status.json"
CONFIRM_TIMEOUT_S = 120
RETIRED_KEY_MS = 15 * 60_000
EST_BUILD_S = 300


BUILD_CLEANUP_PENDING = "build_cleanup_pending"
MAX_CONTRACT_B64 = (checks.MAX_CONTRACT_BYTES * 4 + 2) // 3


class DialogCancelled(Exception):
    def __init__(self, job_id: str, reason: str):
        super().__init__(job_id)
        self.job_id, self.reason = job_id, reason


def _ws_dict(ws) -> dict:
    return dataclasses.asdict(ws)


def _ws_from(d: dict):
    from .config import Workspace
    return Workspace(**{k: tuple(v) if isinstance(v, list) else v for k, v in d.items()})


def lock_or_none(home: Path):
    """Single daemon per user: an exclusive flock on bh.lock. Returns the fd, or None if held."""
    fd = os.open(home / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def daemon_running(home: Path) -> bool:
    try:
        fd = lock_or_none(home)
    except OSError:
        return False
    if fd is None:
        return True
    os.close(fd)
    return False


def write_status(home: Path, **fields) -> None:
    tmp = home / f".{STATUS_NAME}.{os.getpid()}"
    data = json.dumps({**fields, "updated_at": now_ms(), "pid": os.getpid()}).encode()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, home / STATUS_NAME)


def read_status(home: Path) -> dict:
    try:
        with open(home / STATUS_NAME, "rb") as f:
            return json.loads(f.read(65536))
    except (OSError, ValueError):
        return {}


async def osascript_confirm(summary: dict, osascript: str = "/usr/bin/osascript",
                            on_spawn: Callable = lambda p: None) -> Optional[bool]:
    """PROTOCOL 4.2 step 11: a macOS dialog, `giving up after 120`. True allow, False deny, None no answer.
    The text travels as an argv item, never spliced into AppleScript source. Cancelling the coroutine (job
    cancelled, helper stopping) kills and reaps the dialog process."""
    text = (f"Hermes asks to run {summary['action']} on this Mac.\n\nWorkspace: {summary['workspace']}\n"
            f"Scheme: {summary['scheme']} ({summary['configuration']})\nDestination: {summary['destination']}\n"
            f"Commit: {summary['commit']}\nApproval: {summary['approval_ref']}")
    script = ['on run argv', 'set r to display dialog (item 1 of argv) with title "Borrowed Hands" '
              'buttons {"Deny", "Allow"} default button "Deny" cancel button "Deny" '
              f'giving up after {CONFIRM_TIMEOUT_S}', 'if gave up of r then return "timeout"',
              'return button returned of r', 'end run']
    argv = [osascript]
    for line in script:
        argv += ["-e", line]
    argv += ["--", text]
    try:
        p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return None
    on_spawn(p)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), CONFIRM_TIMEOUT_S + 15)
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        if p.returncode is None:
            p.kill()
        await asyncio.shield(p.wait())
        if isinstance(e, asyncio.CancelledError):
            raise
        return None
    answer = out.decode(errors="replace").strip()
    if p.returncode != 0:          # "Deny" is the cancel button: osascript exits 1 (user canceled)
        return False
    return True if answer == "Allow" else (None if answer == "timeout" else False)


async def _exec_capture(argv: list, stdin_bytes: bytes, timeout: float) -> tuple[int, str]:
    try:
        p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin"})
    except OSError:
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(p.communicate(stdin_bytes), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return 124, ""
    return p.returncode, out.decode(errors="replace")


def _last_status(out: str) -> Optional[str]:
    for line in reversed(out.strip().splitlines()):
        try:
            return json.loads(line).get("status")
        except (ValueError, AttributeError):
            continue
    return None


CLEANUP_TIMEOUT_S = 60.0
CANCEL_OK = ("cancel_sent", "not_running")       # the dispatcher's replies; anything else is a failed cleanup


async def dispatcher_kill_path(running_job_ids: list, prefix: Optional[list] = None) -> bool:
    """PROTOCOL 4.5 start-up kill path, through the pinned sudoers lines: `--cancel` (job id on stdin) for every
    job the previous helper process left running, then `--sweep-only`, which kills every bh-builder process.
    True only if every cancel replied cancel_sent or not_running with exit 0, and the sweep replied swept with
    exit 0. A usage reply (an older dispatcher), a non-zero exit, garbage or a timeout is a failure."""
    prefix = prefix or [capsmod.SUDO, "-n", capsmod.DISPATCH]
    ok = True
    for jid in running_job_ids:
        rc, out = await _exec_capture([*prefix, "--cancel"], (jid + "\n").encode(), CLEANUP_TIMEOUT_S)
        st = _last_status(out)
        good = rc == 0 and st in CANCEL_OK
        ok = ok and good
        (log.info if good else log.error)("kill path: cancel %s -> %s (exit %s)", jid[:8], st, rc)
    rc, out = await _exec_capture([*prefix, "--sweep-only"], b"", CLEANUP_TIMEOUT_S * 3)
    swept = rc == 0 and _last_status(out) == "swept"
    (log.info if swept else log.error)("kill path: sweep %s (exit %s)", "ok" if swept else "FAILED", rc)
    return ok and swept


@dataclass
class Deps:
    """Everything the daemon touches outside itself; tests replace these."""
    executors: dict
    probes: Callable[[config.Policy], capsmod.Probes]
    host: Callable[[], capsmod.Host]
    confirm: Callable[[dict], Awaitable[Optional[bool]]] = osascript_confirm
    kill_path: Callable[[list], Awaitable[bool]] = dispatcher_kill_path
    clock: Callable[[], int] = now_ms
    timing: Timing = Timing()
    connect: Callable = AiohttpTransport.connect
    rand: Callable[[], float] = __import__("random").random
    sleep: Callable = asyncio.sleep
    config_poll_s: float = 2.0
    probe_refresh_s: float = 300.0
    prune_every_s: float = 600.0
    stop_wait_s: float = 90.0        # one deadline for the whole orderly stop; under the LaunchAgent ExitTimeOut
    sweep_retry_s: float = 10.0      # first retry of a failed start-up cleanup; doubles up to 300 s


def default_deps() -> Deps:
    from .executors import load_executors
    return Deps(executors=load_executors(), probes=capsmod.probe, host=capsmod.host)


class Daemon:
    def __init__(self, home: Path, deps: Deps):
        self.home = Path(home)
        self.d = deps
        self.cfg: Optional[Config] = None
        self.cfg_mtime = None
        self.priv = None
        self.store: Optional[JobStore] = None
        self.probes: Optional[capsmod.Probes] = None
        self.session: Optional[Session] = None
        self.t: Optional[Transport] = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        self._retired: dict[str, tuple[bytes, int]] = {}
        self._retry_after: Optional[int] = None
        self._reconnect = False
        self._last_caps = None
        self._status = {"state": "starting", "detail": ""}
        self._stopping = False
        self._cancel_reason: dict[str, str] = {}        # running jobs being cancelled, and why
        self._dialog_job: Optional[str] = None          # at most one confirm dialog at a time
        self._dialog_task: Optional[asyncio.Task] = None
        self._dialog_cancel: dict[str, str] = {}
        self._build_block: Optional[str] = None         # set while a start-up cleanup has not succeeded
        self.build = Lane("build", run_job=self._run_job, recheck=self._recheck, on_drop=self._drop,
                          cancel_running=self._cancel_running)
        self.light = Lane("light", run_job=self._run_job, recheck=self._recheck, on_drop=self._drop,
                          cancel_running=self._cancel_running)

    # ------------------------------------------------------------ entry
    async def run(self) -> int:
        try:
            keyfile.check_private_dir(self.home)
            self.cfg = config.load(self.home)
            self.cfg_mtime = config.mtime(self.home)
            if self.cfg.server is None or not keyfile.has_device_key(self.home):
                print("this Mac is not paired: run 'bh setup' (or 'bh pair')", flush=True)
                return 3
            self.priv = keyfile.load_device_key(self.home)
        except (keyfile.UnsafeKeyStorage, ConfigError) as e:
            print(f"refusing to start: {e}", flush=True)
            return 1
        lock = lock_or_none(self.home)
        if lock is None:
            print("another bh daemon is already running for this user", flush=True)
            return 1
        try:
            return await self._run_locked()
        finally:
            os.close(lock)

    async def _run_locked(self) -> int:
        self.store = JobStore(self.home / DB_NAME)
        try:
            # PROTOCOL 4.5 start-up, before recovering rows or accepting work: stop anything the previous process
            # left running in the build domain (cancel its jobs, sweep every bh-builder process).
            running = [r.job_id for r in self.store.by_state("materializing", "building")]
            clean = True
            if running or self.cfg.capabilities.xcode_build:
                clean = await self.d.kill_path(running)
            if not clean:
                # A build may still be alive: leave its row unresolved, run no build until a sweep succeeds.
                log.error("start-up cleanup of the build user failed; builds are paused and it will be retried")
                self._block_builds(BUILD_CLEANUP_PENDING)
            # Then: running jobs died with the old process (only once cleaned up); expired queued jobs expire.
            for row in self.store.recover_on_start(self.d.clock(), keep_running=not clean):
                job = self._job_from_row(row)
                if job is not None:
                    self._lane(job.action).submit(job)
            self.store.prune(self.d.clock())
            self.probes = await self._probe()
            workers = [asyncio.create_task(self.build.run()), asyncio.create_task(self.light.run()),
                       asyncio.create_task(self._watch())]
            if not clean:
                workers.append(asyncio.create_task(self._retry_cleanup()))
            try:
                stop = await run_forever(lambda: self.cfg.server, lambda: self.priv, self._on_session,
                                         timing=self.d.timing, connect=self.d.connect, rand=self.d.rand,
                                         sleep=self.d.sleep, clock=self.d.clock, on_status=self._set_status,
                                         retry_after=lambda: self._retry_after)
                print(stop.message, flush=True)
                if stop.unpaired:
                    await self._forget_pairing()
                    return 3
                return stop.exit_code
            finally:
                await self._drain()
                for w in workers:
                    w.cancel()
        finally:
            for t in list(self._tasks):
                t.cancel()
            self.store.close()

    async def _drain(self) -> None:
        """Orderly stop: drop pending submits and dialogs, cancel running jobs through their executors, and wait
        (bounded) for them to finish so no build outlives the helper unreported."""
        end = time.monotonic() + self.d.stop_wait_s          # one deadline for everything below
        self._stopping = True
        for lane in (self.build, self.light):                 # first: nothing new starts
            lane.hold(True)
        for t in list(self._tasks):
            t.cancel()
        for lane in (self.build, self.light):
            if lane.q.active is not None:
                try:
                    await asyncio.wait_for(lane.cancel(lane.q.active.job_id, "helper_stopping"),
                                           max(0.0, end - time.monotonic()))
                except asyncio.TimeoutError:
                    log.error("executor cancel did not return before the stop deadline")
                except Exception:
                    log.exception("cancel on stop")
        while (self.build.busy() or self.light.busy()) and time.monotonic() < end:
            await asyncio.sleep(0.05)
        if self.build.busy() or self.light.busy():
            log.error("a job did not stop within %ss; the next start-up sweep cleans up", self.d.stop_wait_s)

    # ------------------------------------------------------------ start-up cleanup (HIGH 1, round 2)
    def _block_builds(self, reason: str) -> None:
        self._build_block = reason
        self.build.hold(True)
        self._set_status(self._status["state"], self._status.get("detail", ""))

    async def _retry_cleanup(self) -> None:
        delay = self.d.sweep_retry_s
        while self._build_block is not None:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300.0)
            running = [r.job_id for r in self.store.by_state("materializing", "building")]
            if not await self.d.kill_path(running):
                log.error("start-up cleanup retry failed; builds stay paused (next try in %.0fs)", delay)
                continue
            for jid in running:
                await self._finish(jid, "failed", None, restarted_error())
            self._build_block = None
            self.build.hold(False)
            log.info("start-up cleanup succeeded; builds resume")
            self._set_status(self._status["state"], self._status.get("detail", ""))
            if self.session is not None:
                await self._send_caps()

    # ------------------------------------------------------------ status
    def _set_status(self, state: str, detail: str) -> None:
        self._status = {"state": state, "detail": detail}
        if state == "connected":
            self._status["proof_ok_at"] = self.d.clock()
        try:
            write_status(self.home, **self._status, server=self.cfg.server.url if self.cfg.server else None,
                         busy=self.build.busy(), queue_depth=self.build.depth(), builds_blocked=self._build_block)
        except OSError:
            pass

    # ------------------------------------------------------------ probes and caps
    async def _probe(self) -> capsmod.Probes:
        return await asyncio.get_running_loop().run_in_executor(None, self.d.probes, self.cfg.policy)

    def _enabled(self) -> frozenset:
        ok = capsmod.usable(self.cfg, self.probes) & frozenset(self.d.executors)
        return ok - {"xcode.build"} if self._build_block is not None else ok

    def _caps(self) -> dict:
        p = self.probes
        if "screen.capture" not in self.d.executors:
            p = replace(p, screen_granted=False)
        if "xcode.build" not in self.d.executors or self._build_block is not None:
            p = replace(p, xcode=None)          # not advertised: builds unavailable (the reason is in bh status)
        return capsmod.caps_body(self.cfg, p, busy=self.build.busy(), queue_depth=self.build.depth(),
                                 host=self.d.host())

    async def _send_caps(self, force: bool = False) -> None:
        body = self._caps()
        if force or body != self._last_caps:
            self._last_caps = body
            await self._send("caps", body)

    # ------------------------------------------------------------ outbound
    async def _send(self, type_: str, body: dict, re: Optional[str] = None) -> Optional[str]:
        """Encode and send under one lock so seq order on the wire is the encode order. Not connected: the
        frame is dropped (states are re-derivable; results are re-sent from the store by jobs.sync)."""
        async with self._send_lock:
            if self.session is None or self.t is None:
                return None
            text, fid = self.session.encode(type_, body, re)
            try:
                await self.t.send(text)
            except (Closed, ConnectionError, RuntimeError):
                return None
            return fid

    async def _request(self, type_: str, body: dict, timeout: float) -> Optional[dict]:
        fut = asyncio.get_running_loop().create_future()
        fid = await self._send_with_future(type_, body, fut)
        if fid is None:
            return None
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending.pop(fid, None)

    async def _send_with_future(self, type_, body, fut):
        async with self._send_lock:
            if self.session is None or self.t is None:
                return None
            text, fid = self.session.encode(type_, body)
            self._pending[fid] = fut
            try:
                await self.t.send(text)
            except (Closed, ConnectionError, RuntimeError):
                return None
            return fid

    # ------------------------------------------------------------ session
    async def _on_session(self, session: Session, t: Transport) -> None:
        self.session, self.t = session, t
        self._last_caps = None
        try:
            await self._send_caps(force=True)                                             # seq 1
            await self._send("jobs.sync", self._sync_body())                              # seq 2
            while True:
                if self._reconnect:
                    self._reconnect = False
                    return
                text = await t.recv()
                try:
                    frame = session.decode(text)
                except UnknownType as u:
                    await self._send("error", {"code": "invalid_arguments", "message": f"unknown type {u.type}",
                                               "detail": {"reason": "unknown_type"}}, re=u.frame_id)
                    continue
                except BadBody as b:
                    await self._send("error", {"code": "invalid_arguments", "message": f"bad body: {b}"[:900],
                                               "detail": {"reason": "schema"}}, re=b.frame_id)
                    continue
                await self._dispatch(frame)
        finally:
            self.session, self.t = None, None
            for f in self._pending.values():
                if not f.done():
                    f.cancel()

    def _sync_body(self) -> dict:
        active = [{"job_id": r.job_id, "state": r.state} for r in self.store.active()][:100]
        results = [self.store.result_body(r) for r in self.store.undelivered(100)]
        return {"active": active, "results": results}

    async def _dispatch(self, f) -> None:
        if f.type == "job.submit":
            task = asyncio.create_task(self._on_submit(f))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif f.type == "job.cancel":
            await self._on_cancel(f)
        elif f.type == "job.ack":
            self.store.mark_delivered(f.body["job_ids"])
        elif f.type in ("ack", "error"):
            if f.type == "error":
                self._retry_after = f.body.get("retry_after_s")
                log.warning("server error: %s %s", f.body["code"], f.body["message"][:200])
            fut = self._pending.get(f.re or "")
            if fut is not None and not fut.done():
                fut.set_result({"type": f.type, "body": f.body})
        elif f.type == "server.rotate":
            await self._on_server_rotate(f)
        elif f.type == "unpaired":
            raise Stop(3, "unpaired by server", unpaired=True)

    # ------------------------------------------------------------ submit (PROTOCOL 4.2)
    def _lane(self, action: str) -> Lane:
        return self.build if action == "xcode.build" else self.light

    def _retry_estimate(self) -> int:
        return min(3600, max(30, self.build.depth() * EST_BUILD_S))

    async def _confirm(self, summary: dict):
        """At most one confirm dialog at a time (MEDIUM 5); others are refused helper_busy at step 11."""
        if self._dialog_job is not None:
            return checks.CONFIRM_BUSY
        jid = summary["job_id"]
        self._dialog_job, self._dialog_task = jid, asyncio.current_task()
        try:
            return await self.d.confirm(summary)
        except asyncio.CancelledError:
            if jid in self._dialog_cancel:
                asyncio.current_task().uncancel()
                raise DialogCancelled(jid, self._dialog_cancel.pop(jid)) from None
            raise
        finally:
            self._dialog_job, self._dialog_task = None, None

    async def _on_submit(self, f) -> None:
        if len(f.body["contract"]) > MAX_CONTRACT_B64:
            await self._send("error", {"code": "invalid_arguments", "message": "contract too large",
                                       "detail": {"reason": "too_large"}}, re=f.id)
            return
        try:
            raw = bhproto.unb64u(f.body["contract"])
        except bhproto.ProtocolError:
            await self._send("error", {"code": "invalid_arguments", "message": "contract is not base64url",
                                       "detail": {"reason": "schema"}}, re=f.id)
            return
        now = self.d.clock()
        retired = {k: pub for k, (pub, until) in self._retired.items() if now < until}
        try:
            out = await checks.verify_contract(
                raw, f.body["sig"], cfg=self.cfg, keys={self.cfg.server.key_id: self.cfg.server.pubkey},
                retired_keys=retired, store=self.store, now_ms=now, enabled=self._enabled(),
                has_room=lambda a: self._lane(a).has_room(), confirm=self._confirm,
                retry_after_s=self._retry_estimate,
                why_disabled={"xcode.build": self._build_block} if self._build_block else {})
        except DialogCancelled as dc:
            err = {"code": "cancelled", "message": "the job was cancelled", "detail": {"reason": dc.reason}}
            self.store.finish(dc.job_id, "cancelled", None, err, self.d.clock())
            if await self._send("error", err, re=f.id) is not None:
                self.store.mark_delivered([dc.job_id])
            return
        if isinstance(out, checks.Rejected):
            log.info("refused a %s request: %s", "job", out.code)
            if await self._send("error", out.body(), re=f.id) is not None:
                self._mark_rejection_delivered(raw)
            return
        if isinstance(out, checks.Duplicate):
            row = out.row
            await self._send("job.state", {"job_id": row.job_id, "state": row.state, "at": row.updated_at}, re=f.id)
            if row.terminal:
                await self._send("job.result", self.store.result_body(row))
            return
        # 13. queued, then accepted (no await between the capacity check and the enqueue)
        self.store.set_state(out.job_id, "queued", self.d.clock())
        if out.workspace is not None:
            self.store.set_grant(out.job_id, _ws_dict(out.workspace))
        pos = self._lane(out.action).submit(out)
        log.info("accepted %s job %s", out.action, out.job_id[:8])
        await self._send("job.accepted", {"job_id": out.job_id, "state": "queued", "at": self.d.clock(),
                                          "position": pos}, re=f.id)
        await self._send_caps()

    def _mark_rejection_delivered(self, raw: bytes) -> None:
        import hashlib
        try:
            jid = bhproto.load_strict(raw).get("job_id")
        except (bhproto.ProtocolError, AttributeError):
            return
        row = self.store.get(jid) if isinstance(jid, str) else None
        if row is not None and row.contract_hash == hashlib.sha256(raw).hexdigest() and row.state == "failed":
            self.store.mark_delivered([jid])

    # ------------------------------------------------------------ running jobs
    def _job_from_row(self, row) -> Optional[AcceptedJob]:
        c = bhproto.load_strict(row.contract)
        snap = self.store.grant_of(row.job_id)
        ws = _ws_from(snap) if (c["action"] == "xcode.build" and snap) else None   # None: fails the grant compare
        return AcceptedJob(job_id=row.job_id, action=c["action"], params=c["params"], contract=c,
                           contract_hash=row.contract_hash, expires_at=c["expires_at"],
                           approval_ref=c["approval_ref"], workspace=ws)

    def _still_allowed(self, job: AcceptedJob) -> Optional[str]:
        """None if this Mac still allows the job, else the reason (for the dequeue re-check and revokes)."""
        # Cleanup availability holds the build lane and final execution gate; it does not revoke permission.
        if job.action not in (capsmod.usable(self.cfg, self.probes) & frozenset(self.d.executors)):
            return "capability_disabled"
        if job.action == "xcode.build":
            ws = self.cfg.workspace(job.params["workspace_id"])
            p = job.params
            if (ws is None or ws.expired(self.d.clock()) or p["scheme"] not in ws.schemes
                    or p["configuration"] not in ws.configurations or p["destination"] not in ws.destinations
                    or p["repo_fingerprint"] != ws.fingerprint):
                return "grant_revoked"
            # The grant the job was accepted under: a different checkout (remote, project) or a newly required
            # confirmation means the owner never approved this run as it would now execute.
            snap = job.workspace
            if (snap is None or ws.remote_url != snap.remote_url or ws.project != snap.project
                    or (ws.confirm and not snap.confirm)):
                return "grant_changed"
        return None

    def _authorize(self, job: AcceptedJob):
        """Expiry and the current grant, at dequeue and again right before the executor starts. Returns the job
        rebuilt on the current grant, or a (state, error) drop."""
        if self.d.clock() > job.expires_at:
            return "expired", {"code": "expired", "message": "the job expired before it started",
                               "detail": {"reason": "expired_in_queue"}}
        why = self._still_allowed(job)
        if why is not None:
            return "cancelled", {"code": "cancelled", "message": "the grant for this job changed on the Mac",
                                 "detail": {"reason": why}}
        if job.action == "xcode.build":
            return dataclasses.replace(job, workspace=self.cfg.workspace(job.params["workspace_id"]))
        return job

    def _recheck(self, job: AcceptedJob):
        """PROTOCOL 4.2 step 14, on dequeue."""
        out = self._authorize(job)
        return out if isinstance(out, tuple) else None

    async def _transition(self, job_id: str, state: str) -> None:
        row = self.store.get(job_id)
        if row is None or row.state == state or row.terminal:
            return
        self.store.set_state(job_id, state, self.d.clock())
        await self._send("job.state", {"job_id": job_id, "state": state, "at": self.d.clock()})

    async def _finish(self, job_id: str, state: str, result: Optional[dict], error: Optional[dict]) -> None:
        if not self.store.finish(job_id, state, result, error, self.d.clock()):
            return
        log.info("job %s %s", job_id[:8], state)
        await self._send("job.state", {"job_id": job_id, "state": state, "at": self.d.clock()})
        await self._send("job.result", self.store.result_body(self.store.get(job_id)))
        await self._send_caps()

    async def _drop(self, job: AcceptedJob, state: str, error: dict) -> None:
        await self._finish(job.job_id, state, None, error)

    async def _run_job(self, job: AcceptedJob) -> None:
        ex: Executor = self.d.executors[job.action]
        if job.action == "xcode.build":
            await self._transition(job.job_id, "building")     # PROTOCOL 4.3: building from dispatch start
            await self._send_caps()

        async def emit(state: str) -> None:
            if state in ("materializing", "building"):
                await self._transition(job.job_id, state)

        # Final gate, with no await between it and ex.run: a cancel or revoke that arrived during the awaits
        # above (when there was no process to stop yet) is honoured here (HIGH 2).
        if self._stopping or (job.action == "xcode.build" and self._build_block is not None):
            # Never started: back to queued (it resumes after the restart or the cleanup), not cancelled.
            self._cancel_reason.pop(job.job_id, None)
            self.store.set_state(job.job_id, "queued", self.d.clock())
            if not self._stopping:
                self._lane(job.action).requeue_front(job)
            return
        reason = self._cancel_reason.get(job.job_id)
        row = self.store.get(job.job_id)
        if reason is None and row is not None and row.cancel_requested:
            reason = "cancelled_on_mac"
        authorized = self._authorize(job)
        if reason is not None or isinstance(authorized, tuple):
            self._cancel_reason.pop(job.job_id, None)
            state, err = authorized if isinstance(authorized, tuple) else (
                "cancelled", {"code": "cancelled", "message": "the job was cancelled", "detail": {"reason": reason}})
            await self._finish(job.job_id, state, None, err)
            return
        job = dataclasses.replace(authorized, policy=self.cfg.policy,
                                  xcode_version=" ".join(self.probes.xcode) if self.probes and self.probes.xcode else "")
        try:
            out = await ex.run(job, emit)
            if job.action == "xcode.build" and out == "busy":
                await self._transition(job.job_id, "queued")
                await asyncio.sleep(5)
                if not self._stopping:
                    self.build.requeue_front(job)
                return
            if not isinstance(out, JobOutcome) or out.state not in ("succeeded", "failed", "cancelled"):
                raise TypeError("executor returned something other than a JobOutcome")
            schema.validate("job-result", {"job_id": job.job_id, "state": out.state, "result": out.result,
                                           "error": out.error})
        except asyncio.CancelledError:
            raise
        except schema.SchemaError as e:
            log.error("executor result for %s failed the schema (%s); reporting internal_error", job.job_id[:8], e)
            out = JobOutcome.internal_error("invalid_result")
        except Exception:
            log.exception("executor for %s crashed", job.action)
            out = JobOutcome.internal_error("executor_crashed")
        why = self._cancel_reason.pop(job.job_id, None)
        if why is not None and out.state == "cancelled":
            out = JobOutcome("cancelled", None, {"code": "cancelled", "message": "the job was cancelled",
                                                 "detail": {"reason": why}})
        await self._finish(job.job_id, out.state, out.result, out.error)

    async def _cancel_running(self, job_id: str, reason: str = "cancelled") -> None:
        """Persist the cancel first (so the final gate in _run_job sees it even if the executor has no process
        yet), then ask the executor to stop."""
        self._cancel_reason.setdefault(job_id, reason)
        if reason != "helper_stopping":          # a stop is not the owner's cancel: the job may resume later
            self.store.request_cancel(job_id)
        row = self.store.get(job_id)
        if row is not None:
            await self.d.executors[row.action].cancel(job_id)

    async def _cancel_job(self, job_id: str, reason: str) -> bool:
        """Cancel wherever the job is: waiting for its confirm dialog, queued, or running."""
        if self._dialog_job == job_id and self._dialog_task is not None:
            self._dialog_cancel[job_id] = reason
            self._dialog_task.cancel()
            return True
        row = self.store.get(job_id)
        return row is not None and await self._lane(row.action).cancel(job_id, reason)

    async def _on_cancel(self, f) -> None:
        jid = f.body["job_id"]
        row = self.store.get(jid)
        if row is None:
            await self._send("error", {"code": "invalid_arguments", "message": "unknown job",
                                       "detail": {"reason": "unknown_job"}}, re=f.id)
            return
        if not row.terminal:
            await self._cancel_job(jid, "cancelled_by_agent")
        await self._send("ack", {}, re=f.id)

    # ------------------------------------------------------------ rotation and unpair
    async def _on_server_rotate(self, f) -> None:
        b = f.body
        old = self.cfg.server
        new_pub = bhproto.unb64u(b["new_pubkey"])
        signed = bhproto.canonical({"new_key_id": b["new_key_id"], "new_pubkey": b["new_pubkey"],
                                    "old_key_id": old.key_id})
        if (len(new_pub) != 32 or bhproto.key_id(new_pub) != b["new_key_id"]
                or not bhproto.verify(new_pub, bhproto.CTX_ROTATE_SERVER, signed, b["sig_new"])):
            log.error("server.rotate did not verify; ignoring it")
            await self._send("error", {"code": "denied", "message": "rotation proof failed",
                                       "detail": {"reason": "bad_signature"}}, re=f.id)
            return
        self.cfg = replace(self.cfg, server=replace(old, pubkey=new_pub, key_id=b["new_key_id"]))
        config.save(self.home, self.cfg)
        self.cfg_mtime = config.mtime(self.home)
        self._retired[old.key_id] = (old.pubkey, self.d.clock() + RETIRED_KEY_MS)
        log.info("server key rotated %s -> %s", old.key_id, b["new_key_id"])
        await self._send("ack", {}, re=f.id)
        self._reconnect = True        # the next hello must carry the new key id

    async def _forget_pairing(self) -> None:
        """PROTOCOL 3.5: cancel running work, delete pins and the device key."""
        for lane in (self.build, self.light):
            await lane.cancel_matching(lambda j: True, reason="revoked")
        keyfile.delete_device_key(self.home)
        cfg = config.load(self.home)
        config.save(self.home, replace(cfg, server=None))

    # ------------------------------------------------------------ config watcher (SPEC 8.3)
    async def _watch(self) -> None:
        last_probe = last_prune = time.monotonic()
        while True:
            await asyncio.sleep(self.d.config_poll_s)
            try:
                for jid in self.store.cancel_requests():
                    if jid not in self._cancel_reason:
                        await self._cancel_job(jid, "cancelled_on_mac")
                if time.monotonic() - last_prune > self.d.prune_every_s:
                    self.store.prune(self.d.clock())
                    last_prune = time.monotonic()
                m = config.mtime(self.home)
                changed = m != self.cfg_mtime
                if changed:
                    self.cfg_mtime = m
                    try:
                        new = config.load(self.home)
                    except (ConfigError, keyfile.UnsafeKeyStorage) as e:
                        log.error("config.toml is invalid, keeping the previous one: %s", e)
                        self._set_status(self._status["state"], f"config error: {e}")
                        new = None
                    if new is not None and new.server == self.cfg.server:
                        self.cfg = new
                        self.probes = await self._probe()
                        last_probe = time.monotonic()
                        for lane in (self.build, self.light):
                            jobs = lane.q.queued() + ([lane.q.active] if lane.q.active is not None else [])
                            for j in jobs:
                                why = self._still_allowed(j)
                                if why is not None and j.job_id not in self._cancel_reason:
                                    await lane.cancel(j.job_id, why)
                    elif new is not None:
                        log.error("config.toml [server] changed under a running daemon; restart it")
                elif time.monotonic() - last_probe > self.d.probe_refresh_s:
                    self.probes = await self._probe()
                    last_probe = time.monotonic()
                if self.session is not None:
                    await self._send_caps(force=changed)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("config watcher")
