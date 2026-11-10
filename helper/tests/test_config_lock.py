"""Every config.toml write is one transaction: load, check, change and write under one exclusive lock
(config.lock), so a stale Config is never written over a newer one (Review round 1, F1)."""
from __future__ import annotations

import ast
import os
import stat
import threading
import time
from pathlib import Path

import pytest

import helpers as H
from borrowed_hands_helper import config, keyfile
from test_cli import GRANT, World, _cfg_bytes, bh, paired

PKG = Path(config.__file__).parent


class Gate:
    """Pause the thread named `name` right after it loads config.toml while holding the lock."""

    def __init__(self, monkeypatch, name):
        self.loaded, self.proceed, self.name = threading.Event(), threading.Event(), name
        real = config.load

        def gated(d=config.DEFAULT_DIR):
            cfg = real(d)
            if threading.current_thread().name == self.name and config.lock_held(d):
                self.loaded.set()
                assert self.proceed.wait(10)
            return cfg
        monkeypatch.setattr(config, "load", gated)


def _start(name, world, argv, out):
    t = threading.Thread(target=lambda: out.__setitem__(name, bh(world, *argv)), name=name)
    t.start()
    return t


def test_two_adds_at_once_cannot_silently_broaden(tmp_path, monkeypatch):
    wa, wb = World(tmp_path), World(tmp_path)          # one config home, two `bh` commands
    paired(wa)
    gate, rc = Gate(monkeypatch, "A"), {}
    a = _start("A", wa, GRANT, rc)                     # demo: scheme Demo, confirm on
    assert gate.loaded.wait(10)
    b = _start("B", wb, GRANT[:-3] + ["--scheme", "Other"], rc)     # broader: + Other, confirm off, no expiry
    time.sleep(0.3)
    assert b.is_alive(), "the second command must wait for the lock, not read the config under it"
    gate.proceed.set()
    a.join(10), b.join(10)
    assert rc == {"A": 0, "B": 2}, wa.text() + wb.text()
    assert "already exists" in wb.errs[0]
    ws = config.load(wa.env.home).workspace("demo")
    assert ws.schemes == ("Demo",) and ws.confirm


def test_a_stale_unrelated_write_cannot_resurrect_a_tightened_grant(tmp_path, monkeypatch):
    wa, wb = World(tmp_path), World(tmp_path)
    paired(wa)
    assert bh(wa, *GRANT[:-3], "--scheme", "Other") == 0          # demo: Demo + Other, confirm off
    gate, rc = Gate(monkeypatch, "A"), {}
    other = [x if x != "demo" else "other" for x in GRANT]
    a = _start("A", wa, other, rc)                                 # adds an unrelated grant
    assert gate.loaded.wait(10)
    b = _start("B", wb, GRANT[:-3] + ["--confirm", "--replace"], rc)  # the owner tightens demo
    time.sleep(0.3)
    assert b.is_alive()
    gate.proceed.set()
    a.join(10), b.join(10)
    assert rc == {"A": 0, "B": 0}, wa.text() + wb.text()
    cfg = config.load(wa.env.home)
    assert cfg.workspace("demo").schemes == ("Demo",) and cfg.workspace("demo").confirm
    assert cfg.workspace("other") is not None


def test_a_held_lock_times_out_with_a_clear_message_and_changes_nothing(tmp_path, monkeypatch):
    w = World(tmp_path)
    paired(w)
    before = _cfg_bytes(w)
    monkeypatch.setattr(config, "LOCK_TIMEOUT_S", 0.2)
    holder_ready, release = threading.Event(), threading.Event()

    def hold():
        with config.locked(w.env.home):
            holder_ready.set()
            release.wait(10)
    t = threading.Thread(target=hold)
    t.start()
    assert holder_ready.wait(10)
    try:
        assert bh(w, *GRANT) != 0
        assert bh(w, "caps", "enable", "system.info") != 0
    finally:
        release.set()
        t.join(10)
    assert _cfg_bytes(w) == before
    assert any("another 'bh' command or the helper is changing config.toml" in e for e in w.errs), w.errs


def test_the_lock_file_is_private_and_separate_from_config(tmp_path):
    home = tmp_path / "BorrowedHands"
    keyfile.ensure_private_dir(home)
    with config.locked(home):
        assert config.lock_held(home)
        st = os.lstat(home / config.LOCK_NAME)
        assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600
    assert not config.lock_held(home)
    assert not (home / config.CONFIG_NAME).exists()


def test_the_lock_file_must_not_be_a_symlink(tmp_path):
    home = tmp_path / "BorrowedHands"
    keyfile.ensure_private_dir(home)
    os.symlink(tmp_path / "elsewhere", home / config.LOCK_NAME)
    with pytest.raises(config.ConfigError):
        with config.locked(home):
            pass
    assert not (tmp_path / "elsewhere").exists()


def test_writing_without_the_lock_is_refused(tmp_path):
    home = tmp_path / "BorrowedHands"
    keyfile.ensure_private_dir(home)
    with pytest.raises(RuntimeError):
        config._write(home, config.Config.empty("Mac"))
    assert not (home / config.CONFIG_NAME).exists()


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            yield node, node.func.attr, getattr(node.func.value, "id", None)


def test_no_production_writer_bypasses_the_locked_update():
    """config.update is the only way production code changes config.toml. config.save (a locked write of a
    whole new Config, nothing loaded first) is allowed only where pairing starts a fresh config."""
    offenders = []
    for src in sorted(PKG.rglob("*.py")):
        if src.name == "config.py":
            continue
        text = src.read_text()
        tree = ast.parse(text)
        parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        for node, attr, base in _calls(tree):
            if base == "config" and attr in ("save", "_write", "dump"):
                fn = node
                while fn in parents and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn = parents[fn]
                where = getattr(fn, "name", "<module>")
                if not (src.name == "account.py" and attr == "save" and where == "pair"):
                    offenders.append(f"{src.name}:{node.lineno} config.{attr} in {where}")
        if "CONFIG_NAME" in text:
            offenders.append(f"{src.name}: names CONFIG_NAME")
    assert offenders == []


