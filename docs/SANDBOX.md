# Borrowed Hands build sandbox

Status: verified on one build Mac (Apple silicon, macOS 27.2, Xcode 27.2). After three security review fix
rounds, `tests/run-tests.sh` passed 112/112 there and `tests/mutation-check.sh` killed 18/18 mutants (output
below). The later integration work (`bh/*` ancestry, remote-dependency refusal, cancellation, retention) was
verified the same way on that Mac: the full suite passed 165/165 (nested units 121/121), dependency variants
13/13, 35/35 mutants were killed, and 18/18 production-install gate checks passed with TEST_MODE absent, test
profiles removed and installed files matching the source hashes. Results on other Macs and Xcode versions
are not established.

## Why builds never start from the owner's session

The first prototype ran builds with `sudo -u bh-builder ...` from the owner's login session. That setup had
two serious problems:

- **launchd namespace inheritance.** A process started with `sudo -u bh-builder` keeps the caller's launchd
  bootstrap namespace. As bh-builder, `launchctl managername` printed `Aqua` and `manageruid` printed the
  owner's uid.
- **uid escape.** As bh-builder, `launchctl submit -l com.bh.nsprobe -o /tmp/bh-nsprobe.out -- /bin/sh -c id`
  ran as the owner's uid, with the owner's groups (including admin). Any build could run code as the owner.
  Separate users give no protection while builds start that way.
- **Damage.** The first runner's launchd cleanup (`launchctl list` / `launchctl remove` for every label not
  starting with `com.apple.`, run as bh-builder) acted on the owner's GUI domain and removed its
  `application.*` jobs, closing the owner's GUI apps. That code was deleted. The runner now never calls
  launchctl. Nothing in this repo runs `launchctl list`, `remove` or `bootout` against a gui/ or user/
  domain. Nothing sends a signal to pid -1 or to a process group.

**Fix (implemented and tested):** builds start only as a LaunchDaemon in the **system** domain:
- label `com.borrowedhands.runner`;
- `UserName`/`GroupName` bh-builder, `InitGroups` false, `SessionCreate` true;
- no KeepAlive, `LaunchOnlyOnce`;
- bootstrapped per job by a root-owned dispatcher.

Inside a build, `launchctl managername` is `System` and `manageruid` is `0` (evidence below). Under every
profile, `launchctl submit` / `bootstrap` from the build fails, and nothing it submitted ever ran.

## Fix round 1 (security review)

Every fix was written test-first and has a test that fails without it. `tests/mutation-check.sh`
deliberately breaks the HIGH 1 and MEDIUM 4 fixes; all 8 mutants were killed.

| # | Fix | Test |
|---|---|---|
| HIGH 1 | bh-dispatch always uses `builder.sb` and https remotes. `--profile` / `--allow-file-remote` are honoured only while the root-owned `/opt/borrowed-hands/TEST_MODE` exists; otherwise the dispatcher returns `usage` before reading stdin or writing a plist. A normal setup deletes `libexec/profiles/*.sb` and `TEST_MODE`; only `--with-test-profiles` installs them. | run-tests: "prod setup: profiles/ is empty", "no TEST_MODE flag", "prod mode refuses ..." (allow-all.sb, ablate-0.sb, builder.sb, --allow-file-remote), "no job dir and no plist written". Mutants: no gate, setup keeping profiles. |
| MEDIUM 2 | `git.sb` rewritten from `(deny default)`. Exec only of Xcode's git and its helpers plus `/bin/sh`; reads limited to system libraries, Xcode, `/private/etc/ssl` and the job; writes to the job and per-user temp/cache; network to mDNSResponder plus TCP `*:443`; mach limited to libinfo, DNSConfiguration, notify, trustd, dirhelper, logd. | `scripts/profile-probe.sh git.sb` runs each row against a positive control: /etc/hosts, /etc/passwd, wifi and network plists, System.keychain, the /Users/Shared bait, listing /Users, writing /tmp, TCP 80/22, UDP 53 and exec of perl/osascript are all DENIED, and TCP 443 still works. A real https fetch of `octocat/Hello-World@7fd1a60` runs under git.sb. A hostile fixture (undefined filter/diff driver, a submodule gitlink, a symlink out of the tree) materializes exactly and builds. |
| MEDIUM 3 | Remote host must be a DNS name: any purely numeric or `0x` label is rejected. Every address it resolves to must be global; loopback, RFC1918, link-local, CGNAT 100.64/10, ULA, multicast, unspecified and reserved are rejected. A resolution failure is also a rejection. `http.followRedirects=false` kept. | 127.1, 0x7f.0.0.1, 10.1, 169.254.169.254, 100.64.0.1, 192.168.1.1, 1.1.1.1, [::1], [fd00::1], localtest.me (resolves to 127.0.0.1) and 127.0.0.1.nip.io each give invalid_job with no job dir. |
| MEDIUM 4 | A failed pre-sweep aborts before bootstrap. A failed post-sweep returns `error` and ignores result.json. read_result opens with O_NOFOLLOW (and O_NONBLOCK), then fstat requires a regular file owned by uid 470 with nlink 1 and mode 0600. runner.err is pre-created by the uid-470 child (O_EXCL, 0600) and checked by the dispatcher before bootstrap. | `tests/unit_dispatch.py` covers: good file; mode 0644; root-owned; hardlink to a root-owned JSON file created in a scratch dir (deleted afterwards); nlink 2 between two uid-470 files; symlink to an otherwise valid file; FIFO; 5000-deep JSON; pre-sweep failure → no job dir and no bootstrap; post-sweep failure → result.json ignored. Mutants: no nlink, no mode, no uid, following symlinks, no pre-abort, no post-abort. |
| MEDIUM 5 | Polled budgets in bh-runner every 3 s. Disk is `du -sxk` of the job dir plus per-user T and C (default 20 GB, `--disk-budget-mb`, max 100 GB). Memory is the summed RSS of all uid-470 processes (default 6 GB, `--mem-budget-mb`, max 64 GB). Over budget gives status `disk_budget` / `memory_budget` and the normal sweep. | `tests/hog-app`: dd of 3 GB against a 500 MB budget gives `disk_budget` ("job used 3100 MB"), the volume is still writable, and no processes are left. A perl allocation of 1.5 GB against a 700 MB budget gives `memory_budget` ("2406 MB RSS"). |
| LOW 6 | urlsplit/.port/username wrapped (any exception becomes invalid_job). JSON parse catches every exception, including RecursionError, in both runner and dispatcher. | Port 99999, port -1, `https://[::1/`, and 2000-deep JSON each give invalid_job (integration and unit). |
| LOW 7 | Per-user T/C dirs: after realpath they must match `^/private/var/folders/[A-Za-z0-9_+./-]+$`, contain no `..`, and be a real directory owned by 470. | unit: rejects a path with `"`, `/tmp/T`, and `/private/var/folders/../../etc`. |
| LOW 8 | `/private/etc/passwd` and `/private/etc/group` removed from builder.sb's read allowlist. Builds still work. | probe `read_etc_passwd` PASS (control ok); profile-probe row. |
| LOW 9 | `(deny ipc-posix-shm* ipc-posix-sem* ipc-sysv*)`. The sandbox does not mediate SysV *creation* (shmget/semget succeed), only use (shmat/semop are denied). SysV objects outlive their creator, so bh-runner's teardown now removes every SysV object owned by bh-builder (`ipcs` / `ipcrm`, as uid 470). | Probes `ipc_posix_shm`, `ipc_posix_sem`, `ipc_sysv_shm_attach`, `ipc_sysv_sem_op` PASS (a C helper compiled at build time; the SysV ones use a fixed key and leave the object behind). `post:sysv_ipc_left_behind` PASS. The result JSON lists `sysv_ipc_removed`. |
| LOW 10 | The runner's signal handler only sets a flag; the run loop raises and teardown kills. | The existing cancel test (SIGTERM gives `cancelled`, no leftovers, sentinel alive) passes. It would also pass without the change, so this one has no test that fails without it. |
| LOW 11 | Teardown kills via `bh-dispatch --sweep-only`, the same uid-470 child as the dispatcher; there is no `sudo -u ... pkill` any more. Setup refuses a symlinked `/Users/Shared`, home or jobs dir, and uses `chown -h` / `chmod -h`. | Not run, because both are destructive: teardown would remove the user, and the symlink test needs a planted symlink at the home path. Code review only. |
| LOW 12 | Setup removes every *direct* group membership (GroupMembership) and asserts `id -G` holds nothing beyond 470 and the implicit groups. | `id -G` can't be 470-only without editing shared system groups. macOS makes everyone(12) and localaccounts(61) implicit for every local user, and `_lpoperator` / `com.apple.sharepoint.group.*` nest them (NestedGroups), so every local account shows them. getgroups() in the build process returns the same list despite InitGroups=false, because macOS resolves group membership dynamically. The test asserts the build's kernel groups contain no staff(20), admin(80) or any other group (`info_kernel_groups=470,12,61,703,704,100,701,702`). |
| test quality | The probe suite runs a positive control first (the same hostile build under allow-all.sb). A row is PASS only if the control shows the escape working; DENIED with a DENIED control, or a target absent on this machine (`ABSENT`), is INCONCLUSIVE. | See the table below. |

## Fix round 2 (scoped re-review)

