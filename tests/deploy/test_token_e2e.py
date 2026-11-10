#!/usr/bin/env python3
"""Boot the real image through s6 with a fake BH_GITHUB_TOKEN and prove where the token is and is not.

Docker only, --network none, fake token built at runtime and handed to the in-container checkers on stdin (never in
argv). Nothing here prints the token.

Process scans run twice, as root and as hermes: root in a default container has no CAP_SYS_PTRACE and cannot read
/proc/<pid>/environ of a uid-10000 process, so a root-only scan is blind to the gateway. The merged scan fails if any
process stays unreadable to both, and a planted uid-10000 process holding the token must be found.

Build first: docker build --network none --pull=false -t bh-token-e2e:test -f deploy/railway/Dockerfile .
Run: python3 tests/deploy/test_token_e2e.py bh-token-e2e:test
"""
import base64
import json
import os
import shlex
import subprocess
import sys
import time

IMAGE = sys.argv[1]
PREFIX = f'bh-token-e2e-{os.getpid()}'
TOKEN = 'github' + '_pat_' + '11BHE2E' + 'Q' * 75
REPO = 'octo-org/demo'
PY = '/opt/hermes/.venv/bin/python'
containers, volumes = [], []
fail = False


def docker(*args, stdin=None, check=True):
    print('$ docker ' + shlex.join(('<redacted>' if TOKEN in a else a.splitlines()[0][:60] if a else a) for a in args), flush=True)
    p = subprocess.run(['docker', *args], input=stdin, text=True, capture_output=True)
    if check and p.returncode:
        raise AssertionError(f'docker {args[0]} failed: {p.stderr}')
    return (p.stdout + (p.stderr if args[0] == 'logs' else '')).strip()


def check(label, cond):
    global fail
    print(('PASS ' if cond else 'FAIL ') + label, flush=True)
    fail |= not cond


# Runs as root or as hermes. Needle on stdin. Per process: readable or not, token in environ or argv (booleans),
# the non-secret GIT_CONFIG_* values, a short command line.
PROCS = r'''
import json, os, sys
needle = sys.stdin.read().encode()
me = os.getpid()
out = {}
for pid in filter(str.isdigit, os.listdir('/proc')):
    if int(pid) == me:
        continue
    try:
        uid = int([l for l in open(f'/proc/{pid}/status') if l.startswith('Uid:')][0].split()[1])
        cmdline = open(f'/proc/{pid}/cmdline', 'rb').read()
    except (OSError, IndexError):
        continue  # exited
    full = cmdline.replace(b'\0', b' ').decode(errors='replace')
    row = {'uid': uid, 'cmd': full[:70], 'gateway': ' gateway run' in full, 'argv_hit': needle in cmdline}
    try:
        environ = open(f'/proc/{pid}/environ', 'rb').read()
    except PermissionError:
        row['readable'] = False
    except OSError:
        continue
    else:
        pairs = [e.split(b'=', 1) for e in environ.split(b'\0') if b'=' in e]
        names = {k for k, _ in pairs}
        row.update(readable=True, env_hit=needle in environ or b'BH_GITHUB_TOKEN' in names or b'BH_GITHUB_REPO' in names,
                   git_config={k.decode(): v.decode() for k, v in pairs if k.startswith(b'GIT_CONFIG_')})
    out[pid] = row
print(json.dumps(out))
'''

# Runs as root. Every regular file under these trees that holds the needle.
FILES = r'''
import json, os, sys
needle = sys.stdin.buffer.read()
hits = []
for root in ('/opt/data', '/opt/bh', '/etc', '/run', '/tmp', '/var', '/root', '/home'):
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            try:
                if os.path.isfile(p) and not os.path.islink(p) and needle in open(p, 'rb').read():
                    hits.append(p)
            except OSError:
                pass
print(json.dumps(sorted(hits)))
'''