def test_every_production_writer_uses_the_lock_at_run_time(tmp_path, monkeypatch):
    """Spy: every write that happens in the CLI paths of this suite happens with the lock held."""
    seen = []
    real = config._write

    def spy(d, cfg):
        seen.append(config.lock_held(d))
        return real(d, cfg)
    monkeypatch.setattr(config, "_write", spy)
    w = World(tmp_path)
    paired(w)
    assert bh(w, *GRANT) == 0
    assert bh(w, "caps", "enable", "system.info") == 0
    assert bh(w, "grant", "revoke", "demo") == 0
    assert seen and all(seen)


def test_daemon_key_rotation_does_not_write_back_its_older_grants(tmp_path):
    """The daemon's in-memory Config can be older than config.toml (its watcher polls). Rotation must change
    only the server pin of what is on disk now, never write its older grants back."""
    import asyncio
    from fake_server import FakeServer
    from borrowed_hands_helper._shared import bhproto
    from test_daemon import arun, deps, pair_home, start, stop

    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)                  # demo: confirm off
            task = await start(home, srv, deps(config_poll_s=60))  # the watcher will not reload in this test
            tight = config.update(home, lambda c: c.with_workspace(H.workspace(confirm=True), replace_existing=True))
            new = bhproto.new_private_key()
            npub = bhproto.pub_raw(new)
            nkid = bhproto.key_id(npub)
            sig_new = bhproto.sign(new, bhproto.CTX_ROTATE_SERVER, bhproto.canonical(
                {"new_key_id": nkid, "new_pubkey": bhproto.b64u(npub), "old_key_id": srv.kid}))
            await srv.send("server.rotate", {"new_pubkey": bhproto.b64u(npub), "new_key_id": nkid,
                                             "sig_new": sig_new})
            await srv.next("ack")
            on_disk = config.load(home)
            assert on_disk.server.key_id == nkid
            assert on_disk.workspaces == tight.workspaces and on_disk.workspace("demo").confirm
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def _broad_then_tight(home):
    """demo on disk allows Demo + Other; returns the change that tightens it to Demo only."""
    config.update(home, lambda c: c.with_workspace(H.workspace(schemes=("Demo", "Other")), replace_existing=True))
    return lambda c: c.with_workspace(H.workspace(schemes=("Demo",)), replace_existing=True)


def test_rotation_never_records_an_mtime_it_did_not_load(tmp_path, monkeypatch):
    """Schedule: rotation writes the new pin; the owner tightens a grant before the daemon notes the file's
    mtime. The daemon must end up on the tightened grant, not skip the reload."""
    import asyncio
    from fake_server import FakeServer
    from borrowed_hands_helper._shared import bhproto
    from borrowed_hands_helper.daemon import Daemon
    from test_daemon import arun, deps, pair_home, stop, until

    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            tighten = _broad_then_tight(home)
            d = Daemon(home, deps(config_poll_s=0.05))
            task = asyncio.create_task(d.run())
            await srv.next("caps")
            await srv.next("jobs.sync")
            assert d.cfg.workspace("demo").schemes == ("Demo", "Other")
            real, armed = config.update, [True]

            def update(dd, change):
                out = real(dd, change)
                if armed:
                    armed.pop()
                    real(dd, tighten)          # lands after the rotation's write, before anything after it
                return out
            monkeypatch.setattr(config, "update", update)
            new = bhproto.new_private_key()
            npub = bhproto.pub_raw(new)
            nkid = bhproto.key_id(npub)
            sig_new = bhproto.sign(new, bhproto.CTX_ROTATE_SERVER, bhproto.canonical(
                {"new_key_id": nkid, "new_pubkey": bhproto.b64u(npub), "old_key_id": srv.kid}))
            await srv.send("server.rotate", {"new_pubkey": bhproto.b64u(npub), "new_key_id": nkid,
                                             "sig_new": sig_new})
            await srv.next("ack")
            assert not armed
            srv.priv, srv.pub, srv.kid = new, npub, nkid      # the server now signs with the new key
            await srv.close_helper(1001)
            await until(lambda: srv.auths == 2)
            await until(lambda: d.cfg.workspace("demo").schemes == ("Demo",), timeout=3)
            assert d.cfg.server.key_id == nkid and not task.done()
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_startup_never_records_an_mtime_newer_than_what_it_loaded(tmp_path, monkeypatch):
    """Schedule: the daemon loads config.toml at start; a grant is tightened before it notes the mtime. It must
    still end up on the tightened grant."""
    import asyncio
    from fake_server import FakeServer
    from borrowed_hands_helper.daemon import Daemon
    from test_daemon import arun, deps, pair_home, stop, until

    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            tighten = _broad_then_tight(home)
            real_load, real_update, armed = config.load, config.update, [True]

            def load(dd=config.DEFAULT_DIR):
                cfg = real_load(dd)
                if armed and not config.lock_held(dd):
                    armed.pop()
                    real_update(dd, tighten)    # the owner's write lands right after the daemon's load
                return cfg
            monkeypatch.setattr(config, "load", load)
            d = Daemon(home, deps(config_poll_s=0.05))
            task = asyncio.create_task(d.run())
            await srv.next("caps")
            await srv.next("jobs.sync")
            assert not armed
            await until(lambda: d.cfg.workspace("demo").schemes == ("Demo",), timeout=3)
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_a_rotated_file_the_watcher_rejected_is_still_loaded_after_the_pin_is_installed(tmp_path, monkeypatch):
    """Schedule (Review round 2, F4b): rotation's locked write also carries a revoke the daemon has not loaded.
    While rotation awaits its worker thread, the watcher sees the new file, rejects it (its pin differs from
    memory) and notes its mtime. Rotation then installs the pin. The daemon must still load that file, drop the
    revoked grant and cancel the queued job under it through the normal cancellation path."""
    import asyncio
    from fake_server import FakeServer
    from borrowed_hands_helper._shared import bhproto
    from borrowed_hands_helper.daemon import Daemon
    from test_daemon import arun, deps, pair_home, stop, until
    from test_fix_round1 import RecordingBuild, results_for, two_builds

    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = RecordingBuild()
            d = Daemon(home, deps(fb, config_poll_s=0.05))
            task = asyncio.create_task(d.run())
            await srv.next("caps")
            await srv.next("jobs.sync")
            a, b = await two_builds(srv, home, fb)              # a runs (blocked), b is queued under demo
            real, armed, rejected = config.update, [True], []

            def update(dd, change):
                if not armed:
                    return real(dd, change)
                armed.pop()
                out = real(dd, lambda c: change(c.without_workspace("demo")))   # revoke + new pin, one write
                end = time.monotonic() + 5
                while time.monotonic() < end and d._rejected_mtime != config.mtime(dd):
                    time.sleep(0.01)                              # let the watcher see (and reject) it first
                rejected.append(d._rejected_mtime == config.mtime(dd) and d.cfg.workspace("demo") is not None)
                return out
            monkeypatch.setattr(config, "update", update)
            new = bhproto.new_private_key()
            npub = bhproto.pub_raw(new)
            nkid = bhproto.key_id(npub)
            sig_new = bhproto.sign(new, bhproto.CTX_ROTATE_SERVER, bhproto.canonical(
                {"new_key_id": nkid, "new_pubkey": bhproto.b64u(npub), "old_key_id": srv.kid}))
            await srv.send("server.rotate", {"new_pubkey": bhproto.b64u(npub), "new_key_id": nkid,
                                             "sig_new": sig_new})
            await srv.next("ack")
            assert rejected == [True], "the schedule did not happen: the watcher never saw the rotated file"
            srv.priv, srv.pub, srv.kid = new, npub, nkid
            await srv.close_helper(1001)
            await until(lambda: srv.auths == 2)
            await until(lambda: d.cfg.workspace("demo") is None, timeout=3)
            fb.release(a["job_id"])
            res = await results_for(srv, {b["job_id"]})
            assert res[b["job_id"]]["state"] == "cancelled"
            assert res[b["job_id"]]["error"]["detail"]["reason"] == "grant_revoked"
            assert b["job_id"] not in fb.jobs
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