| # | Fix | Test (fails without the fix; mutants in `tests/mutation-check.sh`) |
|---|---|---|
| HIGH a | **The root dispatcher enforces budgets**, not only the runner, which runs as the same uid as the build. Every 1 s bh-dispatch checks: `os.statvfs` of the jobs volume (free-space drop since job start against the disk budget, plus an absolute 10 GB free floor); summed RSS of uid 470 against the memory budget (`ps` as root); and the wall-clock deadline (runner timeout + 30 s). On a breach it boots the job out and sweeps; the status is `disk_budget` / `memory_budget` / `timeout`. statvfs sees unlinked-but-open files that a directory walk can't. | `run-tests.sh budget`: an unlinked, still-open 3 GB file against a 1000 MB budget gives `disk_budget` "dispatcher (statvfs): volume free space fell by ..."; the volume stays writable and no processes are left. Mutant: dispatcher without the statvfs check. |
| HIGH b | **The runner's budget checks fail closed.** `du` is replaced by an `os.walk`/`lstat` sum. Files that vanish mid-walk are skipped. EPERM is skipped too: macOS-protected Apple dirs such as `T/com.apple.trustd/TemporaryItems` return it, and the dispatcher's statvfs still counts their bytes. Anything else (EACCES, more than 3M entries) raises. `ps` non-zero or unparsable raises. Any failure gives status `budget_check_failed`. | unit: disk measurement raising, ps rc=1 and unparsable ps output each give `budget_check_failed`. Mutant: runner budget checks failing open. |
| HIGH c | **builder.sb `(deny signal) (allow signal (target same-sandbox))`** (syntax verified on macOS 27.2). The build can signal only processes inside its sandbox, not the runner, its `ps`, or launchd's agents. The runner writes its pid to `<job>/tmp/.bh-runner-pid` on purpose, so the test isn't vacuous. | `tests/hog-app` "stopper": a Run Script reads the runner pid, then tries `kill -STOP`, `pkill -STOP -f`, `kill -KILL`; all three are DENIED, and the unlinked-file hog that follows is still stopped by the dispatcher (`disk_budget`). Positive control: under allow-all.sb the same script stops and kills the runner, and the dispatcher still ends the job and sweeps. Probe `signal_runner` uses the same pid. Mutant: builder.sb without `(deny signal)`. |
| MEDIUM | **setup claims the home exclusively** (`claim_dir`). An existing path is adopted only if it is a real dir owned by 470 with mode 0700, and only if empty unless the user already existed (our own earlier install). Otherwise setup runs a plain `mkdir -m 700` (no `-p` on the final component), `chown -h`, and re-checks. Symlinks are refused. `setup --check-home <path>` runs just this logic, for tests. | Scratch paths under /Users/Shared: a dir owned by 501 with a file, an empty dir owned by 501, and a symlink are each refused (and left untouched); a missing path is created as 470:700. Mutant: setup adopting a home it doesn't own. |
| MEDIUM | **DNS pin.** The runner resolves the remote once (all addresses must be public), then runs git with `-c http.curloptResolve=<host>:443:<addr>` (git 2.54.0 supports it). Rebinding between validation and fetch no longer changes where git connects. | unit: git argv contains `http.curloptResolve=github.com:443:140.82.112.3` from a patched resolver. Live: git pinned to 192.0.2.1 gets no answer from github.com; the real https fetch under git.sb still works through the pin. Mutant: runner without the pin. |
| LOW | 6to4 `2002::/16`, Teredo `2001::/32` and NAT64 `64:ff9b::/96`, `64:ff9b:1::/48` are rejected outright. IPv4-mapped addresses are unwrapped and checked. | unit (python3 3.9): 2002:7f00:1::1, 2001:0:4136:e378::1, 64:ff9b::7f00:1, ::ffff:10.0.0.1, 100.64.1.1 and fd12::1 rejected; public v4+v6 accepted. |
| LOW | **runner.err** is created by root with `openat(job_dir_fd, O_CREAT|O_EXCL|O_NOFOLLOW)`, then `fchown` 470, `fchmod` 0600 and `fstat`-verified on the fd (regular, uid 470, nlink 1, 0600). Only then does its path go into the plist. | Every dispatch path (all build tests). |
| LOW | **teardown** retries `bh-dispatch --sweep-only` 3 times and refuses to remove the user if the sweep still fails. | Not run (destructive). |
| LOW | `results()` catches any exception (including RecursionError) from xcresult JSON; non-dict results are ignored. `user_dir` requires mode 0700. | user_dir unit test now passes only on `JobError` (no blanket OSError). |
| LOW | **git.sb exec narrowed** to `usr/bin/git`, `git-core/git`, `git-core/git-remote-https`, `git-core/git-remote-http` (the symlink target). `/bin/sh` and `/bin/bash` (sh re-execs bash) plus `git-upload-pack`, and the `/private/tmp/bh-stage-*` read, apply only when `BH_TEST_FILE_REMOTE=1`, an SBPL `(if (equal? (param ...) "1") ...)` the runner sets only for file:// remotes (test mode). `notification_center`, `dirhelper` and the notify shm read were dropped: fetch + worktree + checkout over https still work (denials logged, no failure). `logd` kept as a write-only log sink. | git.sb profile probe; real https fetch; all file:// test builds. |
| LOW | Wrong InitGroups comment in setup fixed. msgget/msgsnd probe added (`ipc_sysv_msg_send`); teardown's SysV cleanup already covers queues. | Probe row PASS. |

### Ship decision: DNS rebinding (recorded 2026-11-05)

The remote host is checked when the job is validated and again when the runner resolves it before the
fetch. The fetch is then pinned to that checked address with `http.curloptResolve`. What's left:
- the pinned address is whatever DNS returned at that moment, so a hostile DNS server can still choose
  *which public* address git talks to;
- git.sb can't filter by IP; SBPL only matches `*:443`.

