#!/usr/bin/env python3
"""Real pinned-image boot checks. Docker only, --network none, no host ports or credentials.

Build first: docker build --network none --pull=false -t bh-railway:test -f deploy/railway/Dockerfile .
Run: python3 tests/deploy/test_boot_matrix.py bh-railway:test
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

IMAGE = sys.argv[1]
PREFIX = f'bh-l7-matrix-{os.getpid()}'
containers = []
volumes = []


def docker(*args):
    print('$ docker ' + shlex.join(args), flush=True)
    p = subprocess.run(['docker', *args], text=True, capture_output=True)
    if p.returncode:
        raise AssertionError(f'docker {args[0]} failed: {p.stderr}')
    return (p.stdout + (p.stderr if args[0] == 'logs' else '')).strip()


def py(name, code, user='hermes'):
    # All test snippets are source-controlled and never print credentials or environment values.
    return docker('exec', '--user', user, name, '/opt/hermes/.venv/bin/python', '-c', code)


HEALTH = '''import json, time, urllib.request
end = time.monotonic() + 90
while time.monotonic() < end:
    try:
        with urllib.request.urlopen('http://127.0.0.1:8790/bh/v1/healthz', timeout=1) as r:
            d = json.load(r)
            if r.status == 200 and d == {'ok': True, 'v': 1}:
                print(json.dumps(d)); break
    except Exception:
        pass
    time.sleep(1)
else:
    raise SystemExit('healthz did not become ready in 90s')
'''
PROCESSES = '''import json, os
from pathlib import Path
rows=[]
for p in Path('/proc').iterdir():
    if not p.name.isdigit() or int(p.name) == os.getpid(): continue
    try:
        args=p.joinpath('cmdline').read_bytes().decode().replace('\\0',' ').strip()
        status=dict(line.split(':',1) for line in p.joinpath('status').read_text().splitlines() if ':' in line)
        if args: rows.append({'pid':int(p.name),'ppid':int(status['PPid']), 'uid':int(status['Uid'].split()[0]),'args':args})
    except (OSError, ValueError): pass
print(json.dumps(rows))
'''


def start(label, mounts=()):
    name = PREFIX + '-' + label
    containers.append(name)
    docker('run', '-d', '--network', 'none', '--name', name, '-e', 'BH_OWNERS=cli:local', *mounts, IMAGE)
    print('HEALTH ' + label + ' ' + py(name, HEALTH), flush=True)
    # Let gateway initialization settle before inspecting processes.
    time.sleep(3)
    return name


def inspect(label, name, registrations=1):
    rows = json.loads(py(name, PROCESSES))
    print('PROCESSES ' + label + ' ' + json.dumps(rows), flush=True)
    gateways = [r for r in rows if 'gateway run --replace' in r['args']]
    assert len(gateways) == 1 and gateways[0]['uid'] == 10000, gateways
    parent = next(r for r in rows if r['pid'] == gateways[0]['ppid'])
    assert parent['args'] == 's6-supervise gateway-default', parent
    assert not any('/opt/bh/bh-start.sh' in r['args'] or r['args'].startswith(('tail ', 'grep ')) for r in rows)
    logs = [r for r in rows if r['args'].startswith('s6-log ')]
    assert len(logs) == 1 and logs[0]['uid'] == 10000
    permissions = json.loads(py(name, '''import json, os, stat
paths=['/opt/data/logs','/opt/data/logs/gateways','/opt/data/logs/container-boot.log','/opt/data/logs/curator','/opt/data/logs/agent.log']
print(json.dumps([{ 'path':p, 'uid':os.stat(p).st_uid, 'gid':os.stat(p).st_gid,'mode':oct(stat.S_IMODE(os.stat(p).st_mode))} for p in paths]))
'''))
    assert all(p['uid'] == 10000 and p['gid'] == 10000 for p in permissions), permissions
    print('OWNERSHIP ' + label + ' ' + json.dumps(permissions), flush=True)
    lines = py(name, "from pathlib import Path; print('\\n'.join(l for l in Path('/opt/data/logs/agent.log').read_text().splitlines() if 'borrowed_hands:' in l))")
    print('LISTENER ' + label + '\n' + lines, flush=True)
    assert lines.count('listener up on') == registrations and lines.count('registered (v0.2.0, listener up)') == registrations
    boot = docker('logs', name)
    assert boot.count('[bh-start] seeded ') == 1
    assert boot.count('[stage2] Setup complete; starting user services') == 1
    assert 'Traceback' not in boot and 'Permission denied' not in boot
    print('BOOT ' + label + ' seed_count=1 tracebacks=0 permission_errors=0', flush=True)
    assert json.loads(py(name, HEALTH)) == {'ok': True, 'v': 1}
    print('PASS ' + label, flush=True)


try:
    print('IMAGE ' + docker('image', 'inspect', IMAGE, '--format', '{{.Id}} {{.Architecture}} {{json .Config.Volumes}}'))
    # The base declares VOLUME /opt/data, so Docker supplies an anonymous seeded volume when none is requested.
    no_volume = start('no-explicit-volume')
    # Keep the first boot alive past the original failure window while the other real boots run.
    launched = time.monotonic()
    volume = PREFIX + '-data'
    volumes.append(volume)
    docker('volume', 'create', volume)
    mount = ('--mount', f'type=volume,source={volume},target=/opt/data')
    seeded = docker('run', '--rm', '--network', 'none', '--entrypoint', 'stat', *mount, IMAGE, '-c', '%u:%g %n', '/opt/data')
    print('NAMED_VOLUME_IMAGE_SEED ' + seeded)
    assert seeded.startswith('10000:10000 ')
    named = start('named-first', mount)
    inspect('named-first', named)
    # Snapshot hashes are compared in-container; neither key bytes nor dotenv files are printed.
    snapshot_code = "from pathlib import Path; import hashlib,json; h=Path('/opt/data'); print(json.dumps({str(p.relative_to(h)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [h/'config.yaml',*h.glob('plugin-data/*/server_ed25519.key'),h/'plugin-data/l7-state-marker']}))"
    docker('stop', '-t', '20', named)
    docker('rm', '-v', named); containers.remove(named)
    docker('run', '--rm', '--network', 'none', '--entrypoint', '/opt/hermes/.venv/bin/python', *mount, IMAGE, '-c',
           "from pathlib import Path; p=Path('/opt/data/config.yaml'); p.write_bytes(p.read_bytes()+b'\\n# l7 repeat marker\\nl7_operator_marker: preserve-me\\n'); Path('/opt/data/plugin-data/l7-state-marker').write_bytes(b'keep state')")
    before = docker('run', '--rm', '--network', 'none', '--entrypoint', '/opt/hermes/.venv/bin/python', *mount, IMAGE, '-c', snapshot_code)
    assert len(json.loads(before)) == 3 and sum(p.endswith('/server_ed25519.key') for p in json.loads(before)) == 1
    repeated = start('named-repeat', mount)
    after = py(repeated, snapshot_code)
    assert json.loads(before) == json.loads(after), 'existing config/key/state changed on repeat boot'
    print('REPEAT config_bytes_identical=true server_key_bytes_identical=true plugin_state_bytes_identical=true')
    inspect('named-repeat', repeated, registrations=2)
    docker('stop', '-t', '20', repeated)
    docker('rm', '-v', repeated); containers.remove(repeated)
    with tempfile.TemporaryDirectory(prefix=PREFIX) as tmp:
        home = Path(tmp) / 'home'; home.mkdir()
        bind = ('--mount', f'type=bind,source={home},target=/opt/data')
        ownership = docker('run', '--rm', '--network', 'none', '--entrypoint', 'stat', *bind, IMAGE, '-c', '%u:%g', '/opt/data')
        assert not ownership.startswith('10000:'), ownership
        print('FRESH_BIND_BEFORE ' + ownership)
        fresh = start('fresh-bind', bind)
        inspect('fresh-bind', fresh)
        docker('stop', '-t', '20', fresh)
        docker('rm', '-v', fresh); containers.remove(fresh)
        # A previous broken image may have left root-owned logs under an already-hermes-owned home.
        docker('run', '--rm', '--network', 'none', '--entrypoint', 'sh', *mount, IMAGE, '-c',
               'chown -R root:root /opt/data/logs; stat -c "%u:%g %n" /opt/data /opt/data/logs /opt/data/logs/agent.log')
    repaired = start('legacy-root-logs', mount)
    # Logs append across boots; the current gateway must still be unprivileged and unique.
    repair_rows = json.loads(py(repaired, PROCESSES))
    assert sum('gateway run --replace' in r['args'] and r['uid'] == 10000 for r in repair_rows) == 1
    assert not any('/opt/bh/bh-start.sh' in r['args'] or r['args'].startswith(('tail ', 'grep ')) for r in repair_rows)
    owners = py(repaired, "import os; print(all(os.stat(p).st_uid==10000 for p in ['/opt/data/logs','/opt/data/logs/agent.log']))")
    assert owners == 'True'
    print('PASS legacy-root-logs repaired to hermes')
    docker('stop', '-t', '20', repaired)
    docker('rm', '-v', repaired); containers.remove(repaired)
    with tempfile.TemporaryDirectory(prefix=PREFIX+'-invalid') as tmp:
        for i, data in enumerate([b'', b'# empty policy\n', b'plugins: {enabled: borrowed-hands}\n', b'plugins: {enabled: not-borrowed-hands-please}\n', b'plugins: null\n', b'plugins: [unclosed\n']):
            home=Path(tmp)/str(i); home.mkdir(); config=home/'config.yaml'; config.write_bytes(data); config.chmod(0o600)
            name=PREFIX+'-invalid-'+str(i); containers.append(name)
            docker('run','-d','--network','none','--name',name,'--mount',f'type=bind,source={home},target=/opt/data',IMAGE)
            code=docker('wait',name)
            assert code == '1', code
            logs=docker('logs',name)
            assert 'ERROR: config merge refused' in logs and 'original unchanged' in logs and '[bh-start] seeded' not in logs
            assert config.read_bytes()==data and config.stat().st_mode & 0o777 == 0o600
            print(f'PASS malformed-{i} exit=1 original_bytes_and_0600_preserved no_gateway_handoff')
            docker('rm','-v',name); containers.remove(name)
    elapsed=time.monotonic()-launched
    if elapsed < 40: time.sleep(40-elapsed)
    inspect('no-explicit-volume-after-40s', no_volume)
finally:
    for name in containers:
        subprocess.run(['docker','rm','-f','-v',name], capture_output=True)
    for volume in volumes:
        subprocess.run(['docker','volume','rm',volume], capture_output=True)
