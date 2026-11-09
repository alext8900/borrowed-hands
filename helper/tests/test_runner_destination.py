import importlib.machinery
import os
import pathlib
import types

import pytest

path = pathlib.Path(os.environ.get('BH_TEST_RUNNER', pathlib.Path(__file__).resolve().parents[2] / 'sandbox/bh-runner'))
loader = importlib.machinery.SourceFileLoader('bh_runner_test', str(path))
runner = types.ModuleType(loader.name)
runner.__file__ = str(path)
loader.exec_module(runner)


def test_exact_destinations(monkeypatch):
    monkeypatch.setattr(runner, 'validate_remote', lambda *args: None)
    job = {'v': 1, 'job_id': 'id', 'remote': 'https://example.com/x', 'commit': 'a'*40,
           'project': 'App.xcodeproj', 'scheme': 'App', 'configuration': 'Debug',
           'destination': 'generic/platform=iOS Simulator', 'allowed_refs': runner.ALLOWED_REFS}
    assert runner.validate_job(job, 'id', False)
    for dest in ('platform=iOS Simulator', 'generic/platform=iOS Simulator,id=anything', 'generic/platform=iOS Simulator '):
        with pytest.raises(runner.JobError):
            runner.validate_job({**job, 'destination': dest}, 'id', False)


def test_simulator_argv_is_compile_only():
    captured = []
    fake = types.SimpleNamespace(job={'scheme': 'App', 'configuration': 'Debug', 'destination': 'generic/platform=iOS Simulator'},
                                 dd='/tmp/dd', xcresult='/tmp/result', profile='builder.sb',
                                 run=lambda argv, *args: captured.append(argv) or 0)
    assert runner.Runner.build(fake, '/tmp/App.xcodeproj') == 0
    argv = captured[0]
    assert argv[argv.index('-sdk') + 1] == 'iphonesimulator'
    assert argv[argv.index('-destination') + 1] == 'generic/platform=iOS Simulator'
    assert argv[-1] == 'build' and 'test' not in argv
