"""A revoke reaches the running build while the server is not reading its socket (security review round 3, S1)."""
import asyncio

from fake_server import FakeServer
from test_config_lock import _running_daemon
from test_daemon import arun, until
from test_fix_round1 import RecordingBuild, edit_grant, two_builds


def test_a_revoke_reaches_the_running_build_while_the_server_is_not_reading(tmp_path):
    """A is running, B is queued, the server has stopped reading and the helper's sends wait. The owner revokes
    the grant. The running build's cancel needs no send, so it goes first; with the queued job first the watcher
    waits on that job's report and A keeps building."""
    async def main():
        srv = await FakeServer().start()
        ka = task = None
        try:
            fb = RecordingBuild()
            home, d, task = await _running_daemon(tmp_path, srv, fb, config_poll_s=0.05)
            a, b = await two_builds(srv, home, fb)
            await until(lambda: (r := d.store.get(b["job_id"])) is not None and r.state == "queued")
            await asyncio.sleep(0.4)               # caps for busy and queue_depth are out: nothing left to send

            async def keepalive():                 # frames that need no reply keep the dead timer quiet
                while True:
                    await srv.send("ack", {})
                    await asyncio.sleep(0.1)
            ka = asyncio.create_task(keepalive())
            srv.conn.ws._req.transport.pause_reading()                 # the server stops reading
            proto, n = d.t._ws._writer.protocol, 0
            while not (proto._paused and d._send_lock.locked()):       # small replies until one send waits
                for _ in range(200):
                    await srv.send("job.submit", {"contract": "AAAAA", "sig": "A" * 86})
                    n += 1
                await asyncio.sleep(0.05)
                assert n < 400_000
            await asyncio.sleep(0.3)
            assert fb.cancelled == [] and not task.done()

            edit_grant(home, schemes=("Other",))   # neither job is granted any more
            await until(lambda: a["job_id"] in fb.cancelled, timeout=3)
            assert fb.started == [a["job_id"]]     # B did not start in A's place
        finally:
            for t in (ka, task):
                if t is not None:
                    t.cancel()
            await asyncio.gather(*(t for t in (ka, task) if t is not None), return_exceptions=True)
            await srv.stop()
    arun(main(), timeout=60)