# ---------------------------------------------------------------- unpair when config.toml cannot be updated (F5)
def _offline(monkeypatch):
    from borrowed_hands_helper import account
    from borrowed_hands_helper.connection import Stop

    async def one_shot(*a, **k):
        raise Stop(3, "offline fixture")
    monkeypatch.setattr(account, "_one_shot", one_shot)


def _granted_world(tmp_path):
    w = World(tmp_path)
    paired(w)
    assert bh(w, *GRANT) == 0
    w.out.clear(), w.errs.clear()
    return w


def _break_symlink(home, tmp_path):
    lock = home / config.LOCK_NAME
    if os.path.lexists(lock):
        os.unlink(lock)                    # the real lock file left by an earlier command
    os.symlink(tmp_path / "elsewhere", lock)
    return tmp_path / "elsewhere"


def _break_mode000(home, tmp_path):
    p = home / config.LOCK_NAME
    p.touch()
    os.chmod(p, 0)
    return None


def _break_toml(home, tmp_path):
    p = home / config.CONFIG_NAME
    os.chmod(p, 0o600)
    p.write_bytes(p.read_bytes() + b"\n[[[ not toml\n")
    return None


@pytest.mark.parametrize("breaker", [_break_symlink, _break_mode000, _break_toml])
def test_unpair_reports_partial_cleanup_when_config_cannot_be_updated(tmp_path, monkeypatch, breaker):
    _offline(monkeypatch)
    w = _granted_world(tmp_path)
    home = w.env.home
    target = breaker(home, tmp_path)
    before = _cfg_bytes(w)
    try:
        rc = bh(w, "unpair")
    finally:
        if breaker is _break_mode000:
            os.chmod(home / config.LOCK_NAME, 0o600)
    text = w.text()
    assert rc == 1, text
    assert not any(line.startswith("Unpaired:") for line in w.out), text
    assert any(line.startswith("The device key and job history are deleted, but the server pin and grants in "
                               "config.toml could not be cleared: ") for line in w.out), text
    assert not (home / keyfile.KEY_NAME).exists()
    assert _cfg_bytes(w) == before                                  # nothing was half-written
    if target is not None:
        assert not os.path.lexists(target)                          # the symlink was never followed


def test_unpair_busy_lock_reports_partial_cleanup(tmp_path, monkeypatch):
    _offline(monkeypatch)
    monkeypatch.setattr(config, "LOCK_TIMEOUT_S", 0.2)
    w = _granted_world(tmp_path)
    before = _cfg_bytes(w)
    ready, release = threading.Event(), threading.Event()

    def hold():
        with config.locked(w.env.home):
            ready.set()
            release.wait(10)
    t = threading.Thread(target=hold)
    t.start()
    assert ready.wait(10)
    try:
        rc = bh(w, "unpair")
    finally:
        release.set()
        t.join(10)
    assert rc == 1 and _cfg_bytes(w) == before
    assert not any(line.startswith("Unpaired:") for line in w.out)
    assert any("could not be cleared" in line and "is changing config.toml" in line for line in w.out), w.text()


def test_unpair_still_succeeds_when_config_can_be_updated(tmp_path, monkeypatch):
    _offline(monkeypatch)
    w = _granted_world(tmp_path)
    assert bh(w, "unpair") == 0
    assert any(line.startswith("Unpaired:") for line in w.out)
    cfg = config.load(w.env.home)
    assert cfg.server is None and cfg.workspaces == ()


