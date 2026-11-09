import asyncio
import json
import uuid

import pytest

from borrowed_hands_helper.config import Policy, Workspace
from borrowed_hands_helper.executors import AcceptedJob
from borrowed_hands_helper.executors.xcode import XcodeExecutor


def job():
    ws = Workspace('demo', 'https://github.com/local/repo', 'Local.xcodeproj', ('Demo',), ('Debug',), ('platform=macOS,arch=arm64',))
    return AcceptedJob(str(uuid.uuid4()), 'xcode.build', {'commit': 'a'*40, 'scheme': 'Demo', 'configuration': 'Debug', 'destination': ws.destinations[0]}, {}, '', 0, '', ws)


def test_local_authority_and_hostile_output(tmp_path):
    async def run():
        script = tmp_path / 'fake.py'
        script.write_text('import json,sys\nj=json.load(sys.stdin)\nassert j["remote"] == "https://github.com/local/repo" and j["project"] == "Local.xcodeproj"\nprint(json.dumps({"status":"failed","duration_s":1,"errors":[{"file":"../../secret", "message":"bad /opt/private/secret", "type":"error"}],"warnings":[]}))\n')
        import sys
        ex = XcodeExecutor(Policy(), argv=[sys.executable, str(script)])
        out = await ex.run(job(), lambda _: None)
        assert out.state == 'failed' and out.error is None
        assert '..' not in json.dumps(out.result) and '/opt/private' not in json.dumps(out.result)
    asyncio.run(run())


def test_cancel_before_spawn():
    async def run():
        j = job()
        ex = XcodeExecutor(Policy(), argv=['/not/a/command'])
        await ex.cancel(j.job_id)
        assert (await ex.run(j, lambda _: None)).state == 'cancelled'
    asyncio.run(run())


def test_status_mapping_and_bounded_output():
    from borrowed_hands_helper.executors.xcode import _map
    j = job()
    for status, code, reason in [('unsafe_tree', 'invalid_arguments', 'unsafe_tree'), ('not_reachable', 'needs_resolution', 'not_on_bh_branch'), ('unsupported_dependencies', 'needs_resolution', 'unsupported_dependencies')]:
        out = _map({'status': status, 'message': '/secret/path', 'files': ['../../secret']}, j)
        assert out.error['code'] == code and out.error['detail']['reason'] == reason
        assert '..' not in json.dumps(out.error) and '/secret' not in json.dumps(out.error)
    out = _map({'status': 'failed', 'errors': [{'message': '/opt/private/x ' + 'x'*1000, 'type': 'error'}]*60, 'warnings': [], 'log_tail': 'x'*8000}, j)
    assert len(out.result['errors']) == 50 and out.result['truncated']
    assert len(out.result['errors'][0]['message']) <= 500 and len(out.result['log_tail']) <= 4000


def test_cancel_running_and_task_shutdown(tmp_path):
    import sys
    async def run():
        script = tmp_path / 'wait.py'
        marker = tmp_path / 'started'
        script.write_text('import sys,time,pathlib\nsys.stdin.read()\npathlib.Path(sys.argv[1]).touch()\ntime.sleep(30)\n')
        for shutdown in (False, True):
            marker.unlink(missing_ok=True)
            j = job()
            ex = XcodeExecutor(Policy(), argv=[sys.executable, str(script), str(marker)])
            task = asyncio.create_task(ex.run(j, lambda _: None))
            for _ in range(200):
                if marker.exists():
                    break
                await asyncio.sleep(.01)
            assert marker.exists()
            if shutdown:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                else:
                    assert False
            else:
                await ex.cancel(j.job_id)
                assert (await task).state == 'cancelled'
            assert not ex._processes
    asyncio.run(run())


