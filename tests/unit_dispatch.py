#!/usr/bin/python3 -I
"""Unit tests for bh-dispatch / bh-runner internals. Run as root on the build Mac:
    sudo -n /usr/bin/python3 -I tests/unit_dispatch.py [path/to/bh-dispatch]
Uses only a scratch dir this test creates (/private/tmp/bh-unit-*), removed at the end. Hardlink
cases link to a root-owned JSON file created here, never to system files.
"""
import importlib.machinery
import contextlib
import fcntl
import io
import json
import os
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.realpath(__file__))
DISPATCH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "sandbox", "bh-dispatch")
RESULTS = []


def check(name, cond):
    RESULTS.append(bool(cond))
    print("%s - %s" % ("ok  " if cond else "FAIL", name))


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = types.ModuleType(name)
    mod.__file__ = path
    loader.exec_module(mod)
    return mod


D = load(DISPATCH, "bh_dispatch")
D.R = load(sys.argv[2], "bh_runner") if len(sys.argv) > 2 else D.load_runner()
R = D.R                       # optional runner copy is only for root unit mutation checks
UID = R.BUILDER_UID
assert os.geteuid() == 0, "run as root"

scratch = tempfile.mkdtemp(prefix="bh-unit-", dir="/private/tmp")
try:
    # The helper treats idempotent cancellation as successful cleanup only with status AND exit code.
    real_stdin, real_lock = sys.stdin, D.LOCK
    cancel_id = "01234567-89ab-4def-8012-3456789abcde"
    lock_path = os.path.join(scratch, "cancel.lock")

    def cancel_reply():
        out = io.StringIO()
        sys.stdin = types.SimpleNamespace(buffer=io.BytesIO((cancel_id + "\n").encode()))
        try:
            with contextlib.redirect_stdout(out):
                try:
                    D.cancel_inflight()
                except SystemExit as exc:
                    return exc.code, json.loads(out.getvalue())
        finally:
            sys.stdin = real_stdin

    try:
        D.LOCK = lock_path
        rc, reply = cancel_reply()
        check("cancel with no lock returns not_running and exit 0", rc == 0 and reply["status"] == "not_running")
        with open(lock_path, "w") as f:
            f.write("99999999 11111111-1111-4111-8111-111111111111\n")
        rc, reply = cancel_reply()
        check("cancel with unheld lock returns not_running and exit 0", rc == 0 and reply["status"] == "not_running")
        with open(lock_path) as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            rc, reply = cancel_reply()
            check("cancel of a different job returns not_running and exit 0", rc == 0 and reply["status"] == "not_running")
        for status in ("usage", "error"):
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    D.fail(status, "unit refusal")
                except SystemExit as exc:
                    rc = exc.code
            check("%s keeps nonzero exit" % status, rc == 1)
    finally:
        sys.stdin, D.LOCK = real_stdin, real_lock

    job = os.path.join(scratch, "job")
    os.mkdir(job, 0o700)
    os.chown(job, UID, UID)
    good = json.dumps({"v": 1, "status": "succeeded"})

    def put(content, mode=0o600, uid=UID, name="result.json"):
        p = os.path.join(job, name)
        if os.path.lexists(p):
            os.unlink(p)
        with open(p, "w") as f:
            f.write(content)
        os.chmod(p, mode)
        os.chown(p, uid, uid)
        return p

    # ---- read_result (MEDIUM 4)
    put(good)
    check("read_result accepts a 0600, uid-470, single-link regular file", D.read_result(job) == {"v": 1, "status": "succeeded"})
    put(good, mode=0o644)
    check("read_result rejects mode 0644", D.read_result(job) is None)
    put(good, uid=0)
    check("read_result rejects a root-owned file", D.read_result(job) is None)
    # hardlink: result.json -> a root-owned JSON file we created in the scratch dir
    rootfile = os.path.join(scratch, "root-owned.json")
    with open(rootfile, "w") as f:
        f.write(good)
    os.chmod(rootfile, 0o600)
    os.unlink(os.path.join(job, "result.json"))
    os.link(rootfile, os.path.join(job, "result.json"))
    check("read_result rejects a hardlink to a root-owned file", D.read_result(job) is None)
    os.unlink(os.path.join(job, "result.json"))
    # hardlink between two uid-470 files: owner and mode right, but st_nlink == 2
    p = put(good, name="other.json")
    os.link(p, os.path.join(job, "result.json"))
    check("read_result rejects nlink 2 even when owner/mode are right", D.read_result(job) is None)
    os.unlink(os.path.join(job, "result.json"))
    os.unlink(p)
    # symlink to a file that would pass every other check (uid 470, 0600, one link): only
    # O_NOFOLLOW stops it
    target = os.path.join(scratch, "builder-owned.json")
    with open(target, "w") as f:
        f.write(good)
    os.chmod(target, 0o600)
    os.chown(target, UID, UID)
    os.symlink(target, os.path.join(job, "result.json"))
    check("read_result rejects a symlink (to an otherwise valid uid-470 file)", D.read_result(job) is None)
    os.unlink(os.path.join(job, "result.json"))
    os.mkfifo(os.path.join(job, "result.json"), 0o600)
    os.chown(os.path.join(job, "result.json"), UID, UID)
    check("read_result rejects a FIFO (and does not block)", D.read_result(job) is None)
    os.unlink(os.path.join(job, "result.json"))
    put("[" * 5000)
    check("read_result rejects deeply nested JSON without raising", D.read_result(job) is None)

    # ---- sweep failures (MEDIUM 4): pre-sweep failure aborts before any launchctl/bootstrap,
    # post-sweep failure ignores result.json.
    calls = []
    real = {k: getattr(D, k) for k in ("sweep", "launchctl", "make_job_dir", "job_state", "bootout", "test_mode", "as_builder")}

    def run_main(stdin_obj, args=()):
        out = io.StringIO()
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin = types.SimpleNamespace(buffer=io.BytesIO(json.dumps(stdin_obj).encode()))
        sys.stdout = out
        try:
            rc = D.main(list(args))
        except SystemExit as e:
            rc = e.code
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        line = out.getvalue().strip().splitlines()[-1]
        return rc, json.loads(line)

    jid = "11111111-2222-4333-8444-555555555555"
    jobj = {"v": 1, "job_id": jid, "remote": "file:///private/tmp/bh-stage-unit/x.git", "commit": "a" * 40,
            "project": "A.xcodeproj", "scheme": "A", "configuration": "Debug", "destination": "platform=macOS,arch=arm64",
            "allowed_refs": "refs/heads/bh/*"}
    D.test_mode = lambda: True
    D.LOCK = os.path.join(scratch, "lock1")
    D.launchctl = lambda *a: (calls.append(a), types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1]
    D.bootout = lambda: calls.append(("bootout",))
    D.sweep = lambda: False
    D.make_job_dir = lambda *a: (calls.append(("make_job_dir",)), True)[1]
    rc, res = run_main(jobj, ["--allow-file-remote"])
    check("pre-sweep failure -> status error", res.get("status") == "error" and "refusing" in res.get("message", ""))
    check("pre-sweep failure -> no job dir and no bootstrap", ("make_job_dir",) not in calls
          and not any(c and c[0] == "bootstrap" for c in calls))

    # post-sweep failure: job "runs" (mocked), a perfect result.json exists, sweep then fails
    calls.clear()
    D.LOCK = os.path.join(scratch, "lock2")
    seq = iter([True, False])
    D.sweep = lambda: next(seq)
    jobdir = os.path.join(scratch, "jobs", jid)
    R.JOBS_ROOT = os.path.join(scratch, "jobs")
    os.makedirs(jobdir)
    for d in (os.path.dirname(jobdir), jobdir):
        os.chown(d, UID, UID)
        os.chmod(d, 0o700)
    os.rmdir(jobdir)

    def fake_make(job_dir, job):
        os.mkdir(job_dir, 0o700)
        os.chown(job_dir, UID, UID)
        for name, content in (("result.json", good),):
            p = os.path.join(job_dir, name)
            with open(p, "w") as f:
                f.write(content)
            os.chmod(p, 0o600)
            os.chown(p, UID, UID)
        return True
    D.make_job_dir = fake_make
    D.job_state = lambda: (False, "0")
    D.as_builder = lambda fn, *a: 0
    orig_plist_dir = D.PLIST_DIR
    D.PLIST = os.path.join(scratch, "test.plist")
    rc, res = run_main(jobj, ["--allow-file-remote"])
    check("post-sweep failure -> status error, result.json (succeeded) ignored",
          res.get("status") == "error" and "post-job sweep failed" in res.get("message", ""))
    for k, v in real.items():
        setattr(D, k, v)

    # ---- LOW 6: hostile JSON / URLs never raise out of the validators
    try:
        R.parse_job_json(b"[" * 2000 + b"]" * 2000)
        ok = False
    except R.JobError as e:
        ok = e.status == "invalid_job"
    check("2000-deep JSON -> invalid_job (no RecursionError)", ok)
    for url in ("https://github.com:99999/x/y.git", "https://[::1/x.git", "https://github.com:-1/x.git"):
        try:
            R.validate_remote(url, False, resolve=False)
            ok = False
        except R.JobError as e:
            ok = e.status == "invalid_job"
        check("validate_remote(%s) -> invalid_job" % url, ok)

    # ---- LOW 7: per-user dirs must be real, under /private/var/folders, no SBPL metacharacters
    real_run = R.subprocess.run
    for bad in ('/private/var/folders/x"y/T/', "/tmp/T/", "/private/var/folders/../../etc/"):
        R.subprocess.run = lambda *a, _b=bad, **k: types.SimpleNamespace(stdout=_b, returncode=0)
        try:
            R.user_dir("DARWIN_USER_TEMP_DIR")
            ok = False
        except R.JobError:      # only the explicit rejection counts (no blanket OSError pass)
            ok = True
        except Exception:
            ok = False
        check("user_dir rejects %r" % bad, ok)
    R.subprocess.run = real_run
    # ---- HIGH: runner budget checks fail closed
    rn = R.Runner.__new__(R.Runner)
    rn.disk_mb, rn.mem_mb = 1000, 1000
    def boom():
        raise PermissionError("unreadable")
    rn.disk_used_mb = boom
    rn.mem_used_mb = lambda: 1
    try:
        rn.check_budgets(); st = None
    except R.JobError as e:
        st = e.status
    check("runner: disk measurement failure -> budget_check_failed", st == "budget_check_failed")
    rn.disk_used_mb = lambda: 1
    R.subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout="", returncode=1)
    try:
        R.Runner.mem_used_mb(rn); st = None
    except OSError:
        st = "oserror"
    R.subprocess.run = real_run
    check("runner: ps failure -> mem_used_mb raises (no silent zero)", st == "oserror")
    del rn.mem_used_mb
    R.subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout="12 abc\n", returncode=0)
    try:
        rn.check_budgets(); st = None
    except R.JobError as e:
        st = e.status
    R.subprocess.run = real_run
    check("runner: unparsable ps output -> budget_check_failed", st == "budget_check_failed")

    # ---- MEDIUM/LOW: resolution checks and the DNS pin
    real_gai = R.socket.getaddrinfo
    def gai(addrs):
        return lambda *a, **k: [(R.socket.AF_INET6 if ":" in x else R.socket.AF_INET, 1, 6, "", (x, 443)) for x in addrs]
    for bad in ("2002:7f00:1::1", "2001:0:4136:e378::1", "64:ff9b::7f00:1", "::ffff:10.0.0.1", "100.64.1.1", "fd12::1"):
        R.socket.getaddrinfo = gai([bad])
        try:
            R.resolve_public("evil.example"); ok = False
        except R.JobError as e:
            ok = e.status == "invalid_job"
        check("resolve_public rejects %s" % bad, ok)
    R.socket.getaddrinfo = gai(["140.82.112.3", "2606:4700::1"])
    check("resolve_public accepts public v4+v6", R.resolve_public("github.com") == ["140.82.112.3", "2606:4700::1"])
    rn = R.Runner.__new__(R.Runner)
    rn.job = {"remote": "https://github.com/octocat/Hello-World.git"}
    rn.pin = rn.pin_remote()
    argv = rn.git("fetch")
    check("git argv pins the checked address (http.curloptResolve=github.com:443:140.82.112.3)",
          "http.curloptResolve=github.com:443:140.82.112.3" in argv)
    R.socket.getaddrinfo = real_gai

    # ---- LOW 4: validate_job_dir negatives (no filesystem changes)
    good_id = "aaaaaaaa-2222-4333-8444-55555555beef"
    for bad in (R.JOBS_ROOT + "/../jobs/" + good_id, R.JOBS_ROOT + "/" + good_id.upper(),
                "/tmp/jobs/" + good_id, R.JOBS_ROOT + "/" + good_id + "/", R.JOBS_ROOT + "/x" + good_id,
                R.JOBS_ROOT + "/" + good_id + "\x00", R.JOBS_ROOT + "/" + good_id.replace("-4333-", "-1333-")):
        try:
            R.validate_job_dir(bad); ok = False
        except R.JobError:
            ok = True
        check("validate_job_dir rejects %r" % bad.replace(R.JOBS_ROOT, "<jobs>"), ok)

    # ---- LOW 4: create_runner_err negatives (scratch dirs only)
    cdir = os.path.join(scratch, "cre")
    os.mkdir(cdir, 0o700); os.chown(cdir, UID, UID)
    p0 = D.create_runner_err(cdir)
    st0 = os.lstat(p0)
    check("create_runner_err makes a fresh 0600 uid-470 file", st0.st_uid == UID and (st0.st_mode & 0o777) == 0o600)
    try:
        D.create_runner_err(cdir); ok = False          # already exists: O_EXCL
    except OSError:
        ok = True
    check("create_runner_err refuses an existing runner.err", ok)
    os.unlink(p0); os.symlink(rootfile, p0)
    try:
        D.create_runner_err(cdir); ok = False          # symlink planted at the name
    except OSError:
        ok = True
    check("create_runner_err refuses a planted symlink", ok)
    os.unlink(p0)
    rdir = os.path.join(scratch, "rootdir"); os.mkdir(rdir, 0o700)
    try:
        D.create_runner_err(rdir); ok = False          # job dir not owned by 470
    except OSError:
        ok = True
    check("create_runner_err refuses a job dir not owned by uid 470", ok)
    ldir = os.path.join(scratch, "linkdir"); os.symlink(cdir, ldir)
    try:
        D.create_runner_err(ldir); ok = False          # job dir itself a symlink
    except OSError:
        ok = True
    check("create_runner_err refuses a symlinked job dir", ok)

    # ---- MEDIUM 1: teardown survives fork failures (EAGAIN) and still completes every step
    real2 = {k: getattr(D, k) for k in ("bootout", "sweep", "as_builder", "loaded", "TEARDOWN_DEADLINE_S")}
    fails = {"bootout": 3, "sweep": 3}
    calls2 = []
    def flaky(name, value):
        def f(*a, **k):
            calls2.append(name)
            if fails.get(name, 0) > 0:
                fails[name] -= 1
                raise OSError(35, "Resource temporarily unavailable")
            return value
        return f
    D.bootout = flaky("bootout", None)
    D.sweep = flaky("sweep", True)
    D.as_builder = lambda fn, *a: 0
    D.loaded = lambda: False
    D.PLIST = os.path.join(scratch, "teardown.plist"); open(D.PLIST, "w").close()
    disp = {}
    try:
        D.teardown(disp); crashed = False
    except Exception as e:
        crashed = repr(e)
    check("teardown retries through EAGAIN (no crash): %s" % (crashed or "ok"), crashed is False)
    check("teardown completed every step after retries", disp.get("post_sweep_ok") is True and calls2.count("bootout") == 4
          and calls2.count("sweep") == 4 and disp.get("label_gone") is True and not os.path.exists(D.PLIST))
    for k, v in real2.items():
        setattr(D, k, v)

    # ---- layered gates: even if the exact-argv check were wrong, the TEST_MODE gate alone must still
    # refuse test-only flags in production (each layer has its own test)
    real3 = {k: getattr(D, k) for k in ("production_argv_ok", "test_mode", "LOCK")}
    D.production_argv_ok = lambda a: True
    D.test_mode = lambda: False
    D.LOCK = os.path.join(scratch, "lock3")
    for flags in (["--profile", "allow-all.sb"], ["--allow-file-remote"], ["--keep-build-dirs"]):
        rc, res = run_main(jobj, flags)
        check("TEST_MODE gate alone refuses %s in production" % flags[0], res.get("status") == "usage")
    for k, v in real3.items():
        setattr(D, k, v)

    # ---- allowed_refs is exact
    base_job = dict(jobj)
    for bad in ("refs/heads/*", "refs/heads/bh/", "refs/heads/bh/**", "refs/heads/bh/x", "", None):
        j = dict(base_job, allowed_refs=bad)
        try:
            R.validate_job(j, j["job_id"], True); ok = False
        except R.JobError as e:
            ok = e.status == "invalid_job"
        check("validate_job rejects allowed_refs=%r" % (bad,), ok)
    try:
        R.validate_job(dict(base_job), base_job["job_id"], True); ok = True
    except R.JobError:
        ok = False
    check("validate_job accepts allowed_refs=refs/heads/bh/*", ok)

    # ---- .git lookalikes and tree safety (ported from helper gitmat tests)
    for name in (".git", ".GIT", ".git\n", ".git\u200b", "\uff0egit", ".\uff47it", ".git\ufe0f", ".gi\u0307t", ".g\u200bit "):
        check("dotgit_like(%r)" % name, R.dotgit_like(name))
    for name in (".gitignore", "git", ".github", "a.git", ".gitmodules"):
        check("not dotgit_like(%r)" % name, not R.dotgit_like(name))
    def tree(entries):
        root = tempfile.mkdtemp(prefix="tree-", dir=scratch)
        open(os.path.join(root, ".git"), "w").write("gitdir: x\n")   # a worktree's own .git file
        os.makedirs(os.path.join(root, "d"))
        open(os.path.join(root, "d", "t.txt"), "w").write("t")
        for kind, name, target in entries:
            p = os.path.join(root, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if kind == "link":
                os.symlink(target, p)
            else:
                os.makedirs(p)
        return root
    def unsafe(entries):
        try:
            R.check_tree(tree(entries)); return False
        except R.JobError as e:
            return e.status == "unsafe_tree"
    for target in ("/opt/nonexistent-bh/x", "../../../.ssh/id_rsa", "../outside", ".git", "sub/../.git"):
        check("check_tree rejects symlink -> %s" % target, unsafe([("link", "ln", target)]))
    check("check_tree rejects a symlink chain that escapes", unsafe([("link", "a", "/opt"), ("link", "b", "a/x")]))
    check("check_tree rejects nested symlink escaping", unsafe([("link", "d/e/ln", "../../../../x")]))
    for name in (".git\u200b", "\uff0egit", "sub/.GIT"):
        check("check_tree rejects lookalike entry %r" % name, unsafe([("dir", name, None)]))
    try:
        R.check_tree(tree([("link", "ln", "d/t.txt"), ("link", "d/up", "../d")])); ok = True
    except R.JobError:
        ok = False
    check("check_tree accepts internal symlinks", ok)

    # ---- remote dependency detection
    def deps(files):
        root = tempfile.mkdtemp(prefix="deps-", dir=scratch)
        for rel, text in files.items():
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write(text)
        return R.find_remote_dependencies(root)
    v2 = json.dumps({"pins": [{"identity": "x", "kind": "remoteSourceControl", "location": "https://h/x"}], "version": 2})
    v1 = json.dumps({"object": {"pins": [{"package": "x", "repositoryURL": "https://github.com/a/x.git"}]}, "version": 1})
    loc = json.dumps({"pins": [{"identity": "x", "kind": "localSourceControl", "location": "/tmp/x"}], "version": 2})
    check("Package.resolved v2 remote pin detected", deps({"A.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved": v2})
          == ["A.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved"])
    check("Package.resolved v1 remote pin detected", deps({"Package.resolved": v1}) == ["Package.resolved"])
    check("Package.resolved unparsable -> detected (fail closed)", deps({"Package.resolved": "{nope"}) == ["Package.resolved"])
    check("Package.resolved with only local pins -> not detected", deps({"Package.resolved": loc}) == [])
    check("XCRemoteSwiftPackageReference in project.pbxproj detected",
          deps({"A.xcodeproj/project.pbxproj": "/* XCRemoteSwiftPackageReference section */"}) == ["A.xcodeproj/project.pbxproj"])
    check("XCLocalSwiftPackageReference only -> not detected",
          deps({"A.xcodeproj/project.pbxproj": "/* XCLocalSwiftPackageReference section */"}) == [])
    check("Package.swift .package(url:) detected",
          deps({"P/Package.swift": 'dependencies: [.package(url: "https://github.com/a/b", from: "1.0.0")]'}) == ["P/Package.swift"])
    check("Package.swift .package(name:, url:) detected",
          deps({"Package.swift": 'dependencies: [.package(name: "b", url: "https://x/b", .upToNextMajor(from: "1.0.0"))]'}) == ["Package.swift"])
    check("Package.swift with only .package(path:) -> not detected",
          deps({"Package.swift": 'dependencies: [.package(path: "../Local")]'}) == [])
    many = {"m%02d/Package.resolved" % i: v2 for i in range(30)}
    check("at most 20 files reported", len(deps(many)) == 20)
    linked = tempfile.mkdtemp(prefix="deps-", dir=scratch)
    open(os.path.join(scratch, "outside.resolved"), "w").write(v2)
    os.symlink(os.path.join(scratch, "outside.resolved"), os.path.join(linked, "Package.resolved"))
    try:
        R.check_tree(linked); ok = False
    except R.JobError as e:
        ok = e.status == "unsafe_tree"
    check("outside dependency symlink rejected by check_tree before scanning", ok)
    for name, payload in (("package.resolved", v2), ("project.PBXPROJ", "XCRemoteSwiftPackageReference"),
                          ("package.swift", '.package(url: "https://x/b", from: "1.0.0")')):
        check("case-folded dependency name reported as stored: " + name, deps({"App/" + name: payload}) == ["App/" + name])
    for name in ("Package.resolved", "project.pbxproj", "Package.swift"):
        root = tempfile.mkdtemp(prefix="deps-link-", dir=scratch)
        open(os.path.join(root, "payload.txt"), "w").write("remote dependency")
        os.symlink("payload.txt", os.path.join(root, name))
        R.check_tree(root)  # this link is allowed by the tree boundary
        check("in-tree dependency symlink refused: " + name, R.find_remote_dependencies(root) == [name])
    for kind in ("localSourceControl", "", "unknown", "RemoteSourceControl"):
        pin = json.dumps({"pins": [{"kind": kind, "location": "https://example.invalid/a.git"}]})
        check("remote location refused regardless of pin kind %r" % kind, deps({"Package.resolved": pin}) == ["Package.resolved"])
    for declaration in (
            '.package(name: "Foo(bar(baz))", url: "https://x/b", from: "1.0.0")',
            '.package(id: "owner.pkg", from: "1.0.0")',
            '.package(url: remoteURL, from: version)',
            '.package(path: computedPath)',
            '.package(path: "../Local", url: "https://x/b")',
            '.package /* ambiguous */ (url: "https://x/b", from: "1.0.0")'):
        check("nonliteral-local package declaration refused: " + declaration,
              deps({"Package.swift": declaration}) == ["Package.swift"])
    check("literal local paths with parentheses remain accepted",
          deps({"Package.swift": '.package(path: "../Local(foo(bar))")'}) == [])
    # Test the real 8 MiB boundary, including a valid marker entirely beyond the read cap.
    for name, marker in (("Package.resolved", v2), ("project.pbxproj", "XCRemoteSwiftPackageReference"),
                         ("Package.swift", '.package(url: "https://x/b", from: "1.0.0")')):
        check("oversize dependency file refused: " + name,
              deps({name: " " * (R.MAX_SCAN_FILE + 1) + marker}) == [name])
        # Opening as root would defeat a chmod fixture. Inject only the read error at this seam.
        root = tempfile.mkdtemp(prefix="deps-unreadable-", dir=scratch)
        path = os.path.join(root, name)
        open(path, "w").write(marker)
        real_open = open
        def unreadable(p, *a, **kw):
            if p == path:
                raise PermissionError("unit unreadable manifest")
            return real_open(p, *a, **kw)
        R.open = unreadable
        try:
            check("unreadable dependency file refused: " + name, R.find_remote_dependencies(root) == [name])
        finally:
            del R.open
    # Never block on a named pipe or read a special file as a manifest.
    root = tempfile.mkdtemp(prefix="deps-special-", dir=scratch)
    os.mkfifo(os.path.join(root, "Package.swift"))
    opened = []
    def no_special_open(p, *a, **kw):
        opened.append(p)
        raise PermissionError("unit refuses opening a FIFO")
    R.open = no_special_open
    try:
        check("special dependency file refused without open",
              R.find_remote_dependencies(root) == ["Package.swift"] and not opened)
    finally:
        del R.open


    # ---- retention never deletes the in-flight job, keeps the newest 19 older ones
    ents = [("j%02d" % i, 1000 + i) for i in range(25)]
    v = D.retention_victims(ents, "zz-current")
    check("retention keeps the 19 newest of 25 older dirs", sorted(v) == ["j%02d" % i for i in range(6)])
    v = D.retention_victims(ents + [("cur", 1)], "cur")
    check("retention never selects the in-flight job, even when it is the oldest", "cur" not in v and len(v) == 6)
    check("retention with fewer than 20 dirs removes nothing", D.retention_victims(ents[:19], "x") == [])
finally:
    shutil.rmtree(scratch, ignore_errors=True)

print("# unit pass=%d fail=%d" % (sum(RESULTS), len(RESULTS) - sum(RESULTS)))
sys.exit(0 if all(RESULTS) else 1)
