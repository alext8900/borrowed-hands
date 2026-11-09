import json
from types import SimpleNamespace

from borrowed_hands import hooks, runtime, tools


def test_xcode_requires_ticket_and_binds_resolved_destination(monkeypatch):
    ws = {'workspace_id': 'demo', 'destinations': ['platform=macOS,arch=arm64']}
    h = SimpleNamespace(helper_id='mac', name='Mac', caps={'capabilities': [{'name': 'xcode.build', 'workspaces': [ws]}]})
    calls = []
    jobs = SimpleNamespace(listener_unavailable=lambda: None, submit_and_wait=lambda *a, **kw: calls.append(a) or {'ok': True, 'state': 'building'})
    rt = SimpleNamespace(degraded=None, jobs=jobs, store=None, resolve_helper=lambda *a: {'helper': h})
    monkeypatch.setattr(runtime, 'RT', rt)
    hooks.configure(lambda t, a: ('mac', 'Mac', {**a, 'destination': ws['destinations'][0]}))
    args = {'workspace_id': 'demo', 'repo_url': 'https://github.com/a/b', 'commit': 'a'*40, 'scheme': 'Demo'}
    assert json.loads(tools.hands_xcode_build(args, session_id='s'))['error']['code'] == 'denied'
    hooks.pre_tool_call(tool_name='hands_xcode_build', args=args, session_id='s', tool_call_id='c')
    result = json.loads(tools.hands_xcode_build(args, session_id='s', tool_call_id='c'))
    assert result['ok'] and calls[0][2]['destination'] == ws['destinations'][0]
    assert calls[0][2]['repo_fingerprint'] and 'repo_url' not in calls[0][2]


def test_xcode_refuses_changed_default_after_approval(monkeypatch):
    ws = {'workspace_id': 'demo', 'destinations': ['platform=macOS,arch=arm64']}
    h = SimpleNamespace(helper_id='mac', name='Mac', caps={'capabilities': [{'name': 'xcode.build', 'workspaces': [ws]}]})
    calls = []
    service = SimpleNamespace(listener_unavailable=lambda: None, submit_and_wait=lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(runtime, 'RT', SimpleNamespace(degraded=None, jobs=service, store=None,
                                                        resolve_helper=lambda *a: {'helper': h}))
    hooks.configure(lambda t, a: ('mac', 'Mac', {**a, 'destination': ws['destinations'][0]}))
    args = {'workspace_id': 'demo', 'repo_url': 'https://github.com/a/b', 'commit': 'a'*40, 'scheme': 'Demo'}
    hooks.pre_tool_call(tool_name='hands_xcode_build', args=args, session_id='s', tool_call_id='change')
    ws['destinations'] = ['generic/platform=iOS Simulator']
    result = json.loads(tools.hands_xcode_build(args, session_id='s', tool_call_id='change'))
    assert result['error']['detail']['reason'] == 'approval_not_observed' and not calls


def test_xcode_wait_budget_starts_before_resolution(monkeypatch):
    ws = {'workspace_id': 'demo', 'destinations': ['platform=macOS,arch=arm64']}
    h = SimpleNamespace(helper_id='mac', name='Mac', caps={'capabilities': [{'name': 'xcode.build', 'workspaces': [ws]}]})
    calls = []
    service = SimpleNamespace(listener_unavailable=lambda: None, submit_and_wait=lambda *a, **kw: calls.append(a) or {'ok': True, 'state': 'building'})
    monkeypatch.setattr(runtime, 'RT', SimpleNamespace(degraded=None, jobs=service, store=None,
                                                        resolve_helper=lambda *a: {'helper': h}))
    hooks.configure(lambda t, a: ('mac', 'Mac', {**a, 'destination': ws['destinations'][0]}))
    args = {'workspace_id': 'demo', 'repo_url': 'https://github.com/a/b', 'commit': 'a'*40, 'scheme': 'Demo', 'wait_seconds': 300}
    hooks.pre_tool_call(tool_name='hands_xcode_build', args=args, session_id='s', tool_call_id='wait')
    clock = iter((100.0, 102.5))
    monkeypatch.setattr(tools, 'time', SimpleNamespace(monotonic=lambda: next(clock), time=lambda: 0))
    assert json.loads(tools.hands_xcode_build(args, session_id='s', tool_call_id='wait'))['ok']
    assert calls[0][3] == 297.5