def test_production_argv_uses_current_job_policy(monkeypatch):
    from dataclasses import replace
    from borrowed_hands_helper import caps
    import sys
    async def run():
        seen = []
        original = asyncio.create_subprocess_exec
        async def spawn(*argv, **kwargs):
            seen.append(argv)
            return await original(sys.executable, '-c', 'import sys; sys.stdin.read(); print("{\\"status\\":\\"timeout\\"}")', **kwargs)
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        policy = Policy(build_timeout_s=301, disk_budget_mb=1234, mem_budget_mb=2048)
        out = await XcodeExecutor().run(replace(job(), policy=policy), lambda _: None)
        assert seen == [(caps.SUDO, '-n', *caps.dispatch_argv(policy))]
        assert out.error['code'] == 'timeout'
    asyncio.run(run())


def test_dispatcher_busy_is_retryable(tmp_path):
    import sys
    async def run():
        script = tmp_path / 'busy.py'
        script.write_text('import sys\nsys.stdin.read()\nprint("{\\"status\\":\\"busy\\"}")\n')
        ex = XcodeExecutor(argv=[sys.executable, str(script)])
        j = job()
        assert await ex.run(j, lambda _: None) == 'busy'
        await ex.cancel(j.job_id)
        assert (await ex.run(j, lambda _: None)).state == 'cancelled'
    asyncio.run(run())


def test_scrubs_encoded_and_traversal_in_all_dispatcher_strings():
    from borrowed_hands_helper.executors.xcode import _map
    hostile = 'note:%252Fopt%252Fprivate%252Ftoken and sub/../../secret'
    out = _map({'status': 'failed', 'errors': [{'file': hostile, 'message': hostile, 'type': hostile}],
                'warnings': [], 'log_tail': hostile}, job())
    wire = json.dumps(out.result)
    assert '/opt/' not in wire and '../' not in wire and '%2F' not in wire


def test_every_failure_reason_gets_a_specific_actionable_message():
    # audit #5: the reason code survives, and the message tells Hermes and the user what to do about it
    from borrowed_hands_helper import schema
    from borrowed_hands_helper.executors.xcode import _map
    expect = {
        'timeout': ('timeout', 'timeout', 'time limit'),
        'fetch_failed': ('needs_resolution', 'commit_not_found', 'pushed'),
        'not_reachable': ('needs_resolution', 'not_on_bh_branch', 'bh/'),
        'unsupported_dependencies': ('needs_resolution', 'unsupported_dependencies', 'local path package'),
        'unsafe_tree': ('invalid_arguments', 'unsafe_tree', 'symlink'),
        'disk_budget': ('internal_error', 'disk_budget', 'disk'),
        'memory_budget': ('internal_error', 'memory_budget', 'memory'),
        'budget_check_failed': ('internal_error', 'budget_check_failed', 'measure'),
        'something_new': ('internal_error', 'dispatch_error', 'bh selftest'),
    }
    seen = set()
    for status, (code, reason, needle) in expect.items():
        out = _map({'status': status, 'message': 'runner said /secret/path ../../x'}, job())
        e = out.error
        assert e['code'] == code and e['detail']['reason'] == reason, status
        assert needle in e['message'], (status, e['message'])
        assert e['message'] != 'The Mac could not complete the build.' and '/secret' not in e['message'], status
        schema.validate('error', e)
        seen.add(e['message'])
    assert len(seen) == len(expect)                       # no two reasons share one generic sentence
    missing = _map({'status': 'invalid_job', 'message': 'project path missing or not a real directory in the commit'},
                   job())
    assert missing.error['detail']['reason'] == 'project_missing' and 'bh grant list' in missing.error['message']
    other = _map({'status': 'invalid_job', 'message': 'remote host does not resolve: gaierror'}, job())
    assert other.error['detail']['reason'] == 'invalid_job' and other.error['message'] not in seen


def test_dispatch_failure_points_the_owner_at_selftest():
    import sys
    async def run():
        # sudo refusing (e.g. the exact rule is gone) prints nothing on stdout: that must not read as a mystery
        ex = XcodeExecutor(argv=[sys.executable, '-c', 'import sys; sys.stdin.read(); sys.exit(1)'])
        out = await ex.run(job(), lambda _: None)
        assert out.error['code'] == 'internal_error' and out.error['detail']['reason'] == 'dispatch_error'
        assert 'bh selftest' in out.error['message']
    asyncio.run(run())