# Runs as root: modes, owners, s6 container environment.
STATE = r'''
import json, os, stat, sys
from pathlib import Path
needle = sys.stdin.read().encode()
ce = Path('/run/s6/container_environment')
ce_names = sorted(x.name for x in ce.iterdir()) if ce.is_dir() else None
def meta(p, content=False):
    if not os.path.lexists(p):
        return None
    s = os.lstat(p)
    m = [s.st_uid, oct(stat.S_IMODE(s.st_mode))]
    return m + [needle in open(p, 'rb').read()] if content else m
d = '/etc/bh-github/creds'
print(json.dumps({'ce_dir': ce_names is not None,
                  'ce_token_names': [n for n in (ce_names or []) if n.startswith('BH_GITHUB')],
                  'ce_hit': any(needle in x.read_bytes() for x in ce.iterdir()) if ce_names is not None else None,
                  'dir': meta(d), 'files': {f: meta(f'{d}/{f}', True) for f in ('git-credentials', 'api-header')
                                            if os.path.lexists(f'{d}/{f}')},
                  'gitconfig': meta('/etc/bh-github/gitconfig', True)}))
'''

# Runs as hermes with the gateway's GIT_CONFIG_* (passed as JSON in argv: not secret). Git fill per URL, curl -H @file
# against a loopback listener, and the second-helper copy test with and without the gateway's environment include.
USE = r'''
import json, os, socket, subprocess, sys, tempfile, threading
needle = sys.stdin.read()
gitenv = json.loads(sys.argv[1])
base = {'PATH': os.environ['PATH'], 'HOME': '/opt/data/home', 'GIT_TERMINAL_PROMPT': '0', **gitenv}
out = {}
for url in ['https://github.com/octo-org/demo.git', 'https://github.com/octo-org/demo',
            'https://github.com/octo-org/other.git', 'https://github.com/someone/demo.git', 'https://gitlab.com/octo-org/demo.git']:
    p = subprocess.run(['git', 'credential', 'fill'], input=f'url={url}\n\n', text=True, capture_output=True, env=base)
    out[url] = ('password=' + needle + '\n') in p.stdout
srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen(1); srv.settimeout(20)
got = []
def serve():
    try:
        conn, _ = srv.accept(); got.append(conn.recv(65536)); conn.sendall(b'HTTP/1.1 204 No Content\r\n\r\n'); conn.close()
    except OSError:
        pass  # curl never connected (e.g. it could not read the header file): curl_header stays False
t = threading.Thread(target=serve, daemon=True); t.start()
subprocess.run(['curl', '-sS', '-o', '/dev/null', '-H', '@/etc/bh-github/creds/api-header',
                f'http://127.0.0.1:{srv.getsockname()[1]}/repos/octo-org/demo'], capture_output=True, timeout=20)
t.join(5)
out['curl_header'] = bool(got) and ('\r\nAuthorization: Bearer ' + needle + '\r\n') in got[0].decode()
# The bundled GitHub skill's step: a user-level `credential.helper store`. Then what git does after a good push.
for label, env_include in (('copy_with_gateway_env', True), ('copy_without_env_include', False)):
    home = tempfile.mkdtemp()
    open(f'{home}/.gitconfig', 'w').write('[credential]\n\thelper = store\n')
    env = {**base, 'HOME': home} if env_include else {'PATH': base['PATH'], 'HOME': home, 'GIT_TERMINAL_PROMPT': '0'}
    filled = subprocess.run(['git', 'credential', 'fill'], input='url=https://github.com/octo-org/demo.git\n\n',
                            text=True, capture_output=True, env=env).stdout
    subprocess.run(['git', 'credential', 'approve'], input=filled, text=True, capture_output=True, env=env)
    out[label] = os.path.exists(f'{home}/.git-credentials')
    out[label.replace('copy', 'offered')] = ('password=' + needle + '\n') in filled
    subprocess.run(['rm', '-rf', home])
print(json.dumps(out))
'''

