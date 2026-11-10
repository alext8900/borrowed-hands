"""HE-8: one active job, at most 3 queued, FIFO, expiry and grant re-checked on dequeue, cancel."""
from __future__ import annotations

import asyncio

from borrowed_hands_helper.queue import JobQueue, Lane


def test_pure_queue_limits_and_fifo():
    q = JobQueue(max_queued=3)
    assert q.has_room()
    positions = [q.enqueue(f"j{i}") for i in range(4)]
    assert positions == [0, 1, 2, 3]
    assert q.take() == "j0" and q.active == "j0"
    assert q.has_room() is False                # 1 active + 3 queued: the 5th is refused
    assert q.take() is None                     # still busy
    assert q.cancel_queued("j2") is True and q.queued_ids() == ["j1", "j3"]
    assert q.has_room()
    q.done("j0")
    assert q.take() == "j1"


class Job:
    def __init__(self, job_id, ok=True):
        self.job_id = job_id
        self.ok = ok


def run(coro):
    return asyncio.run(coro)


def test_lane_runs_fifo_one_at_a_time_and_rechecks_on_dequeue():
    events = []

    async def main():
        gate = asyncio.Event()

        async def run_job(job):
            events.append(("start", job.job_id))
            if job.job_id == "a":
                await gate.wait()
            events.append(("end", job.job_id))

        def recheck(job):
            return None if job.ok else ("expired", {"code": "expired", "message": "expired before it started"})

        async def dropped(job, state, error):
            events.append(("drop", job.job_id, state))

        async def cancel_running(job_id, reason=None):
            events.append(("cancel_running", job_id))
            gate.set()

        lane = Lane("build", run_job=run_job, recheck=recheck, on_drop=dropped, cancel_running=cancel_running)
        task = asyncio.create_task(lane.run())
        for j in (Job("a"), Job("b", ok=False), Job("c"), Job("d")):
            assert lane.has_room()
            lane.submit(j)
        assert not lane.has_room()                      # a active + b, c, d queued
        await asyncio.sleep(0.01)
        assert await lane.cancel("d") is True           # queued: dropped, never started
        assert await lane.cancel("a") is True           # running: routed to the executor
        for _ in range(100):
            await asyncio.sleep(0.01)
            if ("end", "c") in events:
                break
        assert await lane.cancel("zzz") is False
        task.cancel()

    run(main())
    assert events == [("start", "a"), ("drop", "d", "cancelled"), ("cancel_running", "a"), ("end", "a"),
                      ("drop", "b", "expired"), ("start", "c"), ("end", "c")]


def test_lane_drop_matching_cancels_queued_and_running():
    events = []

    async def main():
        gate = asyncio.Event()

        async def run_job(job):
            events.append(("start", job.job_id))
            await gate.wait()

        async def dropped(job, state, error):
            events.append(("drop", job.job_id, error["detail"]["reason"]))

        async def cancel_running(job_id, reason=None):
            events.append(("cancel_running", job_id))
            gate.set()

        lane = Lane("build", run_job=run_job, recheck=lambda j: None, on_drop=dropped,
                    cancel_running=cancel_running)
        task = asyncio.create_task(lane.run())
        lane.submit(Job("a"))
        lane.submit(Job("b"))
        await asyncio.sleep(0.01)
        n = await lane.cancel_matching(lambda j: True, reason="grant_revoked")
        assert n == 2
        await asyncio.sleep(0.01)
        task.cancel()

    run(main())
    assert ("drop", "b", "grant_revoked") in events and ("cancel_running", "a") in events


def test_cancel_matching_stops_the_running_job_even_when_reporting_a_queued_one_never_returns():
    """Security review round 3, S1: dropping a queued job reports to the server, and a server that stops reading
    its socket holds that report. The running job's cancel reaches only the executor, so it goes first, and no
    queued job is started in its place while the cancels are under way."""
    events = []

    async def main():
        gate = asyncio.Event()

        async def run_job(job):
            events.append(("start", job.job_id))
            await gate.wait()

        async def dropped(job, state, error):
            events.append(("drop", job.job_id))
            await asyncio.sleep(3600)                  # the report that never gets through

        async def cancel_running(job_id, reason=None):
            events.append(("cancel_running", job_id))
            gate.set()

        lane = Lane("build", run_job=run_job, recheck=lambda j: None, on_drop=dropped,
                    cancel_running=cancel_running)
        task = asyncio.create_task(lane.run())
        lane.submit(Job("a"))
        lane.submit(Job("b"))
        lane.submit(Job("c"))
        await asyncio.sleep(0.01)
        stuck = asyncio.create_task(lane.cancel_matching(lambda j: True, reason="revoked"))
        await asyncio.sleep(0.2)
        assert not stuck.done()
        assert events == [("start", "a"), ("cancel_running", "a"), ("drop", "b")]      # b and c never started
        stuck.cancel()
        task.cancel()

    run(main())