PASSWORD_TEXT = ("sudo asked for a password for the Borrowed Hands build command: another sudo rule on this Mac "
                 "overrides its password-free rule. Remove or reorder that rule, then run bh selftest.")


def _sudo_like(stderr: str, rc: int, stdout: str = ''):
    import sys
    code = (f'import sys; sys.stdin.read(); sys.stdout.write({stdout!r}); sys.stderr.write({stderr!r}); '
            f'sys.exit({rc})')
    return XcodeExecutor(argv=[sys.executable, '-c', code])


def test_sudo_password_required_gets_its_own_message():
    # r4: `sudo -n -l` confirms the command is permitted, not that it is password-free; a later PASSWD rule only
    # shows when the build runs. The reason code and schema stay as they are; only the sentence is specific.
    from borrowed_hands_helper import schema
    async def run():
        out = await _sudo_like('sudo: a password is required\n', 1).run(job(), lambda _: None)
        assert out.error['code'] == 'internal_error' and out.error['detail'] == {'reason': 'dispatch_error'}
        assert out.error['message'] == PASSWORD_TEXT
        schema.validate('error', out.error)
    asyncio.run(run())


@pytest.mark.parametrize('stderr,rc', [
    ('sudo: a password is required\n', 2),            # not sudo's refusal exit status
    ('sudo: unable to resolve host\n', 1),             # another sudo failure
    ('', 1),
])
def test_other_dispatch_failures_keep_the_generic_message(stderr, rc):
    async def run():
        out = await _sudo_like(stderr, rc).run(job(), lambda _: None)
        assert out.error['detail']['reason'] == 'dispatch_error' and out.error['message'] != PASSWORD_TEXT
        assert 'bh selftest' in out.error['message']
    asyncio.run(run())


def test_a_build_that_ran_ignores_password_text_in_its_stderr():
    # the runner's own output decides once the dispatcher answered
    async def run():
        ok = '{"status":"succeeded","duration_s":1,"errors":[],"warnings":[]}'
        out = await _sudo_like('a password is required\n', 0, stdout=ok).run(job(), lambda _: None)
        assert out.state == 'succeeded' and out.error is None
    asyncio.run(run())


def test_large_stderr_does_not_block_the_build():
    import sys
    async def run():
        code = ('import sys; sys.stdin.read(); sys.stderr.write("x" * 2000000); '
                'print(\'{"status":"succeeded","duration_s":1,"errors":[],"warnings":[]}\')')
        out = await asyncio.wait_for(XcodeExecutor(argv=[sys.executable, '-c', code]).run(job(), lambda _: None), 20)
        assert out.state == 'succeeded'
    asyncio.run(run())


def test_cancel_while_spawn_is_pending_does_not_hang_on_unread_stderr(monkeypatch):
    # r5 finding 1: the run task is cancelled while the spawn is still pending, so the recovery path terminates
    # and waits for the child. With >64 KiB of unread stderr, Python 3.11 can wait forever unless stderr drains.
    import sys
    async def run():
        held, release = {}, asyncio.Event()
        real = asyncio.create_subprocess_exec

        async def slow_spawn(*argv, **kwargs):
            held['p'] = await real(*argv, **kwargs)
            await release.wait()                                   # the spawn completes only after the cancel
            return held['p']
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', slow_spawn)
        code = 'import sys, time; sys.stderr.write("x" * 2000000); sys.stderr.flush(); time.sleep(30)'
        ex = XcodeExecutor(argv=[sys.executable, '-c', code])
        task = asyncio.create_task(ex.run(job(), lambda _: None))
        for _ in range(200):
            if 'p' in held:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)                                   # let the child fill the stderr pipe
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        try:
            await asyncio.wait_for(task, 10)
        except asyncio.CancelledError:
            pass
        assert held['p'].returncode is not None and not ex._processes
    asyncio.run(run())