# Runs as hermes with HOME=HERMES_HOME=/opt/data: Hermes's own MEDIA delivery validator, both modes. Booleans only.
MEDIA = r"""
import json, os, subprocess, sys
paths = sys.argv[1:]
code = ("import json, sys; sys.path.insert(0, '/opt/hermes'); "
        "from gateway.platforms.base import validate_media_delivery_path as v; "
        "print(json.dumps({p: v(p) is not None for p in sys.argv[1:]}))")
out = {}
for strict in ('0', '1'):
    env = {**os.environ, 'HOME': '/opt/data', 'HERMES_HOME': '/opt/data', 'HERMES_MEDIA_DELIVERY_STRICT': strict}
    env.pop('HERMES_MEDIA_ALLOW_DIRS', None)
    r = subprocess.run([sys.executable, '-c', code, *paths], env=env, capture_output=True, text=True, cwd='/opt/data')
    out[strict] = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {'error': r.stderr[-300:]}
print(json.dumps(out))
"""

# Runs as hermes: the stock `hermes backup`, then which archive entries hold the needle (needle on stdin).
BACKUP = r"""
import json, os, subprocess, sys, zipfile
needle = sys.stdin.buffer.read()
out = '/tmp/bh-e2e-backup.zip'
r = subprocess.run(['/opt/hermes/.venv/bin/hermes', 'backup', '-o', out], capture_output=True, text=True, cwd='/opt/data',
                   env={**os.environ, 'HOME': '/opt/data', 'HERMES_HOME': '/opt/data'})
z = zipfile.ZipFile(out)
names = z.namelist()
print(json.dumps({'rc': r.returncode, 'entries': len(names), 'has_config': any(n.endswith('config.yaml') for n in names),
                  'holding': [n for n in names if needle in z.read(n)]}))
os.unlink(out)
"""

HEALTH = r'''import json, time, urllib.request
end = time.monotonic() + 90
while time.monotonic() < end:
    try:
        with urllib.request.urlopen('http://127.0.0.1:8790/bh/v1/healthz', timeout=1) as r:
            if r.status == 200 and json.load(r) == {'ok': True, 'v': 1}:
                print('ok'); break
    except Exception:
        pass
    time.sleep(1)
else:
    raise SystemExit('healthz did not become ready in 90s')
'''


def boot(label, volume, env):
    name = f'{PREFIX}-{label}'
    containers.append(name)
    args = ['run', '-d', '--network', 'none', '--name', name, '-e', 'BH_OWNERS=cli:local',
            '--mount', f'type=volume,source={volume},target=/opt/data']
    for k, v in env.items():
        args += ['-e', f'{k}={v}']
    docker(*args, IMAGE)
    check(f'{label} gateway healthy', docker('exec', '--user', 'hermes', name, PY, '-c', HEALTH, check=False) == 'ok')
    time.sleep(2)
    return name


def run_in(name, code, user='root', args=()):
    # env -u: docker exec copies the container's configured env (the variable included) into exec'd processes;
    # drop it before the checker starts so it sees only the container's own processes.
    return json.loads(docker('exec', '-i', '--user', user, name, 'env', '-u', 'BH_GITHUB_TOKEN', '-u', 'BH_GITHUB_REPO',
                             PY, '-c', code, *args, stdin=TOKEN))


def scan(name):
    """Merge root and hermes scans. Returns (rows by pid, pids unreadable by both). Retries for exiting processes."""
    for _ in range(3):
        a, b = run_in(name, PROCS, 'root'), run_in(name, PROCS, 'hermes')
        merged = {}
        for pid in set(a) | set(b):
            rows = [r for r in (a.get(pid), b.get(pid)) if r]
            readable = [r for r in rows if r['readable']]
            merged[pid] = dict(readable[0] if readable else rows[0],
                               argv_hit=any(r['argv_hit'] for r in rows))
        blind = sorted(p for p, r in merged.items() if not r['readable'])
        if not blind:
            break
        time.sleep(1)
    return merged, blind


def stop(name):
    docker('rm', '-f', name)
    containers.remove(name)