# ---------------------------------------------------------------- unpair under a live helper (Security review round 1, A)
def test_unpair_with_a_live_helper_does_not_say_deleted(tmp_path, monkeypatch):
    """A helper that survives ('bh run' in a terminal) still has the key, pin and grants in memory: the on-disk
    state is deleted anyway, but the command says so and fails."""
    _offline(monkeypatch)
    w = _granted_world(tmp_path)
    w.running = True
    rc = bh(w, "unpair")
    text = w.text()
    assert rc == 1, text
    assert not any(line.startswith("Unpaired:") for line in w.out), text
    assert "still running" in text and "in memory" in text and "Ctrl-C" in text, text
    assert not (w.env.home / keyfile.KEY_NAME).exists()             # the local delete still happened
    assert config.load(w.env.home).server is None
    assert w.svc.calls == ["uninstall"]


def test_unpair_when_the_service_stop_fails_does_not_say_deleted(tmp_path, monkeypatch):
    _offline(monkeypatch)
    w = _granted_world(tmp_path)
    w.svc.stop_result = (False, "launchctl bootout failed; the service is still loaded")
    rc = bh(w, "unpair")
    text = w.text()
    assert rc == 1, text
    assert not any(line.startswith("Unpaired:") for line in w.out), text
    assert "still loaded" in text and "in memory" in text, text
    assert config.load(w.env.home).server is None


def test_unpair_when_the_helper_lock_cannot_be_checked_does_not_say_deleted(tmp_path, monkeypatch):
    _offline(monkeypatch)
    w = _granted_world(tmp_path)
    w.running = None
    rc = bh(w, "unpair")
    assert rc == 1 and not any(line.startswith("Unpaired:") for line in w.out), w.text()


def test_unpair_stops_the_service_before_deleting(tmp_path, monkeypatch):
    """The service is asked to stop first, so a LaunchAgent helper is already draining when the key goes."""
    _offline(monkeypatch)
    w = _granted_world(tmp_path)
    seen = []
    real = w.svc.uninstall

    def uninstall(**k):
        seen.append((w.env.home / keyfile.KEY_NAME).exists())
        return real(**k)
    w.svc.uninstall = uninstall
    assert bh(w, "unpair") == 0
    assert seen == [True]


def test_uninstall_with_a_symlinked_lock_still_removes_everything(tmp_path, monkeypatch):
    from test_cli import TEARDOWN, uninstall_world
    _offline(monkeypatch)
    w = uninstall_world(tmp_path)
    paired(w)
    assert bh(w, *GRANT) == 0
    target = _break_symlink(w.env.home, tmp_path)
    rc = bh(w, "uninstall")
    text = w.text()
    assert "could not be cleared" in text                          # unpair says what it could not do
    assert TEARDOWN in w.sudo_calls and any(c[:2] == ["/bin/rm", "-f"] for c in w.sudo_calls), text
    assert "Removal check:" in text
    assert not w.env.home.exists() and not os.path.lexists(target)
    assert rc == 0, text                                             # the folder itself was removed afterwards


# ---------------------------------------------------------------- a config version counts as applied only when done (F6)
def _job_state(d, job_id):
    row = d.store.get(job_id)
    return row.state if row else None


@pytest.mark.parametrize("mode", [0o640, 0o000])
def test_a_revocation_the_watcher_could_not_read_is_applied_once_the_file_is_readable(tmp_path, mode, caplog):
    """Review round 3, F6: the watcher must not mark a version as loaded when reading it failed. Real chmod: the
    revoked file is unreadable (0640: refused as unsafe; 000: PermissionError), the daemon keeps its last good
    config and says so once; chmod 600 (which does not change the mtime) must then get the revocation applied and
    the jobs under the revoked grant cancelled."""
    import asyncio
    import logging
    from fake_server import FakeServer
    from borrowed_hands_helper.daemon import Daemon
    from test_daemon import arun, deps, pair_home, stop, until
    from test_fix_round1 import RecordingBuild, two_builds

    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = RecordingBuild()
            d = Daemon(home, deps(fb, config_poll_s=0.05))
            task = asyncio.create_task(d.run())
            await srv.next("caps")
            await srv.next("jobs.sync")
            a, b = await two_builds(srv, home, fb)              # a runs, b is queued, both under demo
            cfg_path = home / config.CONFIG_NAME
            # no await between these two: the watcher cannot see the revoked file while it is still readable
            config.update(home, lambda c: c.without_workspace("demo"))
            os.chmod(cfg_path, mode)
            try:
                await asyncio.sleep(0.4)                         # several polls on the unreadable file
                assert d.cfg.workspace("demo") is not None       # last good config kept
                assert "config error" in (d._status.get("detail") or "")
                m = os.stat(cfg_path).st_mtime_ns
            finally:
                os.chmod(cfg_path, 0o600)
            assert os.stat(cfg_path).st_mtime_ns == m            # the repair did not touch the mtime
            await until(lambda: d.cfg.workspace("demo") is None, timeout=3)
            await until(lambda: _job_state(d, b["job_id"]) == "cancelled", timeout=3)
            await until(lambda: _job_state(d, a["job_id"]) == "cancelled", timeout=3)
            assert b["job_id"] not in fb.jobs
            invalid = [r for r in caplog.records if "config.toml" in r.getMessage()
                       and r.levelno >= logging.ERROR and "invalid" in r.getMessage()]
            assert len(invalid) == 1, [r.getMessage() for r in invalid]   # once per version, not per poll
            fb.release(a["job_id"])
            await stop(task)
        finally:
            await srv.stop()
    caplog.set_level("ERROR")
    arun(main())


