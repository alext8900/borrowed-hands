"""`bh unpair` deletes this Mac's pairing however the paired server behaves: an endpoint that never answers the
WebSocket upgrade, and a server that acks and then withholds its CLOSE while it keeps the line busy, each cost a
bounded wait and nothing more (security review round 3, B1)."""
import asyncio
import time
from dataclasses import replace

from borrowed_hands_helper import account, config, keyfile
from fake_server import FakeServer
from test_daemon import FAST, arun, pair_home


def _gone(home):
    cfg = config.load(home)
    return not keyfile.has_device_key(home) and cfg.server is None and list(cfg.workspaces) == []


def test_an_endpoint_that_never_answers_the_upgrade_cannot_hold_bh_unpair(tmp_path):
    async def main():
        srv = await FakeServer().start()

        async def silent(reader, writer):              # accepts, reads the request, says nothing
            await reader.read(65536)
            await asyncio.sleep(3600)
        quiet = await asyncio.start_server(silent, "127.0.0.1", 0)
        try:
            home = await pair_home(tmp_path, srv)
            port = quiet.sockets[0].getsockname()[1]   # the paired server, now answering like this
            config.update(home, lambda c: replace(c, server=replace(c.server, url=f"ws://127.0.0.1:{port}/bh/v1/ws")))
            said = []
            t0 = time.monotonic()
            cmd = asyncio.create_task(account.unpair(home, timing=FAST, out=said.append))
            done, _ = await asyncio.wait({cmd}, timeout=FAST.connect_s + 6.0)
            assert done, "bh unpair is still waiting for the upgrade answer"
            assert time.monotonic() - t0 >= FAST.connect_s * 0.9          # it did wait for the server first
            assert cmd.result() == 0 and _gone(home)
            assert any("could not reach the server" in s for s in said)
            assert any("The server was not told" in s for s in said)
        finally:
            quiet.close()
            await srv.stop()
    arun(main(), timeout=60)


def test_a_server_that_acks_unpair_and_never_closes_cannot_hold_bh_unpair(tmp_path):
    async def main():
        srv = await FakeServer().start()
        hostile = None
        try:
            home = await pair_home(tmp_path, srv)
            srv.auto_ack = False                       # this server answers by hand

            async def hostile_server():
                p = await srv.next("unpair", timeout=60)
                srv.conn.ws._req.transport.pause_reading()            # it never sees, or answers, the CLOSE
                await srv.send("ack", {}, re=p["id"])
                while True:                                           # and it keeps the line busy
                    await asyncio.sleep(1.0)
                    await srv.send("ack", {})
            hostile = asyncio.create_task(hostile_server())
            said = []
            cmd = asyncio.create_task(account.unpair(home, timing=FAST, out=said.append))
            done, _ = await asyncio.wait({cmd}, timeout=FAST.close_s + 6.0)
            assert done, "bh unpair is still waiting for the server's CLOSE"
            assert cmd.result() == 0 and _gone(home)
            assert any(s.startswith("Unpaired:") for s in said) and not any("was not told" in s for s in said)
        finally:
            if hostile is not None:
                hostile.cancel()
            await srv.stop()
    arun(main(), timeout=120)


def test_a_transport_that_never_returns_from_anything_cannot_hold_bh_unpair(tmp_path):
    """The deletion does not depend on how the transport behaves: one deadline covers telling the server."""
    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)

            async def never(url, timing):
                await asyncio.sleep(3600)
            said = []
            cmd = asyncio.create_task(account.unpair(home, connect=never, timing=FAST, out=said.append))
            done, _ = await asyncio.wait({cmd}, timeout=account.TELL_SERVER_S(FAST) + 6.0)
            assert done, "bh unpair is still waiting on the transport"
            assert cmd.result() == 0 and _gone(home)
            assert any("could not reach the server" in s for s in said)
        finally:
            await srv.stop()
    arun(main(), timeout=120)
