"""`bh pair`, `bh unpair` and `bh rotate-key`: short-lived connections that change this Mac's identity.

Pairing writes the device key and the [server] pins only after the server proved the key from the pairing
string (PROTOCOL 3.3), and resets capabilities and grants: pairing grants nothing. A one-shot
connection authenticates like the daemon, so the server supersedes the daemon's connection (4409); the
daemon exits and its LaunchAgent restarts it with the new state.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from . import __version__, caps as capsmod, config, keyfile
from ._shared import bhproto
from .config import Config, ServerPin
from .connection import (AiohttpTransport, Closed, ConnectFailed, DeviceInfo, Session, Stop, Timing,
                         handshake_auth, handshake_pair, now_ms)
from .daemon import DB_NAME, daemon_running

ACK_TIMEOUT_S = 5.0
ROTATE_TIMEOUT_S = 10.0


def TELL_SERVER_S(timing: Timing) -> float:
    """The longest `bh unpair` waits on the server before it deletes locally: each bounded step, and a margin."""
    return timing.connect_s + timing.handshake_s + ACK_TIMEOUT_S + timing.close_s + 2.0


def _host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url


def _delete_jobs_db(home: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(home / (DB_NAME + suffix))
        except FileNotFoundError:
            pass


async def _hello_frames(session: Session, t, name: str, active=()) -> None:
    """PROTOCOL 1.3 step 8: caps at seq 1, jobs.sync at seq 2. A one-shot advertises nothing; the daemon
    re-sends the real caps when it reconnects. `active` lists the jobs still running here, so the server
    does not declare them lost (PROTOCOL 4.5)."""
    h = capsmod.host()
    caps = {"helper_version": __version__, "name": name, "os": "macos", "os_version": h.os_version, "arch": h.arch,
            "busy": False, "queue_depth": 0, "capabilities": []}
    for type_, body in (("caps", caps), ("jobs.sync", {"active": list(active)[:100], "results": []})):
        text, _ = session.encode(type_, body)
        await t.send(text)


async def _await_reply(session: Session, t, fid: str, timeout: float) -> Optional[str]:
    """Wait for `ack` or `error` answering frame `fid`; other frames are ignored (the daemon handles them)."""
    async def loop():
        while True:
            f = session.decode(await t.recv())
            if f.re == fid and f.type in ("ack", "error"):
                return f.type
    try:
        return await asyncio.wait_for(loop(), timeout)
    except (asyncio.TimeoutError, Closed, bhproto.ProtocolError):
        return None
    except Exception:
        return None


async def pair(home: Path, pairing: str, name: Optional[str] = None, insecure_local: bool = False, *,
               connect=AiohttpTransport.connect, timing: Timing = Timing(),
               host: Callable[[], capsmod.Host] = capsmod.host, out=print) -> int:
    """SPEC 8.4 exit codes: 0 ok; 2 rejected or expired code; 3 server key proof failed; 4 cannot connect."""
    try:
        endpoint, code, server_pub = bhproto.parse_pairing(pairing.strip(), allow_insecure_local=insecure_local)
    except bhproto.ProtocolError as e:
        out(f"that is not a valid pairing string ({e}); copy the whole bh1:... line from /hands pair")
        return 2
    keyfile.ensure_private_dir(home)
    if daemon_running(home):
        out("the helper is running; stop it first with 'bh service uninstall', then pair again")
        return 1
    device_name = config.sanitize_device_name(name) if name else config.default_device_name()
    h = host()
    priv = bhproto.new_private_key()               # kept in memory until the server proves its key
    try:
        t = await connect(endpoint, timing)
    except ConnectFailed as e:
        out(f"cannot reach {_host_of(endpoint)}: {e}")
        return 4
    try:
        helper_id, session = await handshake_pair(
            t, endpoint, code, server_pub, priv, DeviceInfo(device_name, __version__, h.os_version, h.arch), timing)
    except Stop as s:
        await t.close(1000)
        out(s.message)
        return s.exit_code
    except (Closed, bhproto.ProtocolError, asyncio.TimeoutError, OSError) as e:
        await t.close(1000)
        out(f"cannot reach {_host_of(endpoint)}: {e}")
        return 4
    # Proof verified: only now persist anything.
    keyfile.replace_device_key(home, priv)
    _delete_jobs_db(home)
    cfg = replace(Config.empty(device_name), server=ServerPin(endpoint, server_pub, bhproto.key_id(server_pub),
                                                              helper_id))
    config.save(home, cfg)
    try:
        await _hello_frames(session, t, device_name)
    except (Closed, OSError):
        pass
    await t.close(1000)
    out(f"Paired as '{device_name}' (helper {helper_id[:8]}). Nothing is enabled yet: turn on what you want "
        f"with 'bh caps enable ...' and 'bh grant add ...' (or keep going in 'bh setup').")
    return 0


def _active(home: Path) -> list:
    if not (home / DB_NAME).exists():
        return []
    from .jobstore import JobStore
    s = JobStore(home / DB_NAME)
    try:
        return [{"job_id": r.job_id, "state": r.state} for r in s.active()]
    finally:
        s.close()


async def _one_shot(home: Path, connect, timing: Timing):
    cfg = config.load(home)
    if cfg.server is None or not keyfile.has_device_key(home):
        raise Stop(3, "this Mac is not paired")
    priv = keyfile.load_device_key(home)
    t = await connect(cfg.server.url, timing)
    try:
        session = await handshake_auth(t, cfg.server, priv, timing)
        await _hello_frames(session, t, cfg.device_name, _active(home))
    except BaseException:
        await t.close(1000)
        raise
    return cfg, priv, session, t


async def unpair(home: Path, *, connect=AiohttpTransport.connect, timing: Timing = Timing(), out=print,
                 helper_stopped=lambda wait: True, stop_hint: str = "") -> int:
    """PROTOCOL 3.5 from the Mac: tell the server if we can (5 s), then always delete local state.

    `helper_stopped(wait)`: True only when no helper process is alive (the caller checks the helper lock; with
    `wait` it may wait for one that is stopping). A live helper keeps the key, pin and grants in memory whatever
    is deleted on disk, so without that this never says the pairing is deleted and returns 1."""
    told = False

    async def tell() -> bool:
        cfg, _, session, t = await _one_shot(home, connect, timing)
        try:
            text, fid = session.encode("unpair", {})
            await t.send(text)
            return await _await_reply(session, t, fid, ACK_TIMEOUT_S) == "ack"
        finally:
            await t.close(1000)
    try:
        # One deadline over everything that waits on the server, so the deletion below never depends on it
        told = await asyncio.wait_for(tell(), TELL_SERVER_S(timing))
    except (Stop, ConnectFailed, Closed, bhproto.ProtocolError, asyncio.TimeoutError, OSError,
            keyfile.UnsafeKeyStorage, config.ConfigError) as e:
        out(f"could not reach the server ({e}); removing this Mac's pairing locally anyway")
    alive = (" But a helper is still running on this Mac, or could not be checked, and it holds the key, pin and "
             "grants in memory until it stops, so it can still connect and build. Stop it (Ctrl-C for 'bh run'"
             + (f"; {stop_hint}" if stop_hint else "") + "), then run 'bh unpair' again to confirm.")
    keyfile.delete_device_key(home)
    _delete_jobs_db(home)
    try:
        config.update(home, lambda cfg: replace(cfg, server=None, capabilities=config.Capabilities(), workspaces=()))
    except (config.ConfigError, keyfile.UnsafeKeyStorage, OSError) as e:
        # Busy, unusable lock (symlink, wrong owner, unopenable), damaged or unreadable config.toml, unsafe
        # folder, or any other OS error from the config transaction: say what
        # was and was not removed; never claim the pins and grants are gone.
        out(f"The device key and job history are deleted, but the server pin and grants in config.toml could not "
            f"be cleared: {e}. Fix that, then run 'bh unpair' again."
            + ("" if helper_stopped(False) else alive)
            + ("" if told else " The server was not told; remove the stale entry there with /hands unpair."))
        return 1
    if not helper_stopped(True):
        out("The device key, server pins, grants and job history are deleted from disk." + alive
            + ("" if told else " The server was not told; remove the stale entry there with /hands unpair."))
        return 1
    out("Unpaired: the device key, server pins, grants and job history on this Mac are deleted."
        + ("" if told else " The server was not told; remove the stale entry there with /hands unpair."))
    return 0


async def rotate_key(home: Path, *, connect=AiohttpTransport.connect, timing: Timing = Timing(), out=print) -> int:
    """PROTOCOL 3.6: the new key signs C({helper_id,new_pubkey,sid}); the frame is signed by the old key.
    The new key is staged in a 0600 file before it is sent, and replaces the old one only after the ack."""
    try:
        cfg, _, session, t = await _one_shot(home, connect, timing)
    except (Stop, ConnectFailed, Closed, bhproto.ProtocolError, asyncio.TimeoutError, OSError) as e:
        out(f"cannot rotate while the server is unreachable: {e}")
        return 1
    staged = home / (keyfile.KEY_NAME + ".next")
    new = bhproto.new_private_key()
    try:
        keyfile.write_private_file(staged, (bhproto.b64u(bhproto.seed_of(new)) + "\n").encode())
        npub = bhproto.b64u(bhproto.pub_raw(new))
        sig_new = bhproto.sign(new, bhproto.CTX_ROTATE_DEVICE, bhproto.canonical(
            {"helper_id": cfg.server.helper_id, "new_pubkey": npub, "sid": session.sid}))
        text, fid = session.encode("device.rotate", {"new_pubkey": npub, "sig_new": sig_new})
        await t.send(text)
        if await _await_reply(session, t, fid, ROTATE_TIMEOUT_S) != "ack":
            out("the server did not confirm the new key; keeping the old one")
            return 1
        keyfile.replace_device_key(home, new)
        out("Device key rotated. The helper reconnects with the new key.")
        return 0
    finally:
        try:
            os.unlink(staged)
        except FileNotFoundError:
            pass
        await t.close(1000)