def test_a_failure_after_publishing_a_version_is_retried_until_its_jobs_are_cancelled(tmp_path):
    """F6, related: if processing a newly loaded version fails before its cancellations ran (here the capability
    probe raises once), the version stays pending and the next poll finishes it."""
    import asyncio
    from fake_server import FakeServer
    from borrowed_hands_helper.daemon import Daemon
    from test_daemon import GOOD, arun, deps, pair_home, stop, until
    from test_fix_round1 import RecordingBuild, two_builds

    fail = []

    def probes(*_a, **_k):
        if fail:
            fail.pop()
            raise OSError("probe failed once")
        return GOOD

    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            fb = RecordingBuild()
            d = Daemon(home, deps(fb, config_poll_s=0.05, probes=probes))
            task = asyncio.create_task(d.run())
            await srv.next("caps")
            await srv.next("jobs.sync")
            a, b = await two_builds(srv, home, fb)
            fail.append(True)
            config.update(home, lambda c: c.without_workspace("demo"))
            await until(lambda: not fail, timeout=3)               # the failing probe ran
            await until(lambda: _job_state(d, b["job_id"]) == "cancelled", timeout=3)
            await until(lambda: _job_state(d, a["job_id"]) == "cancelled", timeout=3)
            assert b["job_id"] not in fb.jobs
            fb.release(a["job_id"])
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def _break_config000(home, tmp_path):
    os.chmod(home / config.CONFIG_NAME, 0)
    return None


def test_unpair_with_an_unreadable_config_is_handled_and_still_stops_the_service(tmp_path):
    """Review round 3, F7: a raw OSError from the config transaction (real chmod 000 on config.toml) is the same
    partial cleanup: handled exit 1, the partial message, and the background service is still removed. The real
    _one_shot runs (its first read fails too, so nothing is sent anywhere)."""
    w = _granted_world(tmp_path)
    home = w.env.home
    _break_config000(home, tmp_path)
    try:
        rc = bh(w, "unpair")
    finally:
        os.chmod(home / config.CONFIG_NAME, 0o600)
    text = w.text()
    assert rc == 1, text
    assert not any(line.startswith("Unpaired:") for line in w.out), text
    assert any(line.startswith("The device key and job history are deleted, but the server pin and grants in "
                               "config.toml could not be cleared: ") for line in w.out), text
    assert "uninstall" in w.svc.calls
    assert not (home / keyfile.KEY_NAME).exists()
    assert config.load(home).workspace("demo") is not None         # nothing was half-written


# ---------------------------------------------------------------- watcher steps are independent (Review round 4)
class _FlakyCancel:
    """A RecordingBuild whose cancel raises while `failing` is set (models terminate() failing)."""

    def __new__(cls):
        from test_fix_round1 import RecordingBuild

        class Flaky(RecordingBuild):
            def __init__(self):
                super().__init__()
                self.failing, self.attempts, self.once = None, [], False

            async def cancel(self, job_id):
                self.attempts.append(job_id)
                if self.failing:
                    msg = self.failing
                    if self.once:
                        self.failing = None
                    raise PermissionError(1, msg)
                return await super().cancel(job_id)
        return Flaky()


async def _running_daemon(tmp_path, srv, fb, **kw):
    import asyncio
    from borrowed_hands_helper.daemon import Daemon
    from test_daemon import deps, pair_home
    home = await pair_home(tmp_path, srv)
    d = Daemon(home, deps(fb, **kw))
    task = asyncio.create_task(d.run())
    await srv.next("caps")
    await srv.next("jobs.sync")
    return home, d, task


def _messages(caplog, needle):
    return [r for r in caplog.records if needle in r.getMessage()]


@pytest.mark.parametrize("kind", ["toml"])
def test_capabilities_keep_refreshing_while_a_new_config_version_is_rejected(tmp_path, kind):
    """F8: a rejected config version stays pending, but the periodic capability refresh runs regardless."""
    import asyncio
    from dataclasses import replace
    from fake_server import FakeServer
    from borrowed_hands_helper import caps as capsmod
    from test_daemon import GOOD, FakeBuild, arun, stop, until

    degraded = []

    def probes(*_a, **_k):
        return replace(GOOD, xcode=None) if degraded else GOOD

    async def main():
        srv = await FakeServer().start()
        try:
            home, d, task = await _running_daemon(tmp_path, srv, FakeBuild(), config_poll_s=0.025,
                                                  probe_refresh_s=0.1, probes=probes)
            assert "xcode.build" in capsmod.usable(d.cfg, d.probes)
            if kind == "toml":
                with open(home / config.CONFIG_NAME, "ab") as f:
                    f.write(b"\n[[[ not toml\n")
            else:
                config.update(home, lambda c: replace(c, server=replace(
                    c.server, helper_id="99999999-2222-4333-8444-555555555555")))
            await until(lambda: d._rejected_mtime == config.mtime(home), timeout=3)
            degraded.append(True)                        # the host loses Xcode while the bad file sits there
            await until(lambda: d.probes.xcode is None, timeout=3)
            assert "xcode.build" not in capsmod.usable(d.cfg, d.probes)
            assert d.cfg.workspace("demo") is not None     # the last good config is still the one in use
            assert d.cfg_mtime != config.mtime(home)        # and the bad version is still pending
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_a_failed_cancel_of_a_running_job_is_retried_and_the_version_stays_pending(tmp_path):
    """F9: the executor's cancel raised after the reason was recorded. The job is not cancelled until a cancel
    call completes; the watcher retries it and only then counts the revoking version as applied."""
    import asyncio
    from fake_server import FakeServer
    from test_daemon import arun, stop, until
    from test_fix_round1 import two_builds

    async def main():
        srv = await FakeServer().start()
        try:
            fb = _FlakyCancel()
            home, d, task = await _running_daemon(tmp_path, srv, fb, config_poll_s=0.05)
            a, b = await two_builds(srv, home, fb)
            fb.failing = "terminate refused"
            config.update(home, lambda c: c.without_workspace("demo"))
            await until(lambda: len(fb.attempts) >= 2, timeout=3)     # retried, not skipped
            await asyncio.sleep(0.2)
            assert _job_state(d, a["job_id"]) == "building"
            assert d.cfg_mtime != config.mtime(home)                  # not applied while a cancel is unfinished
            assert _job_state(d, b["job_id"]) == "cancelled"
            fb.failing = None
            await until(lambda: _job_state(d, a["job_id"]) == "cancelled", timeout=3)
            await until(lambda: d.cfg_mtime == config.mtime(home), timeout=3)
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_a_failed_owner_cancel_is_retried(tmp_path):
    """F9, owner path: `bh cancel` on a running job whose executor cancel fails once is retried next poll."""
    import asyncio
    from fake_server import FakeServer
    from test_daemon import arun, stop, until
    from test_fix_round1 import two_builds

    async def main():
        srv = await FakeServer().start()
        try:
            fb = _FlakyCancel()
            home, d, task = await _running_daemon(tmp_path, srv, fb, config_poll_s=0.05)
            a, b = await two_builds(srv, home, fb)
            fb.failing = "terminate refused"
            assert d.store.request_cancel(a["job_id"])                # what `bh cancel` does
            await until(lambda: len(fb.attempts) >= 2, timeout=3)
            assert _job_state(d, a["job_id"]) == "building"
            fb.failing = None
            await until(lambda: _job_state(d, a["job_id"]) == "cancelled", timeout=3)
            fb.release(b["job_id"])
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_a_persistent_probe_failure_is_logged_once_per_distinct_error_and_once_on_recovery(tmp_path, caplog):
    """F10: retried every poll, but one log line per distinct error, and one when it works again."""
    import asyncio
    from fake_server import FakeServer
    from test_daemon import GOOD, FakeBuild, arun, stop, until

    state = {"err": None, "calls": 0}

    def probes(*_a, **_k):
        state["calls"] += 1
        if state["err"]:
            raise OSError(state["err"])
        return GOOD

    async def main():
        srv = await FakeServer().start()
        try:
            home, d, task = await _running_daemon(tmp_path, srv, FakeBuild(), config_poll_s=0.02,
                                                  probe_refresh_s=0.02, probes=probes)
            state["err"] = "probe down A"
            n = state["calls"]
            await until(lambda: state["calls"] >= n + 5, timeout=3)
            assert len(_messages(caplog, "probe down A")) == 1
            state["err"] = "probe down B"                         # a changed error is not hidden
            n = state["calls"]
            await until(lambda: state["calls"] >= n + 5, timeout=3)
            assert len(_messages(caplog, "probe down B")) == 1
            state["err"] = None
            n = state["calls"]
            await until(lambda: state["calls"] >= n + 3, timeout=3)
            assert len(_messages(caplog, "capability probe works again")) == 1
            assert len(_messages(caplog, "probe down")) == 2
            await stop(task)
        finally:
            await srv.stop()
    caplog.set_level("INFO")
    arun(main())


