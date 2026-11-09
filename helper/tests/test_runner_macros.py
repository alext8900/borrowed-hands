"""Swift macros under builder.sb (2026-11-09).

In the Xcode 27 SDK, @State, #Preview and @Observable are macros. swift-frontend runs each macro
plugin through its own `sandbox-exec`, and a process already inside builder.sb can't apply a
second sandbox (`sandbox_apply: Operation not permitted`), so every such build failed. bh-runner
passes SWIFTC_DISABLE_SANDBOX=YES (Xcode's setting for swiftc -disable-sandbox); builder.sb, which
every process of the build inherits, still confines the plugins.

The unit tests pin the argv. The `xcode` tests build real fixtures with the runner's own argv,
sandbox params and environment under `sandbox-exec -f sandbox/builder.sb`, as the current user
(not bh-builder): `uv run --frozen pytest -q -p no:cacheprovider -m xcode tests/test_runner_macros.py`.
"""
import importlib.machinery
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
path = pathlib.Path(os.environ.get('BH_TEST_RUNNER', REPO / 'sandbox/bh-runner'))
loader = importlib.machinery.SourceFileLoader('bh_runner_macros_test', str(path))
runner = types.ModuleType(loader.name)
runner.__file__ = str(path)
loader.exec_module(runner)

PROFILE = str(REPO / 'sandbox/builder.sb')
DESTINATIONS = (runner.DESTINATION, 'generic/platform=iOS Simulator')
SETTING = 'SWIFTC_DISABLE_SANDBOX=YES'


def _fake(job_dir, scheme='App', configuration='Debug', destination=runner.DESTINATION):
    captured = []
    fake = types.SimpleNamespace(
        job={'scheme': scheme, 'configuration': configuration, 'destination': destination,
             'remote': 'https://example.com/x.git'},
        job_dir=job_dir, dd=os.path.join(job_dir, 'dd'), xcresult=os.path.join(job_dir, 'result.xcresult'),
        profile=PROFILE, captured=captured, run=lambda argv, *a, **k: captured.append(argv) or 0)
    return fake


def build_argv(destination, job_dir='/tmp/job', project='/tmp/job/src/App.xcodeproj', **kw):
    fake = _fake(job_dir, destination=destination, **kw)
    assert runner.Runner.build(fake, project) == 0
    return fake.captured[0]


@pytest.mark.parametrize('destination', DESTINATIONS)
def test_argv_disables_the_compiler_plugin_sandbox_exactly_once(destination):
    argv = build_argv(destination)
    assert argv.count(SETTING) == 1
    # one mention in any form: no second, weaker or contradicting copy (SWIFTC_DISABLE_SANDBOX=NO,
    # a raw -disable-sandbox, -Xfrontend ...)
    assert [a for a in argv if 'SWIFTC_DISABLE_SANDBOX' in a or 'disable-sandbox' in a] == [SETTING]
    # a build setting (NAME=VALUE) with the other settings, before the action
    assert argv.index(SETTING) > argv.index('-hideShellScriptEnvironment')
    assert argv.index(SETTING) < argv.index('build') == len(argv) - 1
    # the project's own Swift flags are left alone: the runner never sets OTHER_SWIFT_FLAGS
    assert not [a for a in argv if a.startswith('OTHER_SWIFT_FLAGS')]
    # the existing nested-sandbox precedent is still there
    assert argv.count('-IDEPackageSupportDisableManifestSandbox=YES') == 1


# ---------------------------------------------------------------- real builds (opt-in)

needs_xcode = pytest.mark.skipif(not os.path.exists(runner.XCODEBUILD) or not os.path.exists(runner.SANDBOX_EXEC),
                                 reason='needs Xcode at %s and sandbox-exec' % runner.DEVELOPER_DIR)


def _user_dir(name):
    out = subprocess.run(['/usr/bin/getconf', name], capture_output=True, text=True, check=True).stdout.strip()
    return os.path.realpath(out).rstrip('/')


def run_job(tmp_path, fixture, project, scheme, destination, configuration='Debug', sandboxed=True, drop=()):
    """Build the fixture dir in a job-shaped dir with bh-runner's argv, params and env. With
    sandboxed=False the same xcodebuild argv runs without sandbox-exec (the positive control).
    `drop` removes argv entries (to show a fixture really needs them). Returns (rc, log, job)."""
    job = os.path.realpath(str(tmp_path / 'job'))
    os.makedirs(job)
    for d in ('home', 'tmp', 'logs'):
        os.mkdir(os.path.join(job, d), 0o700)
    shutil.copytree(fixture, os.path.join(job, 'src'), symlinks=True)
    fake = _fake(job, scheme=scheme, configuration=configuration, destination=destination)
    fake.user_tmp, fake.user_cache = _user_dir('DARWIN_USER_TEMP_DIR'), _user_dir('DARWIN_USER_CACHE_DIR')
    runner.Runner.build(fake, os.path.join(job, 'src', project))
    argv = [a for a in fake.captured[0] if a not in drop]
    if sandboxed:
        argv = [runner.SANDBOX_EXEC, '-f', PROFILE] + runner.Runner.sandbox_params(fake) + argv
    env = runner.clean_env(os.path.join(job, 'home'), os.path.join(job, 'tmp') + '/')
    log_path = os.path.join(job, 'logs', 'xcodebuild.log')
    with open(log_path, 'wb') as log:
        rc = subprocess.run(argv, cwd=job, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            timeout=900).returncode
    with open(log_path, encoding='utf-8', errors='replace') as f:
        return rc, f.read(), job


