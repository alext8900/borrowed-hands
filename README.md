# Borrowed Hands

Lend your Mac's Xcode to a Hermes agent in the cloud. Hermes finds your Mac, builds the project you granted, and
gets structured compiler diagnostics back. Your Mac decides what may run.

**Status: 0.2.** This repository holds the Hermes plugin (`borrowed-hands/`), the Mac helper (`helper/`), the build
sandbox (`sandbox/`), the wire protocol (`protocol/`) and their tests. The
[demo project](https://github.com/alext8900/borrowed-hands-demo) is a separate public repository. Screen capture is
not available in this version: the tool name is registered, but its handler is a stub.

```text
Hermes gateway + Borrowed Hands plugin (public HTTPS/WSS endpoint)
                         ^
                         | outbound WSS connection initiated by the Mac
                         | authenticated, signed requests and results
                         |
Mac helper (your user, local grants)
  -> pinned sudo argv -> root dispatcher
     -> system LaunchDaemon -> bh-builder -> sandboxed git / Xcode
```

The Mac needs outbound access to the gateway and to public HTTPS Git hosting. It needs no inbound listener or SSH.
Hermes approval and Mac permission are separate: approving a build in Hermes cannot add a workspace, scheme,
configuration or destination to the Mac's grants. Pairing alone enables nothing.

## Quickstart

The full walkthrough is in **[docs/QUICKSTART.md](docs/QUICKSTART.md)**. In short:

**Run Hermes.** Deploy Hermes with the plugin on Railway from `deploy/railway/Dockerfile` (volume at `/opt/data`,
domain on port 8790, `BH_OWNERS=telegram:<your id>`), or install the plugin into an existing Hermes v0.22.7+ (`hermes plugins install borrowed-hands`) and put a
TLS reverse proxy or a tunnel in front of its listener. Check `https://<your domain>/bh/v1/healthz`.

**Lend your Mac.** On an Apple silicon Mac with Xcode in `/Applications` (no checkout needed; from one,
`uv tool install ./helper` does the same):

```sh
uv tool install "git+https://github.com/alext8900/borrowed-hands@v0.2.0#subdirectory=helper"
uv tool update-shell     # then open a new terminal window
bh setup
```

At the pairing step, send `/hands pair` to your Hermes bot in a direct message and paste the `bh1:` string. Grant the
demo project (`demo`, `https://github.com/alext8900/borrowed-hands-demo.git`, `BorrowedHandsDemo.xcodeproj`, scheme
`BorrowedHandsDemo`). Hermes then tells you the Mac is ready. Ask it:

> Build my demo project on my Mac

The first build asks for the repository URL once; the approval prompt names the exact commit (the latest on the
repository's only `bh/` branch). Hermes fills in what the grant allows only one way and asks about the rest. A full
commit SHA always works as well ([details](docs/QUICKSTART.md#how-hermes-fills-in-a-build-request)).

To build your own project, push the commit to a `bh/` branch of a public repository and grant it with
`bh grant add`; see [Use your own project](docs/QUICKSTART.md#use-your-own-project).

Hermes can also change the code itself: it edits a repository with its own git tools, pushes a `bh/hermes/` branch,
builds it on your Mac, fixes what the compiler reports, and opens a pull request once a readiness check says the
branch tip is still the commit that compiled. It can start a new SwiftUI app from a bundled template the same
way. See [Hermes edits, the Mac compiles](docs/AUTONOMOUS.md).

If something is not working, `bh doctor` on the Mac and `/hands doctor` in chat each list every check with the fix
next to anything that fails.

## What works

- Discover Macs by capability, Xcode version and destination with `hands_find`; read narrowly scoped system info.
- Build a commit reachable from a `bh/*` branch of a granted public HTTPS repository. A `bh/` branch vouches for its
  whole history, including commits also on other branches. The Mac owns the repository URL and project path.
- Compile for `platform=macOS,arch=arm64` or `generic/platform=iOS Simulator`, with code signing disabled.
  Simulator support is compile-only; it does not boot, install, run or test an app.
- Return job status, duration, Xcode version, error and warning counts, and bounded file:line:column diagnostics;
  wait for or cancel a job. One build runs at a time, with at most three queued per Mac.
- Edit, build, fix, pull request: the agent builds its own pushed `bh/*` branch, the result names the commit that
  was built (`provenance`), and `hands_job` with `check_pr_ready` says whether a pull request may be opened for
  it. The plugin never pushes, opens pull requests or holds GitHub credentials; the check is advice the agent asks
  for, not a lock.
- Start a new SwiftUI app from a bundled template; the script prints the `bh grant add` command for the Mac's
  owner, and only the owner can run it.
- A bundled read-only skill, `borrowed-hands:xcode-build-pr`, gives the agent those procedures step by step.
- `bh grant add` never changes an existing grant unless you pass `--replace`; `bh unpair` deletes the pairing
  on the Mac within a fixed time whatever the server does.
- `bh doctor`, `/hands doctor` and `/hands whoami` for setup and troubleshooting.

## Tools

| Tool | Current behavior |
|---|---|
| `hands_find` | Discover paired Macs and their current granted capabilities; no Hermes approval |
| `hands_system_info` | Authenticated OS, architecture, CPU, memory, Xcode and free-disk information; no Hermes approval |
| `hands_xcode_build` | Build the granted repository/commit/scheme/configuration/destination through Hermes approval hooks and independent Mac checks |
| `hands_job` | Status, bounded wait or cancellation by job id; on request, a PR-readiness check of a build (`check_pr_ready`) |
| `hands_screen_capture` | Registered placeholder, unavailable in this version |

## Disclosures (rule 13)

The [plugin README](borrowed-hands/README.md#disclosures-rule-13) has the full list. In short:

- **Background processes.** In Hermes, when enabled and only under `hermes gateway run`: an HTTP/WebSocket listener (default port 8790, reached by Macs over your public WSS URL) on its own background thread, plus an owner-notice thread that edits the pairing message and sends readiness replies to the owner's DM. On the Mac: the helper as a user LaunchAgent, and each build as a system LaunchDaemon running as `bh-builder`.
- **Network calls.** Paired Macs connect in to the plugin. The plugin's one kind of outbound connection: the Hermes server reads the branch list (refs) of the granted public repository over HTTPS, anonymously, only to a public address, on a pinned connection with no redirects and an 8 s deadline. It does so only to resolve a build request that omits the commit, and when `hands_job` is asked for a PR-readiness check (`check_pr_ready: true`) of a build, for the repository remembered for that build's signed contract. It sends no credentials and makes no other third-party calls. The helper connects outbound to your gateway, and the runner fetches the granted repository from its public HTTPS host. Model-provider and chat-platform traffic belongs to your Hermes deployment.
- **Shell commands.** The plugin runs none. The Mac setup runs printed admin commands with `sudo` (hidden user, `/opt/borrowed-hands`, `/etc/sudoers.d/borrowed-hands`, and a byte comparison of that file with `cmp`). The helper runs `sudo -n` with six exact dispatcher command lines and `sudo -n -l --` with each of them to ask sudo whether it is permitted (permitted, not proven password-free: a later sudo rule that adds a password requirement is reported only when a build runs), plus `sw_vers`, `sysctl`, `scutil`, `xcode-select -p`, `xcodebuild -version` and `-license check`, `launchctl` and, only with per-build confirmation on, `osascript`. `bh uninstall` also reads `dscl` records to verify the build user is gone.
- **Stored credentials.** The plugin stores a generated Ed25519 server private key and a SQLite database (paired public keys, pairing-code hashes, jobs and results) in its own `$HERMES_HOME/plugin-data/` directory. The gateway can read them, so a shell or file tool on the local backend, or a reply that names the key's path (Hermes attaches it to the chat by default), can leak the key; set `HERMES_MEDIA_DELIVERY_STRICT=1` on the gateway ([details](borrowed-hands/README.md#limits-and-residual-risks)). The helper stores its device private key, pairing, grants and job state in `~/Library/Application Support/BorrowedHands`, with logs in `~/Library/Logs/BorrowedHands`.
- **Reads outside its own data.** The plugin reads its own Hermes settings and non-secret `BH_*` / `RAILWAY_PUBLIC_DOMAIN` environment fallbacks, and no secrets. Builds can read some host metadata.
- **Hooks.** `pre_tool_call` and `post_tool_call` gate builds and capture through Hermes approval.
- **Screen Recording** is not requested; capture is unavailable in this version.
- **"Administer your computer" prompt.** Deleting the build user during `bh uninstall` can make macOS ask whether your terminal app may "administer your computer"; the delete waits for the answer. Allow goes to that terminal app, not to Borrowed Hands, and how long it lasts was not established. An unanswered prompt stays open after the command ends and deletes the account whenever it is clicked ([details](docs/QUICKSTART.md#uninstall)).
- **No telemetry and no self-update.** Nothing reports usage, and nothing downloads or replaces code at run time.

## Before installing: permissions and limits

The Mac install creates a hidden `bh-builder` user, a root-owned dispatcher under `/opt/borrowed-hands`, and `/etc/sudoers.d/borrowed-hands`. That file permits the installing user's numeric uid to run **six exact dispatcher command lines** as root: four timeout tiers (`300`, `900`, `1800`, `3600`) with that Mac's fixed disk/memory budgets, plus `--sweep-only` and `--cancel`. It does not use wildcard or digit-only argument matching. Setup installs this exact rule even when your account already has broader sudo rights, so builds keep working if those change. The root dispatcher starts each build as a system LaunchDaemon running as `bh-builder`; git materialization and Xcode use separate sandbox profiles. The helper stays a user LaunchAgent. Builds always use `/Applications/Xcode.app`. Review the [setup source](helper/borrowed_hands_helper/setup.py), [dispatcher](sandbox/bh-dispatch), [runner](sandbox/bh-runner) and [sandbox notes](docs/SANDBOX.md) before granting this authority. One Mac user owns the helper; the build user and sandbox are shared machine-wide.

Repository-controlled code can run during a build in two ways, build scripts and Swift compiler plugins (macros), and both are confined by the same build sandbox. Remote Swift package resolution, private repositories, other destinations, Simulator runs and tests, and signing are outside this version. The dependency check is conservative and can reject projects that need vendoring. Diagnostics are untrusted compiler text; scrubbing is hygiene, not an injection boundary.

This is containment with residual risks, not a blanket safety guarantee. A stolen server signing key can request anything existing Mac grants allow. Code running as your Mac user can read its key and alter its grants. The hosting edge can observe result contents and disrupt service. Builds can read some host metadata. Read [protocol residual risks](docs/PROTOCOL.md#8-residual-risks-accepted-for-02). Recorded builds reported `cleanup_ok=true` and **`wipe_ok=false`**, including a protected empty `trustd` directory that could not be removed; complete removal of all build state is not established. No independent security review of 0.1 has been completed.

## Uninstall

On the Mac, `bh uninstall` stops the helper first, removes everything it installed, verifies each removal and
prints a checklist; anything left over or impossible to check is listed with the exact command to run. It never
removes an account, home or caches it cannot verify as its own. macOS keeps a few empty, protected per-user folders
for the build user under `/private/var/folders` that not even root can delete; they stay, owned by uid 470, and the
checklist notes them as nothing to do. Two of them (`TemporaryItems`) cannot even be listed without Full Disk
Access, so the teardown proves them empty from their size and link count, an observed APFS behavior rather than a
documented one; if that proof fails it keeps the build user and asks you to restart the Mac and rerun. A reinstall
reuses uid 470, so setup refuses to install while those old folders are not verifiably empty. Then
`uv tool uninstall borrowed-hands-helper`. On
Hermes, `/hands unpair` each Mac, then remove the plugin or the Railway service and volume. The complete checklist
for both sides, with paths, is in [docs/QUICKSTART.md](docs/QUICKSTART.md#uninstall).

## Roadmap

- **Private repositories**, with credentials that stay on the Mac.
- **Tests in the edit loop:** today Hermes edits, compiles and opens a pull request; next it runs the tests on the
  Simulator before it does, and follows the owner's own git preferences (branch naming, commit style, PR or direct
  push).
- **Simulator runs and tests:** boot, install, run and `xcodebuild test` with results returned.
- **App Store Connect releases:** signed archives and TestFlight or App Store uploads under the owner's approval.
- **Windows build hosts**, with the same pairing and grant model.

## How we tested it

- **Run the tests yourself.** Helper unit tests: `cd helper && uv run --frozen pytest -q`. Plugin and protocol tests
  against a Hermes v0.22.7 checkout that has a `.venv`: `HERMES_SRC=<checkout> bash tests/run-plugin-tests.sh`. The
  sandbox suite (`tests/run-tests.sh`, `scripts/probe-suite.sh`, `tests/mutation-check.sh`) creates a hidden user and
  runs hostile builds, so run it only on a dedicated test Mac; [docs/SANDBOX.md](docs/SANDBOX.md) has the steps and
  recorded output.
- **Local production build acceptance.** Real Hermes v0.22.7 with the production root dispatcher on a real Mac:
  macOS, iOS Simulator and asset-catalog builds, real compiler errors returned with file, line and column, and grant
  refusals. The model was scripted.
- **Cloud acceptance.** A real model on a Hermes gateway deployed on Railway, over public, certificate-verified WSS,
  to a production sandbox install on a real Mac: an expected compile failure with file:line diagnostics, three
  successful builds, and cancellation of a running Xcode build. Every cloud job record matched the Mac dispatcher's
  own output. Requests went through the operator interface with per-scope approvals, not chat.
- **Fresh-Mac install.** On 2026-11-09 a fresh Mac went from `uv tool install` to a successful sandboxed build in
  under a minute of machine time, and a build requested from Telegram succeeded. The friction it found (PATH, Xcode
  selection, the sudo rule, setup messages) is addressed in this version; that fix has unit tests but has not yet
  been re-run on a fresh Mac.
- **Edit, build, fix, pull request, live.** On 2026-11-10 a real model, asked from Telegram on the Railway
  deployment, changed the demo project, pushed a `bh/hermes/` branch, got a real compiler error from the Mac, fixed
  it, built again and opened a pull request; in each run the pull request's head was the commit the Mac's own job
  record says it built. The first runs skipped the readiness check, which is why the bundled skill exists; a later
  run with that skill built by branch, got `ready: true` and then opened the pull request. The new-app template
  compiled on the Mac for macOS on its first build. Cancelled and stale builds being refused are covered by tests
  only. Details and limits: [docs/AUTONOMOUS.md](docs/AUTONOMOUS.md).
- **Guarded restore.** Acceptance runs on the test Mac ran inside a guard that archived the existing production
  install first and restored it afterwards; each restore was checked file by file (bytes, owner, group and mode),
  with no runner label, build task, TEST_MODE flag or test profile left behind. Every completed compile still
  reported `cleanup_ok=true` and `wipe_ok=false` (see the limits above).