def test_a_persistent_cancel_failure_is_logged_once_and_once_on_recovery(tmp_path, caplog):
    import asyncio
    from fake_server import FakeServer
    from test_daemon import arun, stop, until
    from test_fix_round1 import two_builds

    async def main():
        srv = await FakeServer().start()
        try:
            fb = _FlakyCancel()
            home, d, task = await _running_daemon(tmp_path, srv, fb, config_poll_s=0.02)
            a, b = await two_builds(srv, home, fb)
            fb.failing = "terminate refused"
            config.update(home, lambda c: c.without_workspace("demo"))
            await until(lambda: len(fb.attempts) >= 6, timeout=3)
            assert len(_messages(caplog, "terminate refused")) == 1
            fb.failing = None
            await until(lambda: _job_state(d, a["job_id"]) == "cancelled", timeout=3)
            assert len(_messages(caplog, "cancel works again")) == 1
            await stop(task)
        finally:
            await srv.stop()
    caplog.set_level("INFO")
    arun(main())


# ---------------------------------------------------------------- caps go out only when they changed (Review round 5, F11)
async def _caps_bodies(srv, window):
    """Every caps body the server receives until `window` seconds pass with no frame at all."""
    import asyncio
    out = []
    while True:
        try:
            t, p = await asyncio.wait_for(srv.inbox.get(), window)
        except asyncio.TimeoutError:
            return out
        if t == "caps":
            out.append(p["body"])


def test_one_changed_caps_body_across_a_one_shot_probe_failure(tmp_path):
    import asyncio
    from fake_server import FakeServer
    from test_daemon import GOOD, FakeBuild, arun, stop, until

    fail = []

    def probes(*_a, **_k):
        if fail:
            fail.pop()
            raise OSError("probe failed once")
        return GOOD

    async def main():
        srv = await FakeServer().start()
        try:
            home, d, task = await _running_daemon(tmp_path, srv, FakeBuild(), config_poll_s=0.03, probes=probes)
            await _caps_bodies(srv, 0.15)                                  # nothing pending
            fail.append(True)
            config.update(home, lambda c: c.with_capability("xcode.build", False))
            await until(lambda: d.cfg_mtime == config.mtime(home), timeout=3)   # failed once, then applied
            assert not fail
            bodies = await _caps_bodies(srv, 0.3)
            assert len(bodies) == 1, bodies
            assert [c["name"] for c in bodies[0]["capabilities"]] == ["system.info"]
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_no_repeated_caps_body_across_a_one_shot_cancel_failure(tmp_path):
    import asyncio
    from fake_server import FakeServer
    from test_daemon import arun, stop, until
    from test_fix_round1 import two_builds

    async def main():
        srv = await FakeServer().start()
        try:
            fb = _FlakyCancel()
            home, d, task = await _running_daemon(tmp_path, srv, fb, config_poll_s=0.03)
            a, b = await two_builds(srv, home, fb)
            await _caps_bodies(srv, 0.15)
            fb.failing, fb.once = "terminate refused once", True
            config.update(home, lambda c: c.without_workspace("demo"))
            await until(lambda: d.cfg_mtime == config.mtime(home), timeout=3)
            assert len(fb.attempts) >= 2                                   # it failed once and was retried
            bodies = await _caps_bodies(srv, 0.3)
            # The revoke goes out once. The cancelled job then finishing really changes the body (busy, queue
            # depth), so a later frame is allowed, but never the same body twice.
            assert bodies and all(x != y for x, y in zip(bodies, bodies[1:])), bodies
            assert all(x["capabilities"] == bodies[0]["capabilities"] for x in bodies), bodies
            [xb] = [c for c in bodies[0]["capabilities"] if c["name"] == "xcode.build"]
            assert xb["workspaces"] == []
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_a_config_edit_that_does_not_change_caps_sends_no_caps_frame(tmp_path):
    import asyncio
    from dataclasses import replace
    from fake_server import FakeServer
    from test_daemon import FakeBuild, arun, stop, until

    async def main():
        srv = await FakeServer().start()
        try:
            home, d, task = await _running_daemon(tmp_path, srv, FakeBuild(), config_poll_s=0.03)
            await _caps_bodies(srv, 0.15)
            config.update(home, lambda c: replace(c, policy=replace(c.policy, build_timeout_s=1200)))
            await until(lambda: d.cfg_mtime == config.mtime(home), timeout=3)
            assert d.cfg.policy.build_timeout_s == 1200
            assert await _caps_bodies(srv, 0.3) == []
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