The real authority is the signed build authorization (repo fingerprint, commit, refs/heads/bh/* on the
configured remote), enforced by the helper. The runner fetches by full commit sha and checks that the
materialized tree matches that commit. A different server can
at worst fail the fetch or serve a commit with a different sha, which fails the tree check. Decision: ship
with the pin. A malicious public endpoint is accepted as equivalent to a hostile remote, which the sandbox
already assumes.

## Fix round 3 (pre-merge security review)

| # | Fix | Test (mutant in `tests/mutation-check.sh` where marked †) |
|---|---|---|
| MEDIUM 1 | **Process and file limits.** The plist sets `NumberOfProcesses` 256 and `NumberOfFiles` 1024 in both Soft and Hard limits. The effective process cap is min(256, `kern.maxprocperuid`). The dispatcher's teardown (bootout, sweep, leftover check, label check, plist removal) runs every step through `retry()`, which tolerates OSError/SubprocessError (e.g. fork EAGAIN) until a 120 s hard deadline. The wait loop tolerates transient `launchctl print` failures; three measurement failures in a row fail the job closed. A top-level handler guarantees the dispatcher always prints exactly one JSON line. | `tests/hog-app` forkbomb (fork toward 2000): peak 256 uid-470 processes; one JSON line; no leftovers; label gone; sentinel alive; the owner can still fork. fds (3 × 10k opens): each process stopped at 1021, then the same checks pass. unit: teardown with bootout and sweep each raising EAGAIN three times completes every step. † "teardown without the EAGAIN retry guard", killed by that unit test. The forkbomb test can't kill it: at the 256 cap, root's forks never fail. |
| MEDIUM 2 | **Undeletable files.** builder.sb has `(deny file-write-flags file-write-acl)`, so no `chflags uchg` and no ACLs. After `read_result`, the dispatcher deletes `src dd tmp home git result.xcresult` in a uid-470 child: it clears flags, sets dirs to 0700 top-down (including a 0000 top dir), then runs `rmtree`. It keeps `logs/`, `job.json`, `result.json` and `runner.err`. The runner's per-user wipe does the same and reports `wipe_ok`. Teardown clears flags and modes, checks `rm` status, and no longer prints "removed" after a failure. | hog "immutable" (600 MB, `chflags uchg` on the file and its dir, `chmod 000` three levels deep): both chflags DENIED; afterwards the job dir holds only `job.json logs result.json runner.err`; `rm -rf` of it succeeds; free space back to the starting value (315886 → 315886 MB). profile-probe row `chflags_uchg` DENIED with the control ok. † no flags deny (killed by the profile probe); † dispatcher not deleting build dirs. |
| LOW 3 | builder.sb denies `iokit-get-properties` for `IOPlatformSerialNumber`, `IOPlatformUUID` and `IOMACAddress`, and `sysctl-read` of the `net.` prefix. The hostname is still readable. The sample app, hostile app and fixtures still build. | Probe rows `ioreg_serial` and `netstat_peers` PASS: the control shows the serial and LISTEN sockets, builder.sb shows neither. |
| LOW 4 | Tests that fail if the rules break. | † dispatcher memory budget (message must start `dispatcher:`). † dispatcher deadline (allow-all control in which the build SIGSTOPs the runner: `timeout` after 66 s for `--timeout 30`). `iokit_open` probe row (IOServiceOpen on IOSurfaceRoot, which works in the control). Negative unit tests: validate_job_dir (7 cases); create_runner_err (existing file, planted symlink, dir not owned by 470, symlinked dir). Hard-link and `cp -c` (clonefile) of a root-owned, world-readable canary (`/opt/borrowed-hands-test/canary.txt`, created and removed by the tests, never a system file) into the job's tmp: the control reads it (the kernel allows linking a file you don't own); builder.sb denies both. The control's link and clone live in `<job>/tmp`, which the dispatcher deletes. |
| LOW 5 | Numeric args must match `[0-9]{1,7}` (`"5²".isdigit()` is True in Python). Budgets are parsed and range-checked before stdin is read or anything is created. | `--timeout 5²` gives usage; `--disk-budget-mb 99999999` gives usage; no job dir. |
| LOW 6 | Docs: sudoers pin uses `#501` with one line; `EXTRA_WAIT` is 30 s; `load_runner` checks `root_owned_chain(RUNNER)` before executing it. | Covered by the existing "dispatch refuses ..." checks. |
| LOW 7 | `_non_agent` uses `proc_pidpath` plus `proc_pidinfo(PROC_PIDTBSDINFO)` (ppid, zombie status) through ctypes. There's no `ps` fork, so it works while uid 470 is at its process limit. Anything it can't inspect counts as a leftover (fails closed). | Every "no bh-builder processes"/`leftover_pids` check, including after the forkbomb. |

## Integration: refs, dependencies, cancellation, retention

What the sandbox provides to the helper and the Xcode capability:

| Item | Behavior | Test (mutant in `tests/mutation-check.sh` where marked †) |
|---|---|---|
| `allowed_refs` | job.json has a required key `"allowed_refs": "refs/heads/bh/*"` (exact value), checked by the shared validator in both bh-dispatch and bh-runner. Anything else is `invalid_job`. | `refs/heads/*`, `refs/heads/bh/x` and a missing key each give invalid_job with no job dir; unit tests cover 6 bad values and the good one. † validator accepting any allowed_refs. |
| bh/* ancestry | Ported from `helper/borrowed_hands_helper/gitmat.py`. Under git.sb, with the DNS pin and the hardened `-c` recipe, the runner: runs `init --bare --template=`; adds remote `origin` (the job's remote); deletes stale `refs/remotes/origin/bh/*`; runs `fetch --filter=blob:none origin +refs/heads/bh/*:refs/remotes/origin/bh/*`; fetches the commit alone (same filter) only if it isn't in that history; then requires `merge-base --is-ancestor <commit> <ref>` for some fetched ref. The checkout pulls blobs lazily from the same pinned origin. | A commit on `bh/test` builds. A commit only on main gives `not_reachable` and no build runs. A nonexistent sha gives `fetch_failed`. A real https fetch of `octocat/Hello-World` (no bh/ branches) gets through both fetches and stops at `not_reachable`. † runner building commits not reachable from bh/*. |
| tree safety | `check_tree` (from gitmat): after checkout, any symlink resolving outside the checkout or into `.git`, or any entry that is `.git` after NFKC, mark/format/whitespace stripping and casefolding (other than the worktree's own `.git` file), gives status `unsafe_tree`. lstat/readlink/realpath only. | Integration: an escaping symlink and a `.git<ZWSP>` entry each give `unsafe_tree`. Unit: gitmat's escaping targets, a chain escape, a nested escape, three lookalike entries, internal symlinks accepted, and the dotgit predicate lists. † check_tree accepting escaping symlinks; † runner not calling check_tree. |
| remote dependencies | Bounded preflight after `check_tree`, before xcodebuild. Candidate basenames are case-folded; reported paths retain checkout spelling. A remote pin location is refused regardless of its kind. All three recognized files (`Package.resolved`, `project.pbxproj`, `Package.swift`) fail closed if symlinked, nonregular, unreadable, invalid UTF-8 or over 8 MiB. No link is followed. Remote pbxproj references and visible `.package` declarations other than a literal `.package(path: "...")` are refused, including registry, computed arguments and nested parentheses in strings. A hit gives `unsupported_dependencies`, up to 20 sorted relative `files`, and local-path vendoring guidance. | Root units and dispatcher variants cover case, in-tree links, nested-name URL, registry, ambiguous calls, inconsistent remote pins and caps; the existing local-path app still builds. Independent scanner mutants cover each new guard. This is a conservative syntax check, not arbitrary Swift evaluation; dynamic/aliased declarations are not proven absent, and comments may cause refusals. `builder.sb` remains the network/owner-file boundary. |
| local-path packages | Building one needed three runner/profile changes, each found by bisecting on the build Mac. First, `bsd.dirhelper` is now the one allowed mach service: without it, confstr can't return the per-user temp dir, SwiftPM falls back to `/private/tmp`, and resolution fails with `permissionDenied`. Second, `-IDEPackageSupportDisableManifestSandbox=YES`: SwiftPM's own manifest sandbox would be a nested `sandbox_init`, which builder.sb's sandbox doesn't allow; builder.sb still confines everything. Third, `-clonedSourcePackagesDirPath` / `-packageCachePath` inside `dd/`. | local-pkg fixture builds; the probe suite still passes. |
| cancel from the owner's uid | **Proven.** uid 501 sends SIGTERM to its own `sudo -n bh-dispatch` child. The sudo process's real uid is the caller's, so the signal is permitted, and sudo relays it to the dispatcher. This works with the child in the caller's process group and in its own session. Result: `status: cancelled` about 0.6 s after the signal, sent mid-build (the hang app's Run Script running), no uid-470 leftovers, label gone, sentinel alive. For a helper that lost its child, argv-free `bh-dispatch --cancel` (job id on stdin) signals the in-flight dispatcher for exactly that job (gap 8). | `tests/cancel_owner.py` modes `sigterm-sudo`, `sigterm-sudo-sess`, `cancel-cmd`, `cancel-wrong-id` (answers `not_running`, build untouched); `--cancel` with nothing running gives `not_running`; malformed id gives `usage`. † `--cancel` signalling a dispatcher running a different job. |
| exact production argv | Without TEST_MODE the dispatcher accepts only the three argv shapes in gap 8, matching the helper's pinned sudoers lines. | Prod mode refuses 9 near-miss argv (bare `--timeout 300`, tier 301, reordered, leading zero, repeated flag, budget out of range, `--sweep-only --cancel`, `--cancel x`, none) and accepts the 4 tier forms and `--sweep-only`. † any `--timeout` value accepted; † no exact-argv check. |
| retention | Before each job, the dispatcher deletes the oldest job dirs (by mtime, uuid-named, owned by 470, lstat only) as uid 470 so that at most 20 remain including the new job. It never selects the in-flight job (only one can exist: the lock). The count removed is in `dispatch.retention_removed`. | 25 pre-made dirs: after one job, 20 remain, the 6 oldest are gone, the 19 newest and the new job's dir are kept. Unit: never the in-flight job, even when it is the oldest; fewer than 20 removes nothing. † dispatcher without retention; † retention allowed to select the in-flight job. |
| result shape | Every build result (`succeeded`/`failed`) has `errors`, `warnings` (at most 50, same shape as errors), `error_count` and `warning_count`, including when xcresulttool produced nothing (`[]`, `[]`, `0`, `0`). | A succeeded result is exactly `errors: []`, `warnings: []`, `error_count: 0`, `warning_count: 0`. |

**Final status list** (one JSON line from bh-dispatch; SPEC 9.1 maps them):
`succeeded | failed | timeout | cancelled | fetch_failed | not_reachable | unsupported_dependencies | unsafe_tree |
invalid_job | disk_budget | memory_budget | budget_check_failed | busy | usage | error`.
`unsafe_tree` maps to failed / `invalid_arguments` with `detail.reason: "unsafe_tree"` (SPEC 9.1,
PROTOCOL error enum). The helper applies this mapping before dispatch results reach the wire.
`unsupported_dependencies` also carries `files`.

## Swift macros (2026-11-09)

**Problem.** In the Xcode 27 SDK, `@State`, `#Preview` and `@Observable` are Swift macros, so almost
every SwiftUI app needs macro plugins to compile. swift-frontend runs each plugin
(`swift-plugin-server` for the SDK's `SwiftUIMacros`, `PreviewsMacros` and `ObservationMacros`)
through its own `sandbox-exec`. A process already inside builder.sb can't apply a second sandbox,
so every such build failed:

```
sandbox-exec: sandbox_apply: Operation not permitted
error: external macro implementation type 'SwiftUIMacros.StateMacro' could not be found for macro 'State()';
  '.../MacOSX.platform/Developer/usr/bin/swift-plugin-server' produced malformed response
```

Reproduced on macOS 27.2 / Xcode 27.2 (27B52) with the runner's own argv, sandbox params and clean
environment under `sandbox-exec -f builder.sb`, as the owner's uid: an `@State`, a `#Preview` and an
`@Observable` app each fail on both destinations; the same argv without sandbox-exec builds.

**Fix.** The runner's fixed argv adds `SWIFTC_DISABLE_SANDBOX=YES`, the same approach as
`-IDEPackageSupportDisableManifestSandbox=YES` for SwiftPM manifests. It is Xcode's own build setting
(declared in Swift.xcspec) for `swiftc -disable-sandbox`, which stops the compiler wrapping plugins in its
own sandbox. It does not touch `OTHER_SWIFT_FLAGS`, so a project's own Swift flags still apply.
Alternatives checked:
- `'OTHER_SWIFT_FLAGS=$(inherited) -disable-sandbox'` also builds, and the command-line `$(inherited)` keeps
  target-level flags. It reaches the same three Swift compile steps, but it is a string the runner would
  have to keep merging with the project's flags.
- Allowing the nested sandbox in builder.sb is not possible: inside builder.sb even
  `sandbox-exec -p '(version 1)(allow default)' /usr/bin/true` fails with `sandbox_apply: Operation not
  permitted` (exit 71). No SBPL rule permits it.
- No plugin-only setting exists. `-disable-sandbox` is the only sandbox option in
  `swift-frontend -help-hidden`, and `SWIFTC_DISABLE_SANDBOX` the only one in Xcode's Swift specs.

**The project can't undo it.** Command-line build settings take precedence over project and target
settings, the project's base xcconfig files and SDK conditionals (`[sdk=...]`). Two mechanisms rank higher:
`xcodebuild -xcconfig` and an `XCODE_XCCONFIG_FILE` environment variable. The runner exposes neither: its
argv has no `-xcconfig`, and `clean_env` builds the environment from scratch, so an incoming
`XCODE_XCCONFIG_FILE` never reaches xcodebuild. `tests/macro-app` sets `SWIFTC_DISABLE_SANDBOX = NO` at
project level, at target level and under `[sdk=macosx*]` / `[sdk=iphonesimulator*]`. Its Debug
configuration sets `OTHER_SWIFT_FLAGS = -DBH_PROJECT_FLAG` without `$(inherited)`, and Release sets it to
`""`. Every Swift compile line still carries `-disable-sandbox`, and Debug still carries the project's flag
(an `#error` checks it). There is no `-enable-sandbox` flag. If a project could turn the plugin sandbox
back on, its own macro build would fail; nothing would get out of builder.sb. The same holds for the
higher-precedence mechanisms above if they were ever exposed. This is a build-correctness property, not a
security boundary: a project can already run arbitrary code (Run Script phases, custom compilers) under
builder.sb, and builder.sb is the boundary. The fixture covers project-level, target-level and conditional
settings; a project base xcconfig file is not separately tested.

**Plugins stay inside builder.sb.** A sandbox applies to the process and every descendant and can't be
removed, so plugins (children of swift-frontend, under xcodebuild, under sandbox-exec) inherit it.
Checked two ways:
- While `tests/macro-app` builds, `sandbox_check(pid, NULL, 0)` on the build's `swift-plugin-server`
  (parent swift-frontend) returns 1 (sandboxed), and `network-outbound` is denied.
- `tests/macro-probe-app` loads its own executable plugin (`-load-plugin-executable`, a shell script) that
  tries to read a canary outside the job, write outside the job, list the owner's home, and connect to a
  127.0.0.1 listener. As a positive control, with the same argv but no sandbox-exec, the plugin runs under
  swift-frontend and all four work. Under builder.sb all four are DENIED, and the plugin can still write
  to its checkout.

**Residual risk.** Before this change the compiler's own plugin sandbox was stricter than builder.sb: deny
default on top of Apple's `system.sb`, with only `.dylib` reads, metadata and exec added (no job writes,
no network). Now SDK macro plugins and any local-package
macro plugin run under builder.sb alone. They can read builder.sb's read allowlist and write the job's
build dirs and the per-user temp/cache dirs. That is the same access a project's Run Script phase or
(since the integration round) local package manifest already has, so a hostile project gains no new capability. (Build tool
plugins would run under builder.sb too, but they are untested; see gap 6.) Still closed: network, mach
services other than `bsd.dirhelper`, other users' files, and the contents and directory listings of the
owner's home. File metadata (stat/lstat/readlink) stays readable profile-wide, including in the owner's
home, as for every build process (gap 1).
A local package that defines its own macros needs a vendored swift-syntax (remote packages are refused);
that case is still untested.

**Tests.** `helper/tests/test_runner_macros.py`: the argv carries `SWIFTC_DISABLE_SANDBOX=YES` exactly once,
as a build setting before `build`, with no other sandbox flag and no `OTHER_SWIFT_FLAGS` (both
destinations). The opt-in `-m xcode` tests build `tests/macro-app` under builder.sb for
macOS Debug, iOS Simulator Debug and macOS Release. A control build without the setting fails with the
three macro errors above, and the probe app checks plugin confinement.
`tests/mutate-swift-macro-sandbox.sh` builds four argv mutants (setting removed, moved after `build`,
replaced with the `OTHER_SWIFT_FLAGS` form, followed by `=NO`); the unit tests kill all four. The macro-free demo app (both destinations), `tests/simulator-demo` and
`tests/deps-apps/local-pkg` still build with the new argv. Local package targets also get
`-disable-sandbox`. Not yet run through the installed dispatcher as bh-builder (uid 470).

## Architecture

```
helper (owner, uid 501)
  └─ sudo -n /opt/borrowed-hands/bin/bh-dispatch [--timeout S] < job.json     (root)
       validate job.json (same validator as bh-runner) · flock · sweep uid 470 (as uid 470)
       mkdir job dir as uid 470, write job.json
       write /opt/borrowed-hands/run/com.borrowedhands.runner.plist
       launchctl bootstrap system <plist>        ── only label it ever touches
         └─ bh-runner <job-dir>                 (launchd job: bh-builder, own session, system domain)
              git materialize (hardened recipe, under sandbox/git.sb, https only)
              sandbox-exec -f builder.sb xcodebuild <fixed argv>
              sandbox-exec -f builder.sb xcresulttool get build-results
              teardown: kill every other uid-470 pid · wipe per-user T/C · write result.json
       wait for exit / deadline / SIGTERM → launchctl bootout system/com.borrowedhands.runner
       sweep uid 470 (as uid 470) · read result.json (O_NOFOLLOW, owner check) · print one JSON line
```

Files:

| Path (repo) | Installed as | What |
|---|---|---|
| `sandbox/bh-dispatch` | `/opt/borrowed-hands/bin/bh-dispatch` (root 755) | root dispatcher, the job-submission path |
| `sandbox/bh-runner` | `/opt/borrowed-hands/libexec/bh-runner` (root 755) | per-job runner (python3 stdlib) |
| `sandbox/builder.sb` | `/opt/borrowed-hands/libexec/builder.sb` | build sandbox profile |
| `sandbox/git.sb` | `/opt/borrowed-hands/libexec/git.sb` | profile for the git materialization |
| (generated per job) | `/opt/borrowed-hands/run/com.borrowedhands.runner.plist` | the LaunchDaemon plist |
| `sandbox/setup-bh-builder.sh` | | idempotent: user, group, home, install (root) |
| `sandbox/teardown-bh-builder.sh` | | removes all of the above |
| `scripts/probe-suite.sh` | | hostile probe suite, PASS/FAIL table |
| `scripts/sandbox-violations.sh` | | builds an app and lists the sandbox denials (discovery) |
| `tests/run-tests.sh` | | end-to-end tests |
| `tests/hostile-app/` | | Run Script + CC-wrapper probes (`probes/probe.sh`) |
| `tests/hang-app/` | | hanging Run Script + detached daemon (timeout/cancel tests) |
| `tests/profiles/{allow-all,medium}.sb` | `.../libexec/profiles/` with `--with-test-profiles` | baselines |

The LaunchDaemon plist that bh-dispatch writes for each job looks like this (paths shortened):

```
Label com.borrowedhands.runner
ProgramArguments /usr/bin/python3 -I /opt/borrowed-hands/libexec/bh-runner --profile <libexec>/builder.sb --timeout N <job-dir>
UserName bh-builder · GroupName bh-builder · InitGroups false · SessionCreate true
RunAtLoad true · KeepAlive false · LaunchOnlyOnce true · AbandonProcessGroup false
WorkingDirectory <job-dir> · StandardOutPath /dev/null · StandardErrorPath <job-dir>/runner.err
Soft/HardResourceLimits NumberOfProcesses 256, NumberOfFiles 1024
```

### job.json, and what is validated (by both bh-dispatch and bh-runner)

```json
{"v":1,"job_id":"<uuid4>","remote":"https://host/path.git","commit":"<40 hex>",
 "project":"App.xcodeproj","scheme":"App","configuration":"Debug","destination":"platform=macOS,arch=arm64",
 "allowed_refs":"refs/heads/bh/*"}
```

- **Keys:** exactly these keys.
- **job_id:** a lowercase uuid4. It is also the job dir name, `/Users/Shared/.bh-builder-home/jobs/<job_id>`.
  bh-runner checks the job dir is canonical, has no symlinks, and is owned by 470 with mode 0700 (the jobs
  root too).
- **remote:** https only, with no userinfo, port, query or fragment. A `file://` remote is accepted only with
  `--allow-file-remote` (tests).
- **commit:** 40 hex characters.
- **project:** a relative `.xcodeproj` / `.xcworkspace` path with no `..`. After checkout it must be a real
  directory, not a symlink.
- **scheme / configuration:** `[A-Za-z0-9_][A-Za-z0-9 ._+-]{0,63}`.
- **destination:** must equal `platform=macOS,arch=arm64`.
- **allowed_refs:** must equal `refs/heads/bh/*`.

xcodebuild argv is fixed:

```
xcodebuild [-sdk iphonesimulator] -project|-workspace <src/...> -scheme S -configuration C -destination D
  -derivedDataPath <job>/dd -resultBundlePath <job>/result.xcresult -disableAutomaticPackageResolution
  -hideShellScriptEnvironment -IDEPackageSupportDisableManifestSandbox=YES
  -clonedSourcePackagesDirPath <job>/dd/SourcePackages -packageCachePath <job>/dd/PackageCache
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY= SWIFTC_DISABLE_SANDBOX=YES build
```

`-sdk iphonesimulator` is added only for `generic/platform=iOS Simulator`. The two nested-sandbox switches
are explained in the integration table (manifests) and "Swift macros (2026-11-09)".

The environment is `env -i` style: `HOME=<job>/home`, `TMPDIR=<job>/tmp/`, a fixed `PATH`, `DEVELOPER_DIR`.

### Result (one JSON line on stdout of bh-dispatch)

`status` is one of `succeeded | failed | timeout | cancelled | fetch_failed | not_reachable | unsupported_dependencies | unsafe_tree | invalid_job | disk_budget | memory_budget | budget_check_failed | busy | usage | error` (see "Integration" above).
Other fields: `duration_s`, `errors[{file (relative to the checkout), line, column (1-based), message,
type}]`, `warnings` (same shape, at most 50), `error_count`, `warning_count`, `log_tail` (on failure) and `cleanup_ok`. A `dispatch` object adds
`domain: system`, `label`, `pre_sweep_ok`, `post_sweep_ok`, `leftover_pids` and `launchd_last_exit`.

Real example (compile error test):
`{"column": 23, "file": "Sources/Greeting.swift", "line": 2, "message": "Cannot convert value of type 'String' to specified type 'Int'", "type": "Swift Compiler Error"}`

### Cleanup (kills only uid 470, by construction)

- **bh-runner:** `kill_all_builder_processes()` returns immediately unless `getuid() == geteuid() == 470`. It
  lists pids with `proc_listpids(PROC_UID_ONLY, 470)` and filters them to `> 1` and not its own pid. It sends
  SIGSTOP until a scan finds nothing new (so a fork loop can't outrun it), then SIGKILLs them all and reaps
  its zombies. It never calls `kill(-1)` (on macOS that also kills the caller) and never signals a process
  group.
- **bh-dispatch:** runs as root, but its sweeps run in a forked child that first calls `setgroups([])`,
  `setgid(470)` and `setuid(470)`, then calls the same function. The kernel therefore can't let it signal
  anything but uid 470.
- **teardown-bh-builder.sh:** runs `pkill -9 -u 470` only inside `sudo -u bh-builder sh -c`, after checking
  both `id -u` and `id -ru` are 470.
- **Tests:** `run-tests.sh` keeps a uid-501 sentinel process alive across the guard, timeout, cancel and
  probe-suite steps, and asserts it survives each one (it does). It also runs the kill function as uid 501
  and asserts that it is a no-op.
- **Apple agents:** launchd itself starts some per-user Apple agents (`distnoted`, `cfprefsd`, `lsd`,
  `trustd`, `secd`, `secinitd`, `containermanagerd`; ppid 1, exact paths) on demand for uid 470. Every sweep
  kills them too. They are only excluded from the "nothing left" check, because launchd can respawn them
  immediately.
- **Per-user dirs:** after each job, bh-runner wipes the builder's per-user T and C dirs and
  `~/Library/{Preferences,LaunchAgents}`.

## Launchd session evidence

These lines come from the hostile build's own Run Script phase and CC wrapper under `allow-all.sb`. That
profile has no sandbox at all, so it shows the uid/launchd boundary alone:

```
info: PROBE script.info_launchd_session=System (manageruid=0)
info: PROBE cc.info_launchd_session=System (manageruid=0)
info: PROBE script.info_launchd_ran_submit=DENIED (did not run)
info: PROBE script.info_launchd_ran_boot=DENIED (did not run)
launchctl_submit               PASS PASS     (launchctl submit exits non-zero)
launchctl_submit_copied_binary PASS PASS
launchctl_bootstrap_user       PASS PASS     (bootstrap user/470)
launchctl_bootstrap_gui_owner  PASS PASS     (bootstrap gui/501)
launchd_owner_session          PASS PASS     (managername != Aqua, manageruid != 501)
post:launchd_ran_as_other_uid  PASS (uids: none)    <- nothing the build submitted ever ran, as any uid
post:launchd_job_fires_after_job PASS
```

Under `builder.sb`, `/bin/launchctl` can't be executed, and a copied launchctl can't reach launchd
(managername comes back empty). All the launchd rows pass there too.

`sudo launchctl print system/com.borrowedhands.runner` during a (hanging) build:

```
	path = /opt/borrowed-hands/run/com.borrowedhands.runner.plist
	type = LaunchDaemon
	state = running
	domain = system
	username = bh-builder
	group = bh-builder
	properties = runatload | creates session | launch only once | inferred program | no initgroups
USER         PID  PPID COMMAND
bh-builder 53655     1 .../Python -I /opt/borrowed-hands/libexec/bh-runner ...
bh-builder 53685 53655 .../xcodebuild -project /Users/Shared/.bh-builder-home/jobs/<id>/src/...
bh-builder 53697     1 bh-hang-marker-daemon
```

After the deadline, the dispatcher reported `status: timeout`, `leftover_pids: false`, and the label was
gone. The detached `bh-hang-marker-daemon` was killed.

## Probe suite results (real output, after fix round 1)

`scripts/probe-suite.sh` builds `tests/hostile-app` through the full dispatch path, twice. First it runs a
positive control under allow-all.sb (no sandbox), then it runs the profile under test. A Run Script phase
and a project-controlled `CC` wrapper (`CC = $(SRCROOT)/probes/cc-wrapper.sh`) each run `probes/probe.sh`.

- **Attacker model:** it knows the owner's username and home. The suite places bait first: a world-readable
  marker in `/Users/Shared` and in `/private/tmp`, a loopback TCP listener, and a "previous job" dir owned
  by uid 470.
- **PASS:** the escape worked in the control and was blocked under the profile.
- **FAIL:** the escape worked under the profile.
- **INCONCLUSIVE:** it was blocked in the control too, or the target doesn't exist on this machine, so the
  row says nothing about the profile.
- **`post:` rows** are checked after the job ends:
  - leftover processes and the probe daemon;
  - whether the runner label is still loaded;
  - uids of anything launchd ran for the build, and launchd jobs still firing 12 s later;
  - prefs, SysV objects and files left outside the job.

`builder.sb` (the deliverable):

```
probe                            script        cc           
-----                            ------        --           
defaults_write                   PASS          PASS         
hdiutil_copied                   INCONCLUSIVE  INCONCLUSIVE 
hdiutil_info                     PASS          PASS         
ipc_posix_sem                    PASS          PASS         
ipc_posix_shm                    PASS          PASS         
ipc_sysv_sem_op                  PASS          PASS         
ipc_sysv_shm_attach              PASS          PASS         
keychain_api_copied_security     INCONCLUSIVE  INCONCLUSIVE 
keychain_api_find_cert           PASS          PASS         
keychain_api_list                PASS          PASS         
kill_other_user                  INCONCLUSIVE  INCONCLUSIVE 
launchctl_bootstrap_gui_owner    INCONCLUSIVE  INCONCLUSIVE 
launchctl_bootstrap_user         INCONCLUSIVE  INCONCLUSIVE 
launchctl_submit                 INCONCLUSIVE  INCONCLUSIVE 
launchctl_submit_copied_binary   INCONCLUSIVE  INCONCLUSIVE 
launchd_owner_session            INCONCLUSIVE  INCONCLUSIVE 
list_builder_keychains           PASS          PASS         
list_users                       PASS          PASS         
list_users_shared                PASS          PASS         
list_var_folders                 PASS          PASS         
net_dns_libinfo                  PASS          PASS         
net_dns_udp                      PASS          PASS         
net_https_curl                   PASS          PASS         
net_tcp_dns_name                 PASS          PASS         
net_tcp_ip                       PASS          PASS         
net_tcp_loopback                 PASS          PASS         
open_app                         INCONCLUSIVE  INCONCLUSIVE 
open_url                         INCONCLUSIVE  INCONCLUSIVE 
osascript_copied_binary          INCONCLUSIVE  INCONCLUSIVE 
osascript_do_shell               PASS          PASS         
osascript_system_events          INCONCLUSIVE  INCONCLUSIVE 
pbcopy                           INCONCLUSIVE  INCONCLUSIVE 
pbpaste                          INCONCLUSIVE  INCONCLUSIVE 
prefs_read_via_cfprefsd          INCONCLUSIVE  INCONCLUSIVE 
ps_other_users_cmdline           INCONCLUSIVE  INCONCLUSIVE 
ps_other_users_env               INCONCLUSIVE  INCONCLUSIVE 
read_builder_home_root           PASS          PASS         
read_etc_hosts                   PASS          PASS         
read_etc_passwd                  PASS          PASS         
read_network_prefs               PASS          PASS         
read_other_job_marker            PASS          PASS         
read_owner_development           INCONCLUSIVE  INCONCLUSIVE 
read_owner_public                INCONCLUSIVE  INCONCLUSIVE 
read_owner_ssh                   INCONCLUSIVE  INCONCLUSIVE 
read_owner_tmpdir                INCONCLUSIVE  INCONCLUSIVE 
read_owner_var_folders           PASS          PASS         
read_shared_marker               PASS          PASS         
read_system_keychain_file        PASS          PASS         
read_tmp_marker                  PASS          PASS         
read_wifi_prefs                  INCONCLUSIVE  INCONCLUSIVE 
screencapture                    INCONCLUSIVE  INCONCLUSIVE 
sudo_nopass                      INCONCLUSIVE  INCONCLUSIVE 
unix_com_docker_vmnetd_sock      INCONCLUSIVE  INCONCLUSIVE 
unix_cupsd                       PASS          PASS         
unix_fctvpnctrl_sock             INCONCLUSIVE  INCONCLUSIVE 
unix_portmap_socket              PASS          PASS         
unix_syslog                      INCONCLUSIVE  INCONCLUSIVE 
unix_systemkeychaincheck_socket  PASS          PASS         
unix_usbmuxd                     PASS          PASS         
users_via_opendirectory          PASS          PASS         
write_builder_home_root          PASS          PASS         
write_builder_launchagents       PASS          PASS         
write_other_job                  PASS          PASS         
write_private_var_tmp            PASS          PASS         
write_tmp                        PASS          PASS         
write_users_shared               PASS          PASS         
post:leftover_processes          PASS          
post:daemon_survives             PASS          
post:runner_label_still_loaded   PASS          
post:launchd_ran_as_other_uid    PASS          (uids: none)
post:launchd_job_fires_after_job PASS          
post:defaults_persist            PASS          
post:sysv_ipc_left_behind        PASS          
post:outside_files_left          PASS          

SUITE build=succeeded probes=77 pass=86 fail=0 inconclusive=54 (control: allow-all.sb)
```

Most INCONCLUSIVE rows are stopped by uid or by the launchd session, so the control blocks them too:
- other users' data, `ps` of other users, kill, sudo;
- launchctl submit/bootstrap, which never ran in the control either (`post:launchd_ran_as_other_uid` has no
  uids under any profile);
- GUI and pasteboard rows, because there is no Aqua session.

The rest fail for unrelated reasons: copied security/hdiutil/osascript binaries, the Wi-Fi plist (not
world-readable on the test Mac), the cfprefsd loginwindow domain, and sockets absent on the test Mac (docker
vmnetd, FortiClient, syslog connect). The launchd session evidence is the unsandboxed control's own
readout: `info_launchd_session=System (manageruid=0)`.

Summary lines (the table above is from fix round 1; after fix round 2, which added `signal_runner`,
`signal_launchd_agent` and `ipc_sysv_msg_send`):
- `builder.sb`: `SUITE build=succeeded probes=80 pass=90 fail=0 inconclusive=56`
- `allow-all.sb`: `pass=7 fail=85 inconclusive=54` (only the post rows pass without a sandbox)

### Earlier comparison against the feasibility profile (before the fix round, no control)

The table below is from before the positive control existed. Read its PASS cells under allow-all as
"blocked by uid/session", not "blocked by the profile".

| probe | builder.sb | medium.sb | allow-all.sb |
|---|---|---|---|
| `defaults_write` | PASS | PASS | FAIL |
| `hdiutil_copied` | PASS | PASS | PASS (*) |
| `hdiutil_info` | PASS | FAIL | FAIL |
| `keychain_api_copied_security` | PASS | PASS | PASS (*) |
| `keychain_api_find_cert` | PASS | FAIL | FAIL |
| `keychain_api_list` | PASS | FAIL | FAIL |
| `kill_other_user` | PASS | PASS | PASS |
| `launchctl_bootstrap_gui_owner` | PASS | PASS | PASS |
| `launchctl_bootstrap_user` | PASS | PASS | PASS |
| `launchctl_submit` | PASS | PASS | PASS |
| `launchctl_submit_copied_binary` | PASS | PASS | PASS |
| `launchd_owner_session` | PASS | PASS | PASS |
| `list_builder_keychains` | PASS | PASS | FAIL |
| `list_users` | PASS | FAIL | FAIL |
| `list_users_shared` | PASS | FAIL | FAIL |
| `list_var_folders` | PASS | FAIL | FAIL |
| `net_dns_libinfo` | PASS | PASS | FAIL |
| `net_dns_udp` | PASS | PASS | FAIL |
| `net_https_curl` | PASS | PASS | FAIL |
| `net_tcp_dns_name` | PASS | PASS | FAIL |
| `net_tcp_ip` | PASS | PASS | FAIL |
| `net_tcp_loopback` | PASS | PASS | FAIL |
| `open_app` | PASS | PASS | PASS |
| `open_url` | PASS | PASS | PASS |
| `osascript_copied_binary` | PASS | PASS | PASS |
| `osascript_do_shell` | PASS | FAIL | FAIL |
| `osascript_system_events` | PASS | PASS | PASS |
| `pbcopy` | PASS | PASS | PASS |
| `pbpaste` | PASS | PASS | PASS |
| `prefs_read_via_cfprefsd` | PASS | PASS | PASS (*) |
| `ps_other_users_cmdline` | PASS | PASS | PASS |
| `ps_other_users_env` | PASS | PASS | PASS |
| `read_builder_home_root` | PASS | FAIL | FAIL |
| `read_etc_hosts` | PASS | FAIL | FAIL |
| `read_network_prefs` | PASS | FAIL | FAIL |
| `read_other_job_marker` | PASS | FAIL | FAIL |
| `read_owner_development` | PASS | PASS | PASS |
| `read_owner_public` | PASS | PASS | PASS |
| `read_owner_ssh` | PASS | PASS | PASS |
| `read_owner_tmpdir` | PASS | PASS | PASS |
| `read_owner_var_folders` | PASS | FAIL | FAIL |
| `read_shared_marker` | PASS | FAIL | FAIL |
| `read_system_keychain_file` | PASS | FAIL | FAIL |
| `read_tmp_marker` | PASS | FAIL | FAIL |
| `read_wifi_prefs` | PASS | PASS | PASS (*) |
| `screencapture` | PASS | PASS | PASS |
| `sudo_nopass` | PASS | PASS | PASS |
| `unix_com_docker_vmnetd_sock` | PASS | PASS | PASS (absent on test Mac) |
| `unix_cupsd` | PASS | PASS | FAIL |
| `unix_fctvpnctrl_sock` | PASS | PASS | PASS (absent on test Mac) |
| `unix_portmap_socket` | PASS | PASS | FAIL |
| `unix_syslog` | PASS | PASS | PASS |
| `unix_systemkeychaincheck_socket` | PASS | PASS | FAIL |
| `unix_usbmuxd` | PASS | PASS | FAIL |
| `users_via_opendirectory` | PASS | FAIL | FAIL |
| `write_builder_home_root` | PASS | PASS | FAIL |
| `write_builder_launchagents` | PASS | PASS | FAIL |
| `write_other_job` | PASS | PASS | FAIL |
| `write_private_var_tmp` | PASS | PASS | FAIL |
| `write_tmp` | PASS | PASS | FAIL |
| `write_users_shared` | PASS | PASS | FAIL |
| `post:leftover_processes` | PASS | PASS | PASS |
| `post:daemon_survives` | PASS | PASS | PASS |
| `post:runner_label_still_loaded` | PASS | PASS | PASS |
| `post:launchd_ran_as_other_uid` | PASS | PASS | PASS |
| `post:launchd_job_fires_after_job` | PASS | PASS | PASS |
| `post:defaults_persist` | PASS | PASS | PASS |
| `post:outside_files_left` | PASS | PASS | FAIL |

Summary lines: `builder.sb: SUITE build=succeeded probes=69 pass=129 fail=0`;
`medium.sb: pass=97 fail=32`; `allow-all.sb: pass=60 fail=69`.

How to read the rows that pass even under allow-all:
- **GUI rows** (`open_*`, `osascript_system_events`, `pbcopy`/`pbpaste`, `screencapture`): the build has no
  GUI session (system domain, no Aqua), so these pass on uid and session alone.
- **Owner-data rows** (`read_owner_*` except var_folders, `ps_other_users_*`, `kill_other_user`,
  `sudo_nopass`): uid already blocks these.
- **(*) rows:** these probes are inconclusive. The copied `security` / `hdiutil` binaries fail even without
  a sandbox. `com.apple.loginwindow` doesn't exist and the Wi-Fi plist isn't world-readable on the test Mac. The
  non-copied rows (`keychain_api_*`, `hdiutil_info`, `read_network_prefs`) are the meaningful ones, and
  builder.sb blocks them.

`info_spawn_daemon_started` / `info_spawn_nohup_started` are informational: a sandbox can't forbid fork, so
the build always starts its detached daemons. The verdict is `post:daemon_survives` and
`post:leftover_processes`.

## builder.sb in brief

- `(allow default)` as the base, then:
- **signals:** `(deny signal) (allow signal (target same-sandbox))`. The build can't signal the runner,
  its checks, or launchd's per-user agents, even though they share its uid.
- **network:** `deny network*` + `deny system-socket`. No TCP, UDP, unix-socket connect, or any socket.
- **writes:** only `<job>/{src,dd,tmp,home,result.xcresult}`, the per-user T and C dirs (wiped after the
  job), and /dev null/tty/fd. The job root isn't writable, so `logs/` and `result.json` can't be forged.
- **reads:** an allowlist. `file-read-metadata` everywhere (stat/readlink for path lookup). Contents and
  listings only from `/System /usr /bin /sbin /Applications/Xcode.app /Library/Developer /Library/Apple`,
  tz/dyld dbs, `/private/etc/{localtime,protocols,services}` (not passwd/group), the global and Xcode prefs plists, `/Library/Preferences/Logging`,
  `/dev`, the job dir, and the per-user T and C dirs.
- **mach-lookup:** only `com.apple.bsd.dirhelper` (for local-path Swift packages; see below).
- **`deny iokit-open`**, plus `iokit-get-properties` denied for serial number, platform UUID and MAC
  addresses, and `sysctl-read` denied for `net.*`.
- **`deny file-write-flags file-write-acl`** (no `chflags uchg`, no ACLs).
- **`deny ipc-posix-shm* ipc-posix-sem* ipc-sysv*`.** SysV creation isn't mediated, so the runner also removes uid-470
  SysV objects at teardown.
- **process-exec denylist:** osascript, open, launchctl, security, hdiutil, screencapture, pbcopy/paste,
  sudo/su/login, ssh/scp, curl, nc, dscl, defaults, systemsetup, tccutil. This is defense in depth only:
  copying the binary bypasses it (the suite does exactly that), and the mach and file rules are the
  boundary.
- **xcresulttool** runs under the same profile.

### Mach services

Allowed: **`com.apple.bsd.dirhelper` only** (local-path Swift packages need it; see the integration
table). Before that: none. I discovered the lookups with `scripts/sandbox-violations.sh` (deny all, read the kernel's
Sandbox log lines). xcodebuild, SWBBuildService, swift-frontend, swift-driver, ld and lsregister look up:

- `com.apple.system.opendirectoryd.libinfo` and `.membership`
- `com.apple.bsd.dirhelper`
- `com.apple.logd`
- `com.apple.system.notification_center`
- `com.apple.distributed_notifications@1v3`
- `com.apple.cfprefsd.{agent,daemon}`
- `com.apple.lsd.mapdb` and `com.apple.lsd.modifydb`
- `com.apple.CoreServices.coreservicesd`
- `com.apple.coreservices.quarantine-resolver`
- `com.apple.mobileassetd.v2`
- `com.apple.FSEvents`
- `com.apple.DiskArbitration.diskarbitrationd`
- `com.apple.CoreSimulator.CoreSimulatorService`
- `com.apple.CoreDevice.CoreDeviceService`
- `com.apple.metadata.mds`
- `com.apple.PowerManagement.control`
- `com.apple.SecurityServer`, `com.apple.securityd.xpc`
- `com.apple.SystemConfiguration.configd`
- `com.apple.analyticsd`
- `com.apple.FileCoordination`
- `com.apple.backupd.sandbox.xpc`
- `com.apple.security.syspolicy.exec`

How I checked that none are needed:
- **Ablation:** I removed each of the first seven (the ones a strict profile would normally keep) one at a
  time. The sample app still built every time: `ablate-0..6: status=succeeded` in about 9 s each.
- **All denied:** with all of them denied, the sample app, the hostile app (Run Script + CC wrapper) and the
  compile-error case build or fail correctly. This includes a freshly re-created bh-builder (teardown, then
  setup, then the first build: `status=succeeded duration=9.22`).

What it costs: os_log output from build tools is dropped, and getpwuid/getgrouplist fall back to
`/etc/passwd` and `/etc/group`. That's why `whoami` inside the build prints `470`.

This is the boundary for keychain, Launch Services, Apple Events, pasteboard, DiskArbitration, cfprefsd
proxy reads, opendirectory user enumeration, and network by proxy (nsurlsessiond and others). If a future
project needs a service, add exactly that `global-name`, with the violation log line as evidence.

## Remaining gaps (precise)

1. **File metadata is readable everywhere.** stat/lstat/getattrlist/readlink are allowed on any path. A
   build can confirm that a path it already knows exists, and learn its size and mtime. It can't list
   directories or read contents outside the allowlist. Paths in other users' homes are still blocked by uid.
2. **The exec denylist can be bypassed by copying the binary.** That's by design; the mach, file and network
   rules hold either way (rows `*_copied_*`).
3. **fork can't be denied.** Detached daemons live until the end-of-job sweep, at most the job timeout. They
   can't do more than the build can, because they inherit the sandbox. The plist sets
   `NumberOfProcesses` 256 (RLIMIT_NPROC, counted per uid; the effective cap is min(256,
   `kern.maxprocperuid`), which was 1333 on the test Mac) and `NumberOfFiles` 1024 (RLIMIT_NOFILE, per process).
   Tested: a fork bomb toward 2000 peaked at 256 uid-470 processes; 3 processes × 10k opens each stopped
   at 1021. Global `kern.maxproc` (2000) and `kern.maxfiles` (30720) are shared with the owner's session; at
   these caps a job uses at most ~13% and 3 × 1024 of them respectively.
4. **Disk and memory budgets are polled, not enforced by the kernel.** macOS has no per-uid quota or RSS
   limit. The root dispatcher polls statvfs and RSS every 1 s; the runner also walks the job every 3 s. A
   build can overshoot by whatever it writes or allocates within one interval: the 3 GB dd was stopped
   between 1.3 and 2.7 GB used against a 500/1000 MB budget. The statvfs delta also counts other activity
   on the volume (Time Machine, swap), so a small budget can trip on a busy machine. The 10 GB free floor
   applies regardless. Wall-clock time is bounded by the runner timeout
   and, independently, by the dispatcher's deadline: runner timeout + `EXTRA_WAIT` (30 s). Tested with the
   runner frozen by SIGSTOP: timeout after 66 s for a 30 s job.
5. **Cross-job state.** The per-user T and C dirs are wiped after each job, and only one job runs at a time
   (flock in both dispatcher and runner). After each job the dispatcher deletes the job's build dirs (`src`,
   `dd`, `tmp`, `home`, `git`, `result.xcresult`) as uid 470, clearing BSD flags and modes first; `logs/`,
   `job.json`, `result.json` and `runner.err` stay until the caller deletes the job dir. Builds can't
   read other job dirs (row `read_other_job_marker`). Apple's per-user agents (distnoted, cfprefsd, ...) may
   be respawned by launchd between jobs; they're Apple binaries, and every sweep kills them.
6. **Swift packages: closed for 0.1.** Remote package dependencies are detected before the build and
   refused with `unsupported_dependencies` and the files to fix; vendored local-path packages build.
   Still true: no network during builds (`-disableAutomaticPackageResolution`); the iOS Simulator
   destination is compile-only. SDK macros (`@State`, `#Preview`, `@Observable`) build since
   2026-11-09, with the compiler's own plugin sandbox off and builder.sb confining the plugins (see
   "Swift macros"). Macros defined in a local package (they need a vendored swift-syntax) and build tool
   plugins are still untested under this profile.
7. **Git materialization runs under git.sb, not builder.sb,** because it needs HTTPS. git.sb is
   deny-default (fix round): outbound TCP 443 and the mDNSResponder socket only, trustd/DNS mach lookups,
   writes only to the job and per-user dirs. git is trusted code;
   the repo can't supply hooks, filters, fsmonitor, submodules or config (the hardened `-c` recipe from
   feasibility probe's materialize script). **Closed:** the runner itself requires the commit to be reachable from
   `refs/heads/bh/*` on the configured remote (filtered fetch + `merge-base --is-ancestor`, else
   `not_reachable`), and refuses unsafe trees (escaping symlinks, `.git` lookalikes) with `unsafe_tree`.
8. **Test flags are gated, and production argv is exact.** `--profile`, `--allow-file-remote` and
   `--keep-build-dirs` only work while the root-owned `TEST_MODE` flag exists. Without it, bh-dispatch accepts
   exactly these argv and nothing else (no extra, missing or reordered arguments, no leading zeros):
   ```
   --timeout T --disk-budget-mb D --mem-budget-mb M     T in {300, 900, 1800, 3600}; 1 <= D <= 102400; 1 <= M <= 65536
   --sweep-only                                          kill every uid-470 process (as uid 470), print {"status":"swept"}
   --cancel                                              job id (uuid4) on stdin; SIGTERM to the in-flight dispatcher for that job
   ```
   The pinned sudoers file (written by the helper's `bh setup`; the sandbox setup installs no sudoers
   file) lists the same argv verbatim with this Mac's budgets, one line per timeout tier plus the sweep
   and cancel lines, using the owner's numeric uid:
   ```
   #501 ALL=(root) NOPASSWD: /opt/borrowed-hands/bin/bh-dispatch --timeout 300 --disk-budget-mb 20480 --mem-budget-mb 6144
   #501 ALL=(root) NOPASSWD: /opt/borrowed-hands/bin/bh-dispatch --timeout 900 --disk-budget-mb 20480 --mem-budget-mb 6144
   #501 ALL=(root) NOPASSWD: /opt/borrowed-hands/bin/bh-dispatch --timeout 1800 --disk-budget-mb 20480 --mem-budget-mb 6144
   #501 ALL=(root) NOPASSWD: /opt/borrowed-hands/bin/bh-dispatch --timeout 3600 --disk-budget-mb 20480 --mem-budget-mb 6144
   #501 ALL=(root) NOPASSWD: /opt/borrowed-hands/bin/bh-dispatch --sweep-only
   #501 ALL=(root) NOPASSWD: /opt/borrowed-hands/bin/bh-dispatch --cancel
   ```
   No globs: sudo's `*` also matches spaces. Cancel normally needs no sudo at all: SIGTERM from the owner's
   uid to its own `sudo` child reaches the dispatcher (proven by `tests/cancel_owner.py`). `--cancel` is for
   a helper that lost its child. It only signals the pid the in-flight dispatcher recorded in the root-owned
   lock file, and only if the lock is held, the job id matches, and that pid is running
   `/opt/borrowed-hands/bin/bh-dispatch`.
9. **The runner process itself is not sandboxed.** It runs as bh-builder, and its code and profiles are
   root-owned. The build can't write `result.json` (it isn't in the write allowlist). Under a weaker profile
   (allow-all, medium), a build could pre-plant `result.json`. The runner unlinks and recreates it after the
   sweep, but a writable job root is still a weaker position.
10. **Hidden-user hygiene.** bh-builder is in the implicit `everyone` and `localaccounts` groups and in
    sharepoint/`_lpoperator` groups from the system's group config (`info_groups=470,12,61,703,704,100,701,702`).
    It isn't in staff or admin; setup checks and removes those two.
11. **DNS: pinned, with a remaining ship decision.** git connects to the address the runner checked
    (`http.curloptResolve`). A hostile DNS server can still choose which *public* address that is; see
    "Ship decision: DNS rebinding".
12. **SysV creation is unmediated.** A build can create (but not attach to or operate on) SysV shm, sem
    and msg objects until teardown removes them. The kernel limits how many can exist system-wide, so a
    build could exhaust that limit for the length of a job.
13. **Supplementary groups** (LOW 12): see the fix-round table. The build's credential always includes
    everyone(12), localaccounts(61), _lpoperator(100) and the sharepoint groups.
14. **Not tested destructively:** teardown's `--sweep-only` path and setup's symlink refusal (LOW 11).
15. **Only tested on one machine.** All results are from one Apple silicon Mac (macOS 27.2, Xcode 27.2). I haven't
    built a large real project (multiple targets, asset catalogs, Metal, xcframeworks) under builder.sb. Expect
    to extend the read allowlist or add a mach service for those, using `scripts/sandbox-violations.sh`.

## Reproduce

On the build Mac, as the admin owner, from a checkout of this repository:

```bash
sudo bash sandbox/setup-bh-builder.sh --with-test-profiles   # idempotent; test mode (TEST_MODE + profiles)
bash tests/run-tests.sh                                       # ~4 min; or `quick` for validation/unit only
bash scripts/probe-suite.sh                                    # builder.sb table
bash scripts/probe-suite.sh --profile allow-all.sb             # baseline (uid + launchd only)
bash scripts/probe-suite.sh --profile medium.sb                # feasibility profile
bash scripts/sandbox-violations.sh builder.sb sample           # denials during a sample build
bash scripts/profile-probe.sh /opt/borrowed-hands/libexec/git.sb      # git.sb rows with positive control
bash scripts/profile-probe.sh /opt/borrowed-hands/libexec/builder.sb build
sudo /usr/bin/python3 -I tests/unit_dispatch.py /opt/borrowed-hands/bin/bh-dispatch
bash tests/mutation-check.sh                                   # HIGH 1 / MEDIUM 4 mutants (restores install)
sudo bash sandbox/setup-bh-builder.sh                          # production install: no test profiles, no TEST_MODE
sudo bash sandbox/teardown-bh-builder.sh                       # remove everything
```

Run these only on a Mac you are prepared to modify: setup creates a hidden user and installs root-owned files.

### tests/run-tests.sh output (after fix round 3)

```
# host build-mac, macOS 27.2, Xcode 27.2
ok   - rejects: file:// remote without flag (invalid_job)
ok   - rejects: ssh remote (invalid_job)
ok   - rejects: https with userinfo (invalid_job)
ok   - rejects: https with port (invalid_job)
ok   - rejects: short sha (invalid_job)
ok   - rejects: project path traversal (invalid_job)
ok   - rejects: absolute project (invalid_job)
ok   - rejects: other destination (invalid_job)
ok   - rejects: extra key (invalid_job)
ok   - rejects: job_id not uuid4 (invalid_job)
ok   - rejects: not json (invalid_job)
ok   - rejects: profile path traversal (usage)
ok   - rejects: remote host 127.1 (invalid_job)
ok   - rejects: remote host 0x7f.0.0.1 (invalid_job)
ok   - rejects: remote host 10.1 (invalid_job)
ok   - rejects: remote host 169.254.169.254 (invalid_job)
ok   - rejects: remote host 100.64.0.1 (invalid_job)
ok   - rejects: remote host 192.168.1.1 (invalid_job)
ok   - rejects: remote host 1.1.1.1 (invalid_job)
ok   - rejects: remote host [::1] (invalid_job)
ok   - rejects: remote host [fd00::1] (invalid_job)
ok   - rejects: remote host localtest.me (invalid_job)
ok   - rejects: remote host 127.0.0.1.nip.io (invalid_job)
ok   - rejects: remote port 99999 (invalid_job)
ok   - dispatch --timeout 5² -> usage (usage)
ok   - dispatch --disk-budget-mb 99999999 -> usage
ok   - rejects: 2000-deep JSON (invalid_job)
ok   - no job dir created by rejected jobs
ok   - prod setup: profiles/ is empty
ok   - prod setup: no TEST_MODE flag
ok   - prod mode refuses '--profile allow-all.sb' (usage)
ok   - prod mode refuses '--profile ablate-0.sb' (usage)
ok   - prod mode refuses '--profile builder.sb' (usage)
ok   - prod mode refuses '--allow-file-remote' (usage)
ok   - prod mode refuses '--keep-build-dirs' (usage)
ok   - prod mode: no job dir and no plist written
ok   - test setup restored (TEST_MODE + profiles)
ok   - setup refuses a pre-created dir owned by uid 501 with a file in it (/Users/Shared/bh-test-home-91976 exists (uid 501, mode 755), not ours; refusing)
ok   - setup refuses a pre-created empty dir owned by uid 501
ok   - setup refuses a symlink at the home path
ok   - setup creates a missing home exclusively as 470:700
  ok   - read_result accepts a 0600, uid-470, single-link regular file
  ok   - read_result rejects mode 0644
  ok   - read_result rejects a root-owned file
  ok   - read_result rejects a hardlink to a root-owned file
  ok   - read_result rejects nlink 2 even when owner/mode are right
  ok   - read_result rejects a symlink (to an otherwise valid uid-470 file)
  ok   - read_result rejects a FIFO (and does not block)
  ok   - read_result rejects deeply nested JSON without raising
  ok   - pre-sweep failure -> status error
  ok   - pre-sweep failure -> no job dir and no bootstrap
  ok   - post-sweep failure -> status error, result.json (succeeded) ignored
  ok   - 2000-deep JSON -> invalid_job (no RecursionError)
  ok   - validate_remote(https://github.com:99999/x/y.git) -> invalid_job
  ok   - validate_remote(https://[::1/x.git) -> invalid_job
  ok   - validate_remote(https://github.com:-1/x.git) -> invalid_job
  ok   - user_dir rejects '/private/var/folders/x"y/T/'
  ok   - user_dir rejects '/tmp/T/'
  ok   - user_dir rejects '/private/var/folders/../../etc/'
  ok   - runner: disk measurement failure -> budget_check_failed
  ok   - runner: ps failure -> mem_used_mb raises (no silent zero)
  ok   - runner: unparsable ps output -> budget_check_failed
  ok   - resolve_public rejects 2002:7f00:1::1
  ok   - resolve_public rejects 2001:0:4136:e378::1
  ok   - resolve_public rejects 64:ff9b::7f00:1
  ok   - resolve_public rejects ::ffff:10.0.0.1
  ok   - resolve_public rejects 100.64.1.1
  ok   - resolve_public rejects fd12::1
  ok   - resolve_public accepts public v4+v6
  ok   - git argv pins the checked address (http.curloptResolve=github.com:443:140.82.112.3)
  ok   - validate_job_dir rejects '<jobs>/../jobs/aaaaaaaa-2222-4333-8444-55555555beef'
  ok   - validate_job_dir rejects '<jobs>/AAAAAAAA-2222-4333-8444-55555555BEEF'
  ok   - validate_job_dir rejects '/tmp/jobs/aaaaaaaa-2222-4333-8444-55555555beef'
  ok   - validate_job_dir rejects '<jobs>/aaaaaaaa-2222-4333-8444-55555555beef/'
  ok   - validate_job_dir rejects '<jobs>/xaaaaaaaa-2222-4333-8444-55555555beef'
  ok   - validate_job_dir rejects '<jobs>/aaaaaaaa-2222-4333-8444-55555555beef\x00'
  ok   - validate_job_dir rejects '<jobs>/aaaaaaaa-2222-1333-8444-55555555beef'
  ok   - create_runner_err makes a fresh 0600 uid-470 file
  ok   - create_runner_err refuses an existing runner.err
  ok   - create_runner_err refuses a planted symlink
  ok   - create_runner_err refuses a job dir not owned by uid 470
  ok   - create_runner_err refuses a symlinked job dir
  ok   - teardown retries through EAGAIN (no crash): ok
  ok   - teardown completed every step after retries
  # unit pass=43 fail=0
ok   - unit_dispatch.py (read_result, sweep failures, JSON/URL, user_dir)
  profile-probe: /opt/borrowed-hands/libexec/git.sb (as uid 501, build-mac)
  read_etc_hosts                 PASS   control=ok     sandboxed=DENIED 
  read_etc_passwd                PASS   control=ok     sandboxed=DENIED 
  read_wifi_prefs                PASS   control=ok     sandboxed=DENIED 
  read_network_prefs             PASS   control=ok     sandboxed=DENIED 
  read_system_keychain_file      PASS   control=ok     sandboxed=DENIED 
  read_shared_marker             PASS   control=ok     sandboxed=DENIED 
  list_users                     PASS   control=ok     sandboxed=DENIED 
  list_users_shared              PASS   control=ok     sandboxed=DENIED 
  chflags_uchg                   PASS   control=ok     sandboxed=DENIED 
  write_tmp                      PASS   control=ok     sandboxed=DENIED 
  net_tcp_80                     PASS   control=ok     sandboxed=DENIED 
  net_tcp_22                     PASS   control=ok     sandboxed=DENIED 
  net_udp_53                     PASS   control=ok     sandboxed=DENIED 
  net_tcp_443                    PASS   control=ok     sandboxed=ok     (must work)
  exec_perl                      PASS   control=ok     sandboxed=DENIED 
  exec_osascript                 PASS   control=ok     sandboxed=DENIED 
  PROFILE-PROBE pass=16 fail=0 inconclusive=0
ok   - git.sb profile probe
  profile-probe: /opt/borrowed-hands/libexec/builder.sb (as uid 501, build-mac)
  read_etc_hosts                 PASS   control=ok     sandboxed=DENIED 
  read_etc_passwd                PASS   control=ok     sandboxed=DENIED 
  read_wifi_prefs                PASS   control=ok     sandboxed=DENIED 
  read_network_prefs             PASS   control=ok     sandboxed=DENIED 
  read_system_keychain_file      PASS   control=ok     sandboxed=DENIED 
  read_shared_marker             PASS   control=ok     sandboxed=DENIED 
  list_users                     PASS   control=ok     sandboxed=DENIED 
  list_users_shared              PASS   control=ok     sandboxed=DENIED 
  chflags_uchg                   PASS   control=ok     sandboxed=DENIED 
  write_tmp                      PASS   control=ok     sandboxed=DENIED 
  net_tcp_80                     PASS   control=ok     sandboxed=DENIED 
  net_tcp_22                     PASS   control=ok     sandboxed=DENIED 
  net_udp_53                     PASS   control=ok     sandboxed=DENIED 
  net_tcp_443                    PASS   control=ok     sandboxed=DENIED 
  exec_osascript                 PASS   control=ok     sandboxed=DENIED 
  PROFILE-PROBE pass=15 fail=0 inconclusive=0
ok   - builder.sb profile probe
ok   - bh-runner refuses to run as uid 501
ok   - kill_all_builder_processes() is a no-op as uid 501
ok   - sentinel (uid 501) alive after guard test
ok   - bh-dispatch refuses to run without root
ok   - sample app (Probe) builds: succeeded in 8.94s
ok   - no bh-builder processes after a build
ok   - runner label booted out after a build
ok   - result.json/dispatch fields present
ok   - https fetch works under git.sb (invalid_job: project path missing or not a real directory in the commit)
bash: line 1: 92886 Alarm clock: 14         /usr/bin/perl -e "alarm 20; exec @ARGV" /Applications/Xcode.app/Contents/Developer/usr/bin/git -c http.curloptResolve=github.com:443:192.0.2.1 ls-remote https://github.com/octocat/Hello-World.git HEAD > /dev/null 2>&1
ok   - git honours the DNS pin (pinned to 192.0.2.1 -> no answer)
ok   - fixture commit really contains a gitlink
ok   - fixture: checkout kept for inspection (--keep-build-dirs, test mode)
ok   - hostile git fixture materializes and builds (succeeded)
ok   - fixture: submodule not fetched (src/sub empty)
ok   - fixture: symlink kept as a link, not followed
ok   - Run Script writing 3 GB with a 500 MB budget -> disk_budget (dispatcher (statvfs): volume free space fell by 2620 MB (budget 500 MB), 313141 MB free (floor 10240 MB))
ok   - no bh-builder processes after disk_budget
ok   - unlinked-but-open 3 GB file, 1000 MB budget -> disk_budget from the dispatcher (disk_budget: dispatcher (statvfs): volume free space fell by 2653 MB (budget 1000 MB), 313108 MB free (floor 10240 MB))
ok   - no bh-builder processes after the unlinked-file hog
ok   - volume still writable after the unlinked-file hog
  BH-STOPPER runner_pid=93350
  BH-STOPPER kill_stop_runner=DENIED
  BH-STOPPER pkill_stop_runner=DENIED
  BH-STOPPER kill_kill_runner=DENIED
ok   - stopper knew the runner's pid (not a vacuous DENIED)
ok   - build cannot kill -STOP the runner
ok   - build cannot pkill -STOP the runner
ok   - build cannot kill -KILL the runner
ok   - budget still enforced after the stop attempts (disk_budget)
ok   - no bh-builder processes after the stopper
ok   - control (allow-all.sb): the build can SIGSTOP and SIGKILL the runner
ok   - control: dispatcher still ends the job and sweeps (error)
ok   - Run Script allocating 1.5 GB with a 700 MB budget -> memory_budget (dispatcher: bh-builder processes use 2454 MB RSS (budget 700 MB))
ok   - memory budget enforced by the root dispatcher (message starts 'dispatcher:')
ok   - no bh-builder processes after memory_budget
  forkbomb: forked=250 peak_uid470_procs=256 status=budget_check_failed
ok   - forkbomb: one JSON status line (budget_check_failed)
ok   - forkbomb: capped by NumberOfProcesses 256 (peak 256 uid-470 processes)
ok   - forkbomb: no bh-builder processes after
ok   - forkbomb: runner label booted out
ok   - forkbomb: sentinel (uid 501) alive
ok   - forkbomb: owner can still fork
  fds: opened=1021 opened=1021 opened=1021 status=timeout
ok   - fds: each process capped by NumberOfFiles 1024
ok   - fds: one JSON status line, no leftovers, label gone, sentinel alive
ok   - fds: no bh-builder processes after
ok   - fds: label gone and sentinel alive
  immutable: chflags_uchg=DENIED chflags_uchg_dir=DENIED chmod_000=ok status=succeeded job_dirs_removed=True
ok   - immutable: chflags uchg denied by builder.sb
ok   - immutable: dispatcher removed the build dirs (only logs/job.json/result.json/runner.err left)
ok   - immutable: rm -rf of the job dir succeeds
ok   - immutable: free space back within 200 MB of the start (315757 -> 315757 MB)
  freezer: freezer_stopped=ok status=timeout elapsed=66s msg=runner did not finish within timeout + 30s
ok   - freezer: the build really SIGSTOPped the runner
ok   - freezer: dispatcher deadline -> timeout within timeout+30s(+15 slack) (66s)
ok   - freezer: no bh-builder processes after
  errors: [{"column": 23, "file": "Sources/Greeting.swift", "line": 2, "message": "Cannot convert value of type 'String' to specified type 'Int'", "type": "Swift Compiler Error"}]
ok   - error build reports failed
ok   - error reported at Sources/Greeting.swift line 2 (1-based)
ok   - hanging build -> status timeout (timeout, 46.96s)
ok   - no bh-builder processes after timeout (detached daemons included)
ok   - sentinel (uid 501) alive after timeout sweep
ok   - SIGTERM to dispatcher -> status cancelled (cancelled)
ok   - no bh-builder processes after cancel
ok   - runner label booted out after cancel
ok   - sentinel (uid 501) alive after cancel
ok   - [builder.sb] build inside launchd is not in the owner's Aqua session (launchd_owner_session not FAIL)
ok   - [builder.sb] launchd jobs submitted by the build never ran as another uid
ok   - [builder.sb] no persistence after the job (processes, launchd firing, label)
ok   - [builder.sb] full probe suite passes, no FAIL/MISSING (rc=0)
ok   - [builder.sb] build's kernel groups: 470 + implicit only (no staff/admin)
  SUITE build=succeeded probes=85 pass=100 fail=0 inconclusive=56 (control: allow-all.sb)
ok   - [allow-all.sb] build inside launchd is not in the owner's Aqua session (launchd_owner_session not FAIL)
ok   - [allow-all.sb] unsandboxed build reports managername System, manageruid 0
ok   - [allow-all.sb] launchd jobs submitted by the build never ran as another uid
ok   - [allow-all.sb] no persistence after the job (processes, launchd firing, label)
  SUITE build=succeeded probes=85 pass=7 fail=95 inconclusive=54 (control: allow-all.sb)
ok   - sentinel (uid 501) alive at the end
ok   - no bh-builder processes at the end
# pass=112 fail=0
bash tests/run-tests.sh  20.25s user 23.72s system 10% cpu 6:53.21 total
```

### tests/mutation-check.sh output (after fix round 3)

```
KILLED   - read_result without the st_nlink == 1 check
KILLED   - read_result without the mode 0600 check
KILLED   - read_result without the uid 470 check
KILLED   - read_result following symlinks
KILLED   - pre-sweep failure not aborting
KILLED   - post-sweep failure not discarding result.json
KILLED   - dispatch honouring --profile/--allow-file-remote without TEST_MODE
KILLED   - setup leaving test profiles + TEST_MODE in a production install
KILLED   - dispatcher without the statvfs disk budget
KILLED   - builder.sb without (deny signal)
KILLED   - runner budget checks failing open
KILLED   - runner without the DNS pin
KILLED   - setup adopting a home it doesn't own
KILLED   - dispatcher teardown without the EAGAIN retry guard
KILLED   - builder.sb without (deny file-write-flags file-write-acl)
KILLED   - dispatcher not deleting the job's build dirs
KILLED   - dispatcher without its memory budget
KILLED   - dispatcher without its wall-clock deadline
# mutants killed=18 survived=0
```