try:
    volume = PREFIX + '-data'
    volumes.append(volume)
    docker('volume', 'create', volume)

    # 0. Upgrade: the volume still carries the credential where the previous image wrote it. The boot below must
    # sweep it (the holder walk in step 1 then finds nothing under /opt/data).
    docker('run', '--rm', '-i', '--network', 'none', '--user', 'hermes', '--entrypoint', 'sh',
           '--mount', f'type=volume,source={volume},target=/opt/data', IMAGE, '-c',
           'umask 077; mkdir -p /opt/data/.config/bh-github && cd /opt/data/.config/bh-github && '
           'read -r t && printf "Authorization: Bearer %s\\n" "$t" > api-header && '
           'printf "https://x-access-token:%s@github.com/octo-org/demo.git\\n" "$t" > git-credentials', stdin=TOKEN + '\n')

    # 1. Valid token: files in place, token in no process environment, argv or s6 container environment.
    c = boot('valid', volume, {'BH_GITHUB_TOKEN': TOKEN, 'BH_GITHUB_REPO': REPO})
    check('upgrade old volume location swept at boot', 'removed a credential from the old location' in docker('logs', c))
    procs, blind = scan(c)
    print('PROCS valid ' + json.dumps({p: {k: r[k] for k in ('uid', 'readable', 'cmd')} for p, r in procs.items()}), flush=True)
    check('valid every process scanned (none unreadable to both root and hermes)', blind == [])
    check('valid scan covered uid-10000 processes', any(r['uid'] == 10000 and r['readable'] for r in procs.values()))
    gateways = [r for r in procs.values() if r['uid'] == 10000 and r['gateway'] and r['readable']]
    check('valid gateway (uid 10000) found and read', len(gateways) >= 1)
    check('valid token in no process environment (incl. gateway, s6, PID 1)', not any(r.get('env_hit') for r in procs.values()))
    check('valid token in no process argv', not any(r['argv_hit'] for r in procs.values()))
    gitenv = gateways[0]['git_config'] if gateways else {}
    check('valid gateway env carries the git config include',
          gitenv == {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'include.path', 'GIT_CONFIG_VALUE_0': '/etc/bh-github/gitconfig'})
    s = run_in(c, STATE)
    print('STATE valid ' + json.dumps(s), flush=True)
    check('valid s6 container_environment exists and lacks the variables', s['ce_dir'] and s['ce_token_names'] == [])
    check('valid s6 container_environment holds no token value', s['ce_hit'] is False)
    check('valid dir hermes 0700', s['dir'] == [10000, '0o700'])
    check('valid files hermes 0600 holding the token',
          s['files'] == {'git-credentials': [10000, '0o600', True], 'api-header': [10000, '0o600', True]})
    check('valid git config root 0644 without token', s['gitconfig'] == [0, '0o644', False])
    holders = run_in(c, FILES)
    check('valid exactly the two credential files under /etc/bh-github hold the token, nothing under /opt/data',
          holders == ['/etc/bh-github/creds/api-header', '/etc/bh-github/creds/git-credentials'])
    # The stock backup archives HERMES_HOME (the volume): it must not contain the token. Control: a planted copy on
    # the volume does land in the archive, so an empty result is not a blind check.
    b = run_in(c, BACKUP, 'hermes')
    print('BACKUP ' + json.dumps(b), flush=True)
    check('valid hermes backup runs and archives the volume', b['rc'] == 0 and b['entries'] > 0 and b['has_config'])
    check('valid hermes backup holds no token', b['holding'] == [])
    docker('exec', '-i', '--user', 'hermes', c, 'sh', '-c', 'cat > /opt/data/home/bh-e2e-canary', stdin=TOKEN)
    b = run_in(c, BACKUP, 'hermes')
    check('control hermes backup does pick up a token planted on the volume', b['holding'] == ['home/bh-e2e-canary'])
    # MEDIA: both credential files, and a symlink to one placed on the volume, refused in both modes. Control: a
    # plain copy on the volume (the canary) is deliverable without strict mode, so the validator is really answering.
    docker('exec', '--user', 'hermes', c, 'ln', '-s', '/etc/bh-github/creds/git-credentials', '/opt/data/home/bh-e2e-link')
    targets = ['/etc/bh-github/creds/git-credentials', '/etc/bh-github/creds/api-header', '/etc/bh-github/gitconfig',
               '/opt/data/home/bh-e2e-link', '/opt/data/home/bh-e2e-canary']
    m = json.loads(docker('exec', '--user', 'hermes', c, PY, '-c', MEDIA, *targets))
    print('MEDIA ' + json.dumps(m), flush=True)
    check('valid MEDIA refuses the credential files, the git config and a link to them, both modes',
          all(m[k].get(t) is False for k in ('0', '1') for t in targets[:4]))
    check('control MEDIA delivers a plain copy on the volume without strict mode', m['0'].get(targets[4]) is True)
    docker('exec', '--user', 'hermes', c, 'rm', '-f', '/opt/data/home/bh-e2e-canary', '/opt/data/home/bh-e2e-link')
    use = run_in(c, USE, 'hermes', (json.dumps(gitenv),))
    print('USE ' + json.dumps(use), flush=True)
    check('valid git sends token only for the repo, curl -H @file sends the header', use == {
        'https://github.com/octo-org/demo.git': True, 'https://github.com/octo-org/demo': True,
        'https://github.com/octo-org/other.git': False, 'https://github.com/someone/demo.git': False,
        'https://gitlab.com/octo-org/demo.git': False, 'curl_header': True,
        'copy_with_gateway_env': False, 'copy_without_env_include': False,
        'offered_with_gateway_env': True, 'offered_without_env_include': False})
    # Positive control, the reviewer's case: a uid-10000 process holding the token in its environment (docker exec
    # copies the container's configured env) must be found, and a root one too.
    docker('exec', '-d', '--user', 'hermes', c, 'sleep', '30')
    docker('exec', '-d', c, 'sleep', '31')
    time.sleep(1)
    procs, blind = scan(c)
    hits = [r for r in procs.values() if r.get('env_hit')]
    check('control scanner finds a planted uid-10000 process', any(r['uid'] == 10000 and 'sleep 30' in r['cmd'] for r in hits))
    check('control scanner finds a planted root process', any(r['uid'] == 0 and 'sleep 31' in r['cmd'] for r in hits))
    logs = docker('logs', c)
    check('valid start log says configured, names no repository',
          'github credentials: configured for the repository named in BH_GITHUB_REPO' in logs and 'octo-org' not in logs)
    stop(c)
    all_logs = logs

    # 2. Same volume, token variable removed: credential deleted, gateway still boots.
    c = boot('removed', volume, {'BH_GITHUB_REPO': REPO})
    s = run_in(c, STATE)
    check('removed files and dir gone', s['dir'] is None and s['files'] == {})
    check('removed git config absent', s['gitconfig'] is None)
    check('removed no file holds the token', run_in(c, FILES) == [])
    all_logs += docker('logs', c)
    stop(c)

    # 3. Malformed token: refused, nothing written, gateway still boots.
    c = boot('malformed', volume, {'BH_GITHUB_TOKEN': 'ghp_' + 'Q' * 36, 'BH_GITHUB_REPO': REPO})
    s = run_in(c, STATE)
    check('malformed nothing written', s['dir'] is None and s['gitconfig'] is None)
    logs = docker('logs', c)
    check('malformed refusal names the variable only',
          'refused: BH_GITHUB_TOKEN is not a fine-grained personal access token' in logs and 'Q' * 36 not in logs)
    all_logs += logs
    stop(c)

    for form in (TOKEN, base64.b64encode(TOKEN.encode()).decode(), base64.b64encode(f'x-access-token:{TOKEN}'.encode()).decode()):
        check('container logs carry no token form', form not in all_logs)
    check('container logs carry no token body, prefix or marker',
          'Q' * 8 not in all_logs and '_pat_' not in all_logs and '11BHE2E' not in all_logs)
finally:
    for name in containers:
        subprocess.run(['docker', 'rm', '-f', '-v', name], capture_output=True)
    for volume in volumes:
        subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True)

sys.exit(1 if fail else 0)