# ---------------------------------------------------------------- pairing removed under a running helper (Security review round 1)
def _rotate_frame(srv):
    from borrowed_hands_helper._shared import bhproto
    new = bhproto.new_private_key()
    npub = bhproto.pub_raw(new)
    nkid = bhproto.key_id(npub)
    sig = bhproto.sign(new, bhproto.CTX_ROTATE_SERVER, bhproto.canonical(
        {"new_key_id": nkid, "new_pubkey": bhproto.b64u(npub), "old_key_id": srv.kid}))
    return nkid, {"new_pubkey": bhproto.b64u(npub), "new_key_id": nkid, "sig_new": sig}


def _other_pin(c):
    from dataclasses import replace
    return replace(c, server=replace(c.server, helper_id="99999999-2222-4333-8444-555555555555"))


@pytest.mark.parametrize("kind,code", [("unpaired", 3), ("deleted", 3), ("other-pin", 1)])
def test_a_helper_whose_pairing_is_gone_from_disk_stops(tmp_path, caplog, kind, code):
    """A: config.toml no longer names the server this helper is paired with (bh unpair under 'bh run', the file
    deleted, or another pin written). The helper cancels its jobs as revoked, drops the key from memory, leaves
    the file alone and exits; it never keeps serving the old server."""
    import asyncio
    from dataclasses import replace
    from fake_server import FakeServer
    from test_daemon import arun
    from test_fix_round1 import RecordingBuild, two_builds

    async def main():
        srv = await FakeServer().start()
        try:
            fb = RecordingBuild()
            home, d, task = await _running_daemon(tmp_path, srv, fb, config_poll_s=0.05)
            a, b = await two_builds(srv, home, fb)
            path = home / config.CONFIG_NAME
            if kind == "unpaired":
                config.update(home, lambda c: replace(c, server=None))
            elif kind == "deleted":
                os.unlink(path)
            else:
                config.update(home, _other_pin)
            after = path.read_bytes() if path.exists() else None
            assert await asyncio.wait_for(task, 5) == code
            assert d.priv is None
            assert a["job_id"] in fb.cancelled and b["job_id"] not in fb.jobs
            from borrowed_hands_helper.daemon import DB_NAME
            from borrowed_hands_helper.jobstore import JobStore
            if kind != "deleted" or (home / DB_NAME).exists():
                store = JobStore(home / DB_NAME)                     # the helper's own store is closed by now
                try:
                    rows = {j: store.get(j) for j in (a["job_id"], b["job_id"])}
                finally:
                    store.close()
                assert all(r.state == "cancelled" for r in rows.values()), rows
                assert all(r.error["detail"]["reason"] == "revoked" for r in rows.values()), rows
            assert (path.read_bytes() if path.exists() else None) == after       # the helper wrote nothing
            assert len(_messages(caplog, "no longer names the server this helper is paired with")) == 1
        finally:
            await srv.stop()
    arun(main())


def test_rotation_does_not_put_a_pin_back_that_the_owner_removed(tmp_path):
    """B: the rotation write is a compare-and-swap on the pin the helper holds. With another [server] (or none) on
    disk it writes nothing and refuses; the helper then stops as above."""
    import asyncio
    from dataclasses import replace
    from fake_server import FakeServer
    from test_daemon import FakeBuild, arun

    async def main():
        srv = await FakeServer().start()
        try:
            home, d, task = await _running_daemon(tmp_path, srv, FakeBuild(), config_poll_s=0.5)
            config.update(home, lambda c: replace(c, server=None))
            before = (home / config.CONFIG_NAME).read_bytes()
            nkid, body = _rotate_frame(srv)
            await srv.send("server.rotate", body)
            err = (await srv.next("error"))["body"]
            assert err["code"] == "denied" and err["detail"]["reason"] == "pin_changed"
            assert (home / config.CONFIG_NAME).read_bytes() == before
            assert d.cfg.server.key_id == srv.kid                    # the old pin, never the new one
            assert await asyncio.wait_for(task, 5) == 3
        finally:
            await srv.stop()
    arun(main())


def test_a_revoked_running_build_is_cancelled_before_the_capability_probe(tmp_path):
    """C: the cancel pass runs as soon as the new config is in memory, not after a slow probe."""
    import threading
    from fake_server import FakeServer
    from test_daemon import GOOD, arun, stop, until
    from test_fix_round1 import RecordingBuild, two_builds

    slow, entered, release = [], threading.Event(), threading.Event()

    def probes(*_a, **_k):
        if slow:
            entered.set()
            release.wait(10)
        return GOOD

    async def main():
        srv = await FakeServer().start()
        try:
            fb = RecordingBuild()
            home, d, task = await _running_daemon(tmp_path, srv, fb, config_poll_s=0.05, probes=probes)
            a, b = await two_builds(srv, home, fb)
            slow.append(True)
            config.update(home, lambda c: c.without_workspace("demo"))
            await until(entered.is_set, timeout=3)
            try:
                assert a["job_id"] in fb.cancelled                   # already cancelled while the probe hangs
                assert d.cfg_mtime != config.mtime(home)             # and the version is not applied yet
            finally:
                release.set()
            await until(lambda: d.cfg_mtime == config.mtime(home), timeout=3)
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_an_empty_expires_is_an_error_not_a_grant_without_expiry(tmp_path):
    """H: `--expires "$UNSET"` must not silently make a grant that never expires."""
    w = World(tmp_path)
    paired(w)
    assert bh(w, *GRANT, "--expires", "") != 0
    assert config.load(w.env.home).workspace("demo") is None
    assert "expiry '' must be like" in w.text()