@pytest.mark.parametrize('second_cancel_during', ['spawn', 'wait'])
def test_repeated_task_cancellation_still_stops_and_reaps_the_child(monkeypatch, second_cancel_during):
    # r6: a second task.cancel() during the recovery await must not abandon the child (executor contract: task
    # cancellation stops the child). 'spawn': while the spawn is still pending; 'wait': while waiting for the
    # terminated child to exit (it takes ~0.5 s to handle SIGTERM).
    import sys
    async def run():
        held, release = {}, asyncio.Event()
        real = asyncio.create_subprocess_exec

        async def slow_spawn(*argv, **kwargs):
            held['p'] = await real(*argv, **kwargs)
            await release.wait()
            return held['p']
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', slow_spawn)
        code = ('import signal, sys, time; signal.signal(signal.SIGTERM, lambda *a: (time.sleep(0.5), sys.exit(0))); '
                'time.sleep(30)')
        ex = XcodeExecutor(argv=[sys.executable, '-c', code])
        task = asyncio.create_task(ex.run(job(), lambda _: None))
        for _ in range(200):
            if 'p' in held:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)                       # the child has installed its SIGTERM handler
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)                         # now inside the recovery await on the pending spawn
        if second_cancel_during == 'spawn':
            task.cancel()
            await asyncio.sleep(0)
            release.set()
        else:
            release.set()
            await asyncio.sleep(0.2)                   # terminated, now waiting for the child to exit
            task.cancel()
        try:
            await asyncio.wait_for(task, 10)
        except asyncio.CancelledError:
            pass
        p = held['p']
        try:
            assert p.returncode is not None, 'the child was abandoned alive'
            assert not ex._processes
        finally:
            if p.returncode is None:                   # never leave the test's sleeper behind
                p.kill()
                await p.wait()
    asyncio.run(run())


def test_a_cancel_while_waiting_after_oversized_output_wins():
    # r5 review of the other await points: the stdout limit terminates the child and waits for it; a cancel that
    # arrives during that wait must still be reported as cancelled, not as a dispatch error
    import sys
    async def run():
        code = ('import signal, sys, time; '
                'signal.signal(signal.SIGTERM, lambda *a: (time.sleep(1), sys.exit(1))); '
                'sys.stdin.read(); sys.stdout.write("x" * 2000000); sys.stdout.flush(); time.sleep(30)')
        ex = XcodeExecutor(argv=[sys.executable, '-c', code])
        j = job()
        task = asyncio.create_task(ex.run(j, lambda _: None))
        await asyncio.sleep(0.5)                       # past the 1 MiB limit: terminated, now waiting for exit
        assert not task.done()
        await ex.cancel(j.job_id)
        out = await asyncio.wait_for(task, 10)
        assert out.state == 'cancelled', out
    asyncio.run(run())


def test_a_cancel_during_the_password_check_wins(monkeypatch):
    # r5 finding 2: the child exits 1 with the password text, but stderr stays open a moment (a grandchild holds
    # it), so the executor is still waiting for stderr when cancel(job_id) arrives. The cancel must win.
    import sys
    async def run():
        entered = asyncio.Event()
        real = XcodeExecutor._needed_password

        async def watched(p, err, data):
            entered.set()
            await asyncio.sleep(0.3)        # this await is where a real cancel can land (stderr EOF lagging exit)
            return await real(p, err, data)
        monkeypatch.setattr(XcodeExecutor, '_needed_password', staticmethod(watched))
        code = ('import subprocess, sys; sys.stdin.read(); '
                'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"], '
                'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL); '
                'sys.stderr.write("sudo: a password is required\\n"); sys.stderr.flush(); sys.exit(1)')
        ex = XcodeExecutor(argv=[sys.executable, '-c', code])
        j = job()
        task = asyncio.create_task(ex.run(j, lambda _: None))
        await asyncio.wait_for(entered.wait(), 10)                 # now inside the stderr wait, child exited 1
        assert not task.done()
        await ex.cancel(j.job_id)
        out = await asyncio.wait_for(task, 10)
        assert out.state == 'cancelled', out
    asyncio.run(run())
