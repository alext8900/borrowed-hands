"""Run an authorized build through the installed dispatcher."""
from __future__ import annotations

import asyncio
import json
import re

from .. import caps, schema, xcresult
from ..config import Policy
from . import AcceptedJob, JobOutcome

_LIMIT = 1024 * 1024

# One fixed, user-readable sentence per existing reason code (audit #5). Never runner text: the runner's own
# message can carry paths and remote details, so only the reason code chooses the sentence.
DISPATCH_MESSAGE = ("The Mac could not start or finish the build. On the Mac, run 'bh selftest' to see what is "
                    "missing; details are in ~/Library/Logs/BorrowedHands/helper.log.")
# `sudo -n -l` (setup, selftest) confirms each build command is permitted, not that it is password-free: a later
# sudo rule can make one need a password, and that only shows when the build runs. sudo -n then exits 1 with this
# text (LANG=C is set for the child), before the dispatcher prints anything.
SUDO_PASSWORD_STDERR = b"a password is required"
PASSWORD_MESSAGE = ("sudo asked for a password for the Borrowed Hands build command: another sudo rule on this Mac "
                    "overrides its password-free rule. Remove or reorder that rule, then run bh selftest.")
_STDERR_KEEP = 4096


async def _stderr_head(stream) -> bytes:
    """The first few KiB of the child's stderr; the rest is read and dropped so the child never blocks on it."""
    head = bytearray()
    while chunk := await stream.read(65536):
        if len(head) < _STDERR_KEEP:
            head.extend(chunk[:_STDERR_KEEP - len(head)])
    return bytes(head)


def _drain(p):
    """Start reading the child's stderr as soon as a process object exists, on every path: a child whose stderr
    pipe fills up can keep `p.wait()` from returning (Python 3.11 waits for the pipes as well as the exit)."""
    return asyncio.create_task(_stderr_head(p.stderr)) if p.stderr is not None else None


async def _settle(fut):
    """Await `fut` to completion even if the awaiting task is cancelled again meanwhile: cleanup that owns a child
    process must not be abandoned halfway. Ends early only if `fut` itself was cancelled (loop shutdown)."""
    fut = asyncio.ensure_future(fut)
    while True:
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            if fut.cancelled():
                raise
            # another cancel of the awaiting task: keep owning `fut`; the caller re-raises cancellation afterwards


REASON_MESSAGES = {
    'timeout': "The build ran past its time limit and was stopped.",
    'commit_not_found': ("The Mac could not fetch that commit from the granted repository. Check that it is "
                         "pushed to the public repository on a bh/* branch."),
    'not_on_bh_branch': ("That commit is not on a bh/* branch of the granted repository. Push it to a branch "
                         "named bh/<something>, then build again."),
    'unsupported_dependencies': ("The project uses remote Swift packages, which this version does not fetch. "
                                 "Vendor them into the repository as a local path package; detail.files lists "
                                 "the files that reference them."),
    'unsafe_tree': ("The build sandbox refused the checkout: it contains a symlink that points outside the "
                    "repository, an extra .git entry, or too many files."),
    'project_missing': ("The project path in the Mac's grant does not exist in that commit. The Mac owner can "
                        "check it with 'bh grant list'."),
    'invalid_job': ("The Mac's build runner refused the request, for example because the repository host did "
                    "not resolve to a public address. The Mac owner can check the grant with 'bh grant list'."),
    'disk_budget': "The build used more disk space than this Mac allows for builds and was stopped.",
    'memory_budget': "The build used more memory than this Mac allows for builds and was stopped.",
    'budget_check_failed': "The Mac could not measure the build's disk and memory use, so it stopped the build.",
    'dispatch_error': DISPATCH_MESSAGE,
}


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _cancelled():
    return JobOutcome('cancelled', None, {'code': 'cancelled', 'message': 'The build was cancelled.'})