# ---------------------------------------------------------------- the stop holds against the paired server (Security review round 2)
@pytest.mark.parametrize("gone,code", [("removed", 3), ("other-pin", 1)])
def test_a_rotation_stream_does_not_hold_off_the_pairing_gone_stop(tmp_path, gone, code):
    """R1: the watcher skips only the rotation's own pin. A server that keeps sending valid server.rotate frames
    after the owner removed [server], or wrote another server's pin, gets pin_changed for each one and the helper
    still stops on its next poll."""
    import asyncio
    from dataclasses import replace
    from fake_server import FakeServer
    from test_daemon import FakeBuild, arun

    async def main():
        srv = await FakeServer().start()
        try:
            home, d, task = await _running_daemon(tmp_path, srv, FakeBuild(), config_poll_s=0.05)
            config.update(home, (lambda c: replace(c, server=None)) if gone == "removed" else _other_pin)

            sent = []

            async def stream():                          # ends only when the helper closes the connection
                while True:
                    await srv.send("server.rotate", _rotate_frame(srv)[1])
                    sent.append(1)
            flood = asyncio.create_task(stream())
            try:
                assert await asyncio.wait_for(task, 3) == code       # while the frames keep coming
                assert sent
            finally:
                flood.cancel()
                await asyncio.gather(flood, return_exceptions=True)
            assert d.priv is None and (config.load(home).server is None) == (gone == "removed")
        finally:
            await srv.stop()
    arun(main())


def _halted(d):
    """The helper has decided to stop, seen from a handler that is still running (the run loop is not woken)."""
    import asyncio
    ev = asyncio.Event()
    ev.set()
    d._halt = ev


def _revoked_row(d, job_id):
    row = d.store.get(job_id)
    return row.state == "cancelled" and row.error["detail"]["reason"] == "revoked"


def test_a_submit_that_arrives_after_the_halt_is_not_queued(tmp_path):
    """R2: a job.submit handled after the helper halted is finished as revoked, never enqueued."""
    from fake_server import FakeServer
    from test_daemon import arun, stop, until
    from test_fix_round1 import RecordingBuild, build_contract

    async def main():
        srv = await FakeServer().start()
        try:
            fb = RecordingBuild()
            home, d, task = await _running_daemon(tmp_path, srv, fb)
            _halted(d)
            d.build.hold(True)           # the lane takes nothing, so only the submit check can finish the job
            a, ra, sa = build_contract(srv)
            await srv.submit(ra, sa)
            await until(lambda: d.store.get(a["job_id"]) is not None and d.store.get(a["job_id"]).terminal, timeout=3)
            assert _revoked_row(d, a["job_id"])
            assert fb.started == [] and d.build.q.active is None and d.build.q.queued() == []
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_a_job_at_the_final_gate_after_the_halt_does_not_start(tmp_path):
    """R2: the halt lands during the awaits before the final gate. The job is finished as revoked (not put back
    to queued, which the next start would resume) and the executor never runs."""
    from fake_server import FakeServer
    from test_daemon import arun, stop, until
    from test_fix_round1 import RecordingBuild, build_contract

    async def main():
        srv = await FakeServer().start()
        try:
            fb = RecordingBuild()
            home, d, task = await _running_daemon(tmp_path, srv, fb)
            real = d._send_caps

            async def caps_then_halt():
                await real()
                _halted(d)
            a, ra, sa = build_contract(srv)
            d._send_caps = caps_then_halt                # the await just before the final gate
            await srv.submit(ra, sa)
            await until(lambda: d.store.get(a["job_id"]) is not None and d.store.get(a["job_id"]).terminal, timeout=3)
            assert _revoked_row(d, a["job_id"])
            assert fb.started == []
            await stop(task)
        finally:
            await srv.stop()
    arun(main())


def test_a_server_that_never_answers_close_cannot_keep_the_connection_task_alive(tmp_path):
    """R3: closing a transport is bounded, so the exit after a halt does not wait on the server."""
    import asyncio
    import time
    from dataclasses import replace
    from fake_server import FakeServer
    from borrowed_hands_helper import connection, keyfile
    from test_daemon import FAST, arun, pair_home

    class NeverCloses:
        def __init__(self, inner):
            self.inner, self.gave_up = inner, False

        async def send(self, t):
            await self.inner.send(t)

        async def recv(self):
            return await self.inner.recv()

        async def close(self, code=1000):
            try:
                await asyncio.sleep(3600)                # the server withholds its CLOSE
            finally:
                self.gave_up = True
                await self.inner.close(code)

    async def main():
        srv = await FakeServer().start()
        try:
            home = await pair_home(tmp_path, srv)
            cfg, priv, made = config.load(home), keyfile.load_device_key(home), []

            async def connect(url, timing):
                made.append(NeverCloses(await connection.AiohttpTransport.connect(url, timing)))
                return made[-1]

            async def on_session(session, t):
                raise connection.Stop(1, "done")
            t0 = time.monotonic()
            stop_ = await asyncio.wait_for(connection.run_forever(
                lambda: cfg.server, lambda: priv, on_session, timing=replace(FAST, close_s=0.2),
                connect=connect), 5)
            assert stop_.message == "done" and time.monotonic() - t0 < 3 and made[0].gave_up
        finally:
            await srv.stop()
    arun(main())
