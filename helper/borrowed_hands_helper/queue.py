"""Job lanes: one active job and at most 3 queued, FIFO, per lane.

`JobQueue` is the pure state; `Lane` drives it on the daemon's loop. On dequeue a lane re-checks the job
(PROTOCOL 4.2 step 14: expiry and that the grant still exists) and drops it if the check fails. Cancel of a
queued job removes it before it can ever reach an executor; cancel of the running job is routed to the
executor, and the lane waits for the run to finish (the run reports the terminal state). The daemon uses
one lane for builds and a separate one for system.info and screen.capture (PROTOCOL 4.3).
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Awaitable, Callable, Generic, Optional, Protocol, TypeVar

log = logging.getLogger("borrowed_hands.helper")

MAX_QUEUED = 3


class HasJobId(Protocol):
    job_id: str


J = TypeVar("J", bound=HasJobId)


class JobQueue(Generic[J]):
    def __init__(self, max_queued: int = MAX_QUEUED):
        self.max_queued = max_queued
        self.active: Optional[J] = None
        self._queued: deque[J] = deque()

    def has_room(self) -> bool:
        return (1 if self.active is not None else 0) + len(self._queued) < 1 + self.max_queued

    def enqueue(self, job: J) -> int:
        """Returns the number of jobs ahead of this one."""
        if not self.has_room():
            raise OverflowError("queue full")
        self._queued.append(job)
        return (1 if self.active is not None else 0) + len(self._queued) - 1

    def take(self) -> Optional[J]:
        if self.active is not None or not self._queued:
            return None
        self.active = self._queued.popleft()
        return self.active

    def done(self, job_id: str) -> None:
        if self.active is not None and _id(self.active) == job_id:
            self.active = None

    def cancel_queued(self, job_id: str) -> bool:
        for j in self._queued:
            if _id(j) == job_id:
                self._queued.remove(j)
                return True
        return False

    def queued(self) -> list[J]:
        return list(self._queued)

    def queued_ids(self) -> list[str]:
        return [_id(j) for j in self._queued]

    def depth(self) -> int:
        return (1 if self.active is not None else 0) + len(self._queued)


def _id(j) -> str:
    return j if isinstance(j, str) else j.job_id


Drop = tuple[str, dict]     # (terminal state, error body)


class Lane(Generic[J]):
    def __init__(self, name: str, *, run_job: Callable[[J], Awaitable[None]],
                 recheck: Callable[[J], Optional[Drop]],
                 on_drop: Callable[[J, str, dict], Awaitable[None]],
                 cancel_running: Callable[[str, str], Awaitable[None]], max_queued: int = MAX_QUEUED):
        self.name = name
        self.q: JobQueue[J] = JobQueue(max_queued)
        self._run_job = run_job
        self._recheck = recheck
        self._on_drop = on_drop
        self._cancel_running = cancel_running
        self._wake = asyncio.Event()
        self._held = False

    def hold(self, on: bool) -> None:
        """While held, nothing is dequeued (the running job, if any, is unaffected)."""
        self._held = on
        self._wake.set()

    def requeue_front(self, job: J) -> None:
        """Put the job that was just dequeued back at the head (it never started)."""
        if self.q.active is not None and self.q.active.job_id == job.job_id:
            self.q.active = None
        self.q._queued.appendleft(job)

    def has_room(self) -> bool:
        return self.q.has_room()

    def submit(self, job: J) -> int:
        pos = self.q.enqueue(job)
        self._wake.set()
        return pos

    def depth(self) -> int:
        return self.q.depth()

    def busy(self) -> bool:
        return self.q.active is not None

    async def cancel(self, job_id: str, reason: str = "cancelled_by_request") -> bool:
        if self.q.active is not None and self.q.active.job_id == job_id:
            await self._cancel_running(job_id, reason)
            return True
        for j in self.q.queued():
            if j.job_id == job_id:
                self.q.cancel_queued(job_id)
                await self._on_drop(j, "cancelled", {"code": "cancelled", "message": "the job was cancelled",
                                                     "detail": {"reason": reason}})
                return True
        return False

    async def cancel_matching(self, pred: Callable[[J], bool], reason: str) -> int:
        n = 0
        for j in self.q.queued():
            if pred(j):
                n += await self.cancel(j.job_id, reason)
        if self.q.active is not None and pred(self.q.active):
            n += await self.cancel(self.q.active.job_id, reason)
        return n

    async def run(self) -> None:
        while True:
            job = None if self._held else self.q.take()
            if job is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                drop = self._recheck(job)
                if drop is not None:
                    await self._on_drop(job, *drop)
                else:
                    await self._run_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("lane %s: job %s crashed in the helper", self.name, job.job_id[:8])
            finally:
                if self.q.active is not None and self.q.active.job_id == job.job_id:
                    self.q.done(job.job_id)