def _clean(value, limit):
    text = xcresult._scrub(value, xcresult._make_ctx(None, []), limit)
    # Dispatcher data is untrusted, including paths outside the scrubber's known roots.
    text = re.sub(r'[^\s,;)]*(?:\.\./)+[^\s,;)]*', '<path>', text)
    return re.sub(r'(?<![\w])/(?:[^\s,;)]*)', '<path>', text)[:limit]


def _file(value):
    value = _clean(value, 500)
    return xcresult._file_field(value, xcresult._make_ctx(None, []))


def _map(raw, job):
    status = raw.get('status')
    if status in ('succeeded', 'failed'):
        errors, warnings = [], []
        truncated = False
        for key, target in (('errors', errors), ('warnings', warnings)):
            items = raw.get(key, [])
            if not isinstance(items, list):
                return JobOutcome.internal_error('invalid_result')
            truncated |= len(items) > 50
            for item in items[:50]:
                if not isinstance(item, dict) or not isinstance(item.get('message'), str):
                    return JobOutcome.internal_error('invalid_result')
                d = {'message': _clean(item['message'], 500), 'type': _clean(str(item.get('type', '')), 100)}
                if isinstance(item.get('file'), str):
                    d['file'] = _file(item['file'])
                for k in ('line', 'column'):
                    if type(item.get(k)) is int and 0 <= item[k] <= 10000000:
                        d[k] = item[k]
                target.append(d)
        def count(k, fallback):
            n = raw.get(k, fallback)
            return min(n, 10000000) if type(n) is int and n >= 0 else fallback
        duration = raw.get('duration_s', 0)
        if type(duration) not in (int, float) or not 0 <= duration <= 3600000:
            return JobOutcome.internal_error('invalid_result')
        result = {'status': status, 'duration_ms': int(duration * 1000), 'xcode': _clean(job.xcode_version, 100),
                  'destination': job.params['destination'], 'error_count': count('error_count', len(errors)),
                  'warning_count': count('warning_count', len(warnings)), 'errors': errors, 'warnings': warnings,
                  'log_tail': _clean(str(raw.get('log_tail', '')), 4000), 'truncated': truncated}
        try:
            schema.validate('job-result', {'job_id': job.job_id, 'state': status, 'result': result, 'error': None})
        except schema.SchemaError:
            return JobOutcome.internal_error('invalid_result')
        return JobOutcome(status, result, None)
    if status == 'cancelled':
        return _cancelled()
    mapping = {'timeout': ('timeout', 'timeout'), 'fetch_failed': ('needs_resolution', 'commit_not_found'),
               'not_reachable': ('needs_resolution', 'not_on_bh_branch'),
               'unsupported_dependencies': ('needs_resolution', 'unsupported_dependencies'),
               'unsafe_tree': ('invalid_arguments', 'unsafe_tree'),
               'disk_budget': ('internal_error', 'disk_budget'), 'memory_budget': ('internal_error', 'memory_budget'),
               'budget_check_failed': ('internal_error', 'budget_check_failed')}
    if status == 'invalid_job':
        mapping[status] = ('needs_resolution', 'project_missing') if 'project path missing' in str(raw.get('message', '')) else ('internal_error', 'invalid_job')
    code, reason = mapping.get(status, ('internal_error', 'dispatch_error'))
    detail = {'reason': reason}
    if status == 'unsupported_dependencies' and isinstance(raw.get('files'), list):
        detail['files'] = [_file(x) for x in raw['files'][:50] if isinstance(x, str)]
    return JobOutcome('failed', None, {'code': code, 'message': REASON_MESSAGES.get(reason, DISPATCH_MESSAGE),
                                       'detail': detail})


