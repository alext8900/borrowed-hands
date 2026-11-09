# Borrowed Hands

Borrow locally granted Xcode builds and system info from a Mac you paired. The Mac connects outbound to this
Hermes, and the Mac decides what may run. The Mac helper and build sandbox are in the same repository. `hands_screen_capture` is a registered
placeholder: its handler is a stub and this helper has no screen-capture executor.

Tools: `hands_find`, `hands_system_info`, `hands_xcode_build`, `hands_job`, `hands_screen_capture`.
Command: `/hands pair | status | unpair <name or id>` (owner only; `pair` only in a direct message).

`hands_xcode_build` takes every argument as optional, so "Build my demo project on my Mac" is enough. The Mac,
workspace, scheme, configuration and destination default from the Mac's grant when it allows one choice
(configuration falls back to `Debug` when granted). The repository URL defaults to the one remembered for that
grant: the Mac shares only a fingerprint, so the first build names the URL once. With no `commit`, the plugin
reads the granted repository's branch list and builds the tip of its only `bh/*` branch, or of the `bh/<name>`
given as `branch`. Several `bh/*` branches, or several granted values, return `needs_input` with the options
instead of guessing. The commit is resolved before the approval prompt: the prompt shows the first 7 characters of
the SHA that the signed contract binds in full, for example "Build demo (scheme Demo, Debug, macOS arm64) at commit
3449717 (latest on bh/fixed) on Macbook Pro M5 Max". A value that is not a plain granted-style name appears quoted
and shortened, with control characters and markup removed, so it cannot pass for another part of the prompt. The
Mac still checks its grants, its own remote and `bh/*` ancestry on every request. Hermes frames that reason with
its own fixed label for plugin approval rules, which the plugin cannot change.

Approval scope: a "session" or "always" answer covers the same Mac, workspace, scheme, configuration and
destination, never a broader one. It does not name the commit or the branch, so after such an answer a build of a
newer commit, or of the tip of a different `bh/*` branch of that granted repository, runs without a new prompt.
Answer "once" to approve each build separately.

## Disclosures (rule 13)

What this plugin does inside Hermes:

- **Long-running background process: a network listener.** When `listen.enabled` is true and the process is
  `hermes gateway run`, the plugin opens an HTTP and WebSocket listener (default `0.0.0.0`, port 8790) inside the
  Hermes gateway process, on a background thread with its own event loop. Routes: `/bh/v1/ws` and `/bh/v1/healthz`.
  Paired Macs reach it through the public WSS URL you configure (`public_url`). It is off by default and never
  starts in `hermes plugins validate`, doctor or CLI processes. Limits are under [Listener limits](#listener-limits).
- **Long-running background process: an owner-notice worker.** After `/hands pair`, a second background thread
  removes the pairing string from the pairing message and sends at most four short readiness replies to the
  owner's DM, through Hermes's own messaging adapter. Its routes are memory-only and expire after 24 hours.
- **Stored credentials and data.** In this plugin's directory under `$HERMES_HOME/plugin-data/`: a generated
  Ed25519 server private key (`server_ed25519.key`, mode 0600) and its lock file, and a SQLite database
  (`bh.sqlite3`, mode 0600) holding paired Macs' public keys, pairing-code hashes (never the codes), signed job
  contracts, job results including compiler diagnostics, owner notices, an audit log, and the public repository
  URL last named for each granted workspace.
- **Reads outside its own data.** Its own settings from Hermes config, and the non-secret environment fallbacks
  `BH_LISTEN`, `BH_PORT`, `BH_PUBLIC_URL`, `RAILWAY_PUBLIC_DOMAIN`, `BH_OWNERS` and `BH_TRUSTED_PROXY`. It reads no
  secrets: no API keys, `.env` files, Hermes `auth.json`, keychains or other tools' logins.
- **Network calls.** One outbound call of its own: when a build request names no commit, the Hermes server
  contacts the host of the granted public repository (for example github.com) over HTTPS to read its branch list,
  the same anonymous `GET <repo>/info/refs?service=git-upload-pack` request `git ls-remote` makes. It only reads
  a repository whose fingerprint matches a grant the Mac advertised. The host must be a DNS name (IP-address and
  single-label hosts are refused; a Unicode name is used as its IDNA A-label only when that conversion is
  lossless). The name is resolved once, every returned address must be public (loopback, private, link-local,
  site-local, CGNAT, multicast, reserved and unspecified addresses, and IPv6 forms that embed one, are refused),
  and the connection goes directly to that checked address, without a proxy, while TLS still verifies the
  certificate for the name. It sends no credentials or cookies and follows no redirects. The tool call stops
  waiting after 8 seconds, covering DNS, connection, TLS and the whole response (capped at 1 MiB); at that point
  the connection is shut down and no request is sent after it. A lookup stuck inside the operating system's DNS
  resolver cannot be cancelled: it keeps one background thread until the resolver gives up, at most 4 such
  lookups are outstanding, and further reads are refused until one returns. Builds with an explicit commit make
  no outbound call. Otherwise it sends build and system-info requests only to Macs you paired, over
  connections those Macs open, and makes no other third-party calls. Chat replies go through Hermes's configured
  messaging adapter. Hermes itself still uses your configured model and chat services.
- **Shell commands.** None. The plugin starts no processes.
- **Hooks.** A `pre_tool_call` hook asks for Hermes approval before `hands_xcode_build` and
  `hands_screen_capture`; for a build with no commit it first reads the branch list (see Network calls). A
  `post_tool_call` hook discards the approval record of any call that did not complete normally (blocked,
  failed, cancelled or timed out), only within that chat. Build and capture calls without a Hermes `tool_call_id`
  are blocked before approval; call them directly through the agent. Tickets bind the resolved execution scope,
  including defaults and a resolved commit, and are consumed once, only by the tool call that created it: the
  handler reads that call's id from Hermes's dispatch context (Hermes v0.22.7 does not pass it to handlers), and
  without one it builds nothing. A build that cannot be resolved gets no approval prompt and returns its reason
  instead. While one call waits for approval, an identical call in the same chat is refused rather than replacing
  its ticket; once the approved call starts building, an identical call is a new request. Hermes approval is not
  the security boundary: the Mac checks its own grants on every request. The plugin also adds a short system
  prompt section and the `/hands` command.
- **No telemetry.** Nothing is reported to the author or any third party.
- **No self-update.** The plugin never downloads or replaces code. Updates arrive only as a new reviewed pin.

What the separate Mac install does. It is not loaded by Hermes; its source is in the same repository
([setup](../helper/borrowed_hands_helper/setup.py), [dispatcher](../sandbox/bh-dispatch),
[runner](../sandbox/bh-runner), [sandbox notes](../docs/SANDBOX.md)):

- **Shell commands and sudo.** `bh setup` prints each admin command before running it with `sudo`: it creates a
  hidden macOS user `bh-builder`, installs a root-owned dispatcher and sandbox profiles in `/opt/borrowed-hands`,
  and adds one syntax-checked sudoers drop-in file named `borrowed-hands`. That file authorizes your
  numeric uid for six exact dispatcher command lines as root: four timeout tiers (`300`, `900`, `1800`, `3600`)
  with this Mac's fixed disk/memory budgets, plus `--sweep-only` and `--cancel`. There are no wildcard argument
  grants. At run time the helper calls `sudo -n` with only those command lines, and reads system facts with
  `sw_vers`, `sysctl`, `scutil` and `xcodebuild -version`. `launchctl` installs its service. If you turn on
  per-build confirmation, `osascript` shows a local dialog.
- **Long-running background processes.** The helper runs as you, as a user LaunchAgent. The dispatcher starts each
  build as a system LaunchDaemon running as `bh-builder`; git materialization and Xcode run under separate sandbox
  profiles. Repository-controlled code runs on the Mac only as build scripts and Swift compiler plugins (macros),
  both confined by the same build sandbox.
- **Network calls.** The helper opens one outbound WSS connection to your gateway. The runner fetches the granted
  repository from its public HTTPS host. The Mac needs no inbound listener.
- **Stored credentials.** The helper stores its device Ed25519 private key, pairing, grants and job state under
  `~/Library/Application Support/BorrowedHands`, and logs under `~/Library/Logs/BorrowedHands`.
- **Screen Recording.** Not requested. Setup skips capture and does not ask for Screen Recording in this version.

### Listener limits

Frames up to `max_frame_bytes` (default and maximum 16 MiB); before a connection authenticates, its first complete
frame must be at most 16 KiB and it has 10 seconds. There are at most 4 unauthenticated connections per client
address and 8 in total. The transport uses the full frame limit even before authentication, bounding incomplete
message payloads at 8 x 16 MiB = 128 MiB; parser copies, queued messages, decoded strings and socket buffers add
memory overhead, so this is not an RSS limit. The client address is the socket peer, or the last
`X-Forwarded-For` entry only when `trusted_proxy: railway` (`BH_TRUSTED_PROXY=railway`) says a Railway edge is in
front. When enabled, restrict ingress to that proxy. The full 16 MiB wire budget supports bounded build
diagnostics and screenshot results near the schema's 12 MiB base64 limit. The signed envelope adds base64
expansion and metadata: senders must truncate or return `frame_too_large` if the complete frame exceeds 16 MiB,
even when its inner result passes the schema.

## Limits and residual risks

Builds accept only public HTTPS repositories, immutable commits reachable from `bh/*`, and locally granted
projects/schemes/configurations. Exact destinations are `platform=macOS,arch=arm64` and
`generic/platform=iOS Simulator`, with signing disabled. Simulator support is compile-only; no boot, install, UI
or tests. Remote Swift packages are unsupported. One build runs with up to three queued per Mac.

A stolen server signing key can request anything existing Mac grants allow; code running as the Mac user can
read its key and alter grants. The hosting edge can observe result contents and disrupt service. Builds can read
some host metadata. Diagnostics are untrusted compiler text; scrubbing is hygiene, not a security boundary.
Recorded completed compiles reported `cleanup_ok=true` and `wipe_ok=false`, including an unlink-protected empty
trustd directory; full persistent-state removal is not established. See [protocol residual risks](../docs/PROTOCOL.md#8-residual-risks-accepted-for-01).
No independent security review of 0.1 has been completed; this README makes no review or release-approval claim.

## Settings

`plugins.entries.borrowed-hands.settings` in `config.yaml`: `listen: {enabled, host, port}`, `public_url`
(`wss://<your host>/bh/v1/ws`), `owners` (`platform:user_id` list), `max_frame_bytes`, `trusted_proxy`
(`railway` or unset). Environment fallbacks: `BH_LISTEN`, `BH_PORT`, `BH_PUBLIC_URL`, `RAILWAY_PUBLIC_DOMAIN`,
`BH_OWNERS`, `BH_TRUSTED_PROXY`.

## Uninstall

`hermes plugins remove borrowed-hands`, then delete this plugin's directory under `$HERMES_HOME/plugin-data/`.
Paired Macs are refused from then on.