def _tail(log):
    return log[-4000:]


@pytest.mark.parametrize('destination,configuration', [
    (runner.DESTINATION, 'Debug'),
    ('generic/platform=iOS Simulator', 'Debug'),
    (runner.DESTINATION, 'Release'),        # Release sets OTHER_SWIFT_FLAGS = "" at target level
])
@pytest.mark.xcode
@needs_xcode
def test_swiftui_macros_build_under_builder_sb(tmp_path, destination, configuration):
    rc, log, _ = run_job(tmp_path, REPO / 'tests/macro-app', 'MacroApp.xcodeproj', 'MacroApp', destination, configuration)
    assert rc == 0 and '** BUILD SUCCEEDED **' in log, _tail(log)
    assert 'sandbox_apply' not in log and 'could not be found for macro' not in log
    # the project sets SWIFTC_DISABLE_SANDBOX = NO (project, target, and [sdk=...] conditional) and
    # its own OTHER_SWIFT_FLAGS without $(inherited); the command-line setting still reaches swiftc
    swiftc = [line for line in log.splitlines() if 'builtin-Swift-Compilation -- ' in line]
    assert swiftc and all(' -disable-sandbox ' in line for line in swiftc)
    if configuration == 'Debug':
        # and the project's own flag (checked by #error in App.swift) is still there
        assert all(' -DBH_PROJECT_FLAG ' in line for line in swiftc)


@pytest.mark.xcode
@needs_xcode
def test_macro_fixture_fails_without_the_setting(tmp_path):
    """Control: the same fixture and argv minus SWIFTC_DISABLE_SANDBOX=YES fails the way main did."""
    rc, log, _ = run_job(tmp_path, REPO / 'tests/macro-app', 'MacroApp.xcodeproj', 'MacroApp', runner.DESTINATION, drop=(SETTING,))
    assert rc != 0 and '** BUILD FAILED **' in log
    assert 'sandbox-exec: sandbox_apply: Operation not permitted' in log
    for macro in ('SwiftUIMacros.StateMacro', 'PreviewsMacros.SwiftUIView', 'ObservationMacros.ObservableMacro'):
        assert "external macro implementation type '%s' could not be found" % macro in log


@pytest.fixture
def probe_env(tmp_path):
    """A canary dir outside the job and outside the per-user T/C dirs, and a localhost listener."""
    outside = tempfile.mkdtemp(prefix='bh-macro-probe-', dir='/private/tmp')
    with open(os.path.join(outside, 'canary.txt'), 'w') as f:
        f.write('canary\n')
    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(8)
    try:
        yield outside, srv.getsockname()[1]
    finally:
        srv.close()
        shutil.rmtree(outside, ignore_errors=True)


def _probe(tmp_path, outside, port, sandboxed):
    staged = tmp_path / 'fixture'
    shutil.copytree(REPO / 'tests/macro-probe-app', staged, symlinks=True)
    (staged / 'probe-config').write_text('%s\n%s\n%d\n' % (outside, os.path.realpath(os.path.expanduser('~')), port))
    rc, log, job = run_job(tmp_path, staged, 'MacroProbe.xcodeproj', 'MacroProbe', runner.DESTINATION, sandboxed=sandboxed)
    results = pathlib.Path(job, 'src', 'probe-results.txt')
    assert results.exists(), 'the probe plugin never ran:\n' + _tail(log)
    raw = results.read_text()
    rows = dict(line.split('=', 1) for line in raw.splitlines()[1:] if '=' in line)
    return rc, log, rows, raw


@pytest.mark.xcode
@needs_xcode
def test_macro_plugins_stay_inside_builder_sb(tmp_path, probe_env):
    outside, port = probe_env
    escapes = ('read_outside_canary', 'write_outside', 'list_owner_home', 'tcp_localhost')
    # positive control: same argv (plugin sandbox off), no builder.sb: every escape works
    rc, log, rows, raw = _probe(tmp_path / 'control', outside, port, sandboxed=False)
    assert rc != 0                                   # the stand-in plugin never answers
    assert '/usr/bin/swift-frontend\n' in raw, raw  # launched by the compiler, as a macro plugin
    assert {k: rows.get(k) for k in escapes + ('write_checkout',)} == dict.fromkeys(escapes + ('write_checkout',), 'ALLOWED'), raw
    os.unlink(os.path.join(outside, 'written-by-plugin'))
    # under builder.sb: the plugin runs (it wrote its results into the checkout) but escapes nothing
    rc, log, rows, raw = _probe(tmp_path / 'sandboxed', outside, port, sandboxed=True)
    assert rc != 0
    assert raw.startswith('ran=1 '), raw            # it ran (builder.sb hides its parent from ps)
    assert {k: rows.get(k) for k in escapes} == dict.fromkeys(escapes, 'DENIED'), raw
    assert rows.get('write_checkout') == 'ALLOWED', raw   # builder.sb's own job write allowance
    assert not os.path.exists(os.path.join(outside, 'written-by-plugin'))