class XcodeExecutor:
    def __init__(self, policy=None, argv=None):
        self.policy = policy or Policy()
        self.argv = argv
        self._processes = {}
        self._cancelled = set()
        self._finished = set()

    async def cancel(self, job_id):
        if job_id in self._finished:
            return
        self._cancelled.add(job_id)
        p = self._processes.get(job_id)
        if p is not None and p.returncode is None:
            try:
                p.terminate()
            except ProcessLookupError:
                pass

    async def run(self, job: AcceptedJob, emit_state):
        out = await self._run_once(job)
        if out != "busy":
            self._finished.add(job.job_id)
            if len(self._finished) > 1024:
                self._finished.clear()
        return out

    async def _run_once(self, job: AcceptedJob):
        if job.job_id in self._cancelled:
            self._cancelled.discard(job.job_id)
            return _cancelled()
        ws = job.workspace
        if ws is None:
            return JobOutcome.internal_error('missing_grant')
        payload = {'v': 1, 'job_id': job.job_id, 'remote': ws.remote_url, 'project': ws.project,
                   'commit': job.params['commit'], 'scheme': job.params['scheme'],
                   'configuration': job.params['configuration'], 'destination': job.params['destination'],
                   'allowed_refs': 'refs/heads/bh/*'}
        argv = self.argv or [caps.SUDO, '-n', *caps.dispatch_argv(job.policy or self.policy)]
        p = None
        spawn = None
        err = None
        data = bytearray()
        try:
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'C'}))
            p = await asyncio.shield(spawn)
            err = _drain(p)
            self._processes[job.job_id] = p
            if job.job_id in self._cancelled:
                p.terminate()
            p.stdin.write(json.dumps(payload).encode())
            await p.stdin.drain()
            p.stdin.close()
            while True:
                chunk = await p.stdout.read(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > _LIMIT:
                    p.terminate()
                    await p.wait()
                    if job.job_id in self._cancelled:          # a cancel that arrived during the wait wins
                        return _cancelled()
                    return JobOutcome.internal_error('dispatch_error', DISPATCH_MESSAGE)
            await p.wait()
            if job.job_id in self._cancelled:
                return _cancelled()
            raw = json.loads(bytes(data), object_pairs_hook=_unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
            if not isinstance(raw, dict):
                return JobOutcome.internal_error('dispatch_error', DISPATCH_MESSAGE)
            if raw.get("status") == "busy":
                return "busy"
            if raw.get("status") == "succeeded" and p.returncode != 0:
                return JobOutcome.internal_error("dispatch_error", DISPATCH_MESSAGE)
            return _map(raw, job)
        except asyncio.CancelledError:
            # Repeated task cancellation must not abandon the child halfway: _settle keeps awaiting the spawn and
            # the exit, then the original cancellation propagates.
            if p is None and spawn is not None:
                try:
                    p = await _settle(spawn)
                except OSError:
                    pass
            if p is not None and err is None:
                err = _drain(p)                        # before terminate/wait: an unread stderr can block the wait
            if p is not None and p.returncode is None:
                try:
                    p.terminate()
                except ProcessLookupError:
                    pass
                await _settle(p.wait())
            raise
        except (OSError, ValueError, TypeError):
            if p is not None and p.returncode is None:
                try:
                    p.terminate()
                except ProcessLookupError:
                    pass
                await p.wait()
            if job.job_id in self._cancelled:
                return _cancelled()
            needed_password = await self._needed_password(p, err, data)
            if job.job_id in self._cancelled:              # cancel(job_id) during that wait still wins
                return _cancelled()
            if needed_password:
                return JobOutcome.internal_error('dispatch_error', PASSWORD_MESSAGE)
            return JobOutcome.internal_error('dispatch_error', DISPATCH_MESSAGE)
        finally:
            if err is not None and not err.done():
                err.cancel()
            self._processes.pop(job.job_id, None)
            self._cancelled.discard(job.job_id)

    @staticmethod
    async def _needed_password(p, err, data) -> bool:
        """sudo -n refused for want of a password: exit 1, nothing from the dispatcher, sudo's own text on stderr."""
        if p is None or err is None or p.returncode != 1 or data.strip():
            return False
        try:
            head = await asyncio.wait_for(asyncio.shield(err), 2)
        except (asyncio.TimeoutError, OSError):
            return False
        return SUDO_PASSWORD_STDERR in head


def make_executor(**kwargs):
    return XcodeExecutor()
