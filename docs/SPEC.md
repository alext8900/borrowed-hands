# Borrowed Hands 0.1 implementation spec

Read with `docs/PROTOCOL.md` (wire format, Frozen v1) and `docs/SANDBOX.md` (the build sandbox: `sandbox/bh-dispatch`,
`sandbox/bh-runner`, `builder.sb`, `git.sb`, setup/teardown, 112/112 tests and 18/18 mutants on the build Mac). This spec integrates
with the sandbox's JSON contracts; it does not re-specify them. Hermes citations are against the v0.22.7 source
(`b200f5bc6`). Code and test results beat this page; if they disagree, fix this page and say so in the commit.

**0.1 priorities:** extremely simple install and pairing, with clear permission guidance and a
working self-test; a fully working Hermes -> Mac -> Xcode -> structured diagnostics -> Hermes loop; security enforced
locally on the Mac, never dependent on the model or on Hermes approval settings; no feature expansion before Nov 11.

## 1. Architecture

```
 Chat (Telegram etc.)            Railway: Hermes v0.22.7 gateway (pinned image digest)
 owner types /hands pair  --->   +-------------------------------------------------------+
                                 | borrowed-hands plugin                                  |
 agent calls hands_* tools --->  |  tools.py  hooks.py  commands.py  prompt.py            |
                                 |      | run_coroutine_threadsafe                        |
                                 |  listener thread: own asyncio loop, aiohttp :8790      |
                                 |      | store.py (SQLite in plugin-data)                |
                                 +------|------------------------------------------------+
                                        | wss://<domain>/bh/v1/ws  (TLS ends at the Railway edge;
                                        |  frames are Ed25519-signed end to end)
                                        v  (the Mac dials out)
 Mac, owner's user:  bh daemon (LaunchAgent; Terminal `bh run` as fallback)  -- grants in config.toml are the authority
                      |  system.info runs as the owner
                      |  screen.capture: asks the capture companion (user session, holds the TCC grant; 9.5)
                      |  xcode.build: sudo -n /opt/borrowed-hands/bin/bh-dispatch --timeout N < job.json   (BUILT)
                      v
 root: bh-dispatch -> launchctl bootstrap system com.borrowedhands.runner (LaunchDaemon, SessionCreate)
 uid 470 bh-builder: bh-runner -> git under git.sb (443 only) -> xcodebuild + xcresulttool under builder.sb
```

Invariants (each has a test in section 13):
1. Nothing reaches the Mac except a contract signed by the pinned server key, inside a frame signed by the server key.
2. The helper fetches only from the `remote_url` in its own config. Hermes sends a fingerprint, never a URL to fetch.
3. Hermes can never create or widen a grant. There is no protocol message for it. Pairing grants nothing.
4. Builds run as `bh-builder` in the system launchd domain (never from the owner's session, never as the owner, never
   as root), inside `builder.sb`, with code signing off (SANDBOX.md explains why the session matters).
5. Tool results never contain helper-side absolute paths; diagnostics are untrusted, schema- and length-bounded data.
6. A failure in the plugin never crashes the gateway (bind errors, missing aiohttp, DB errors all degrade to `helper_unavailable`).
7. No security decision depends on a model's output or on Hermes approval settings.

## 2. Repository layout

```
borrowed-hands/            Hermes plugin; this is the catalog `subdir`, so it is the only tree the install scanner reads
  plugin.yaml
  __init__.py              register(ctx): wiring only
  bhproto.py               byte-identical copy of protocol/bhproto.py (test enforces this)
  config.py                settings + env fallbacks
  gate.py                  gateway detection
  listener.py              thread-owned loop, aiohttp app, per-connection session state machine
  pairing.py               codes, pair exchange, owner notice
  jobs.py                  contract minting, submit, wait, cancel, reconcile
  store.py                 SQLite schema + migrations + all queries
  match.py                 requirement language (PROTOCOL 6.2)
  tools.py                 5 tool schemas + handlers
  hooks.py                 pre_tool_call + approval tickets
  commands.py              /hands
  prompt.py                system prompt section text
  schemas/                 copy of protocol/schemas (validated at runtime with a tiny built-in checker, no jsonschema dependency)
  README.md                disclosures (rule 13)
helper/                    `borrowed-hands-helper` (uv project, package borrowed_hands_helper, entry point `bh`);
                           gitmat.py and xcresult.py are merged (see 9.1 for what each is still used for)
sandbox/                   bh-dispatch (root), bh-runner (uid 470), builder.sb, git.sb, setup/teardown (SANDBOX.md)
protocol/                  schemas/*.json, gen_schemas.py, test-vectors/, gen_test_vectors.py, bhproto.py
deploy/railway/            Dockerfile, bh-start.sh, config.yaml (railway.json is at the repo root)
tests/, scripts/           sandbox tests and probes (built), plus plugin/, protocol/, deploy/ (outside
                           borrowed-hands/ so hostile fixtures are not in the scanned tree)
docs/
```

Everything security-sensitive on the Mac (helper, dispatcher, runner, profiles, setup) lives in this one repo,
visible, disclosed and reviewable, even though the catalog scanner only reads `borrowed-hands/`.

`bhproto.py` API (both sides): `canonical(obj) -> bytes`, `b64u(bytes) -> str`, `unb64u(str) -> bytes`,
`sign(priv, context: bytes, data: bytes) -> str`, `verify(pub_raw, context, data, sig_b64u) -> bool`,
`key_id(pub_raw) -> str`, `repo_fingerprint(url) -> str`, `normalize_repo_url(url) -> str`, `check_expiry(payload, now_ms)`,
`parse_pairing(s, allow_insecure_local=False) -> (endpoint, code_bytes, server_pub_raw)`, `wrap(payload, priv|None) -> str`,
`unwrap(text, peer_pub|None) -> (payload_dict, raw_bytes)`, `load_strict(bytes)` (rejects duplicate keys and floats).
It uses only the stdlib and `cryptography.hazmat.primitives.asymmetric.ed25519`. Its tests load
`protocol/test-vectors/vectors.json` and must reproduce every vector.

## 3. plugin.yaml

```yaml
name: borrowed-hands
version: 0.1.0
description: "Borrow narrowly granted capabilities (Xcode builds, screen capture, system info) from a Mac you paired. The Mac dials out and decides."
author: Alex Thompson
requires_hermes: ">=0.22.7"
provides_tools: [hands_find, hands_system_info, hands_xcode_build, hands_job, hands_screen_capture]
provides_hooks: [pre_tool_call]
python_dependencies: ["aiohttp>=3.9,<4", "cryptography>=42,<51"]
config_schema: {}   # filled from section 8.1
license: MIT
homepage: https://github.com/alext8900/borrowed-hands
```

`python_dependencies` are validated and surfaced only, never installed (`hermes_cli/plugins_manifest.py:399-402`).
Both are present in the official image. On a bare install without aiohttp, the listener logs
`borrowed-hands: aiohttp missing; listener disabled` and every tool returns `helper_unavailable`
(`detail.reason: "listener_unavailable"`).

## 4. register(ctx) wiring

Order inside `register(ctx)` (the whole function must return in under 3 s; the loader deadline is 10 s,
`plugins_loader.py:44`):
1. `cfg = config.load(ctx)` (reads `ctx.get_config`, `plugins.py:266`, with env fallbacks).
2. `data_dir = ctx.state.data_dir` (`plugins_state.py:141`), resolved **once** here, `mkdir(mode=0o700)`. Pass the path everywhere. Never call `get_hermes_home()` from the listener thread.
3. `store = Store(data_dir / "bh.sqlite3")` (migrations; on error, log it and continue with tools in degraded mode).
4. `ctx.register_tool(...)` x5 (`plugins.py:463`), toolset `borrowed_hands`. Always registered, whether or not the listener runs: static tools, because runtime-registered tools proved hazardous: a stale call was fuzzy-repaired to a sibling tool and ran.
5. `ctx.register_hook("pre_tool_call", hooks.pre_tool_call)` (`plugins.py:935`).
6. `ctx.register_command("hands", commands.handle, description="Borrowed Hands: pair, status, unpair, rotate-key", args_hint="pair|status|unpair <helper>|rotate-key")` (`plugins.py:649`).
7. `ctx.register_system_prompt_section("borrowed-hands", prompt.TEXT)` (`plugins.py:959`; one position `after_memory`, 4000 chars max per section, 8000 total, `plugins_dispatch.py:98-103`).
8. Listener start (section 6) if the gate passes. `ctx.on_unload(listener.stop)` (`plugins.py:416`).

Do not use `ctx.spawn_task` (it needs a running loop; there is none in register, `plugins.py:425-435`, verified).

## 5. Tools

All handlers are sync, `def handler(args, session_id="", **_)` (dispatch passes `task_id`, `session_id`, `user_task`
filtered by signature, `model_tools.py:838-852`, `tools/registry.py:980-988`). All return a JSON string except a
successful `hands_screen_capture`. Common envelope:

```json
{"ok": true, "...": "..."}
{"ok": false, "error": {"code": "<PROTOCOL 2.4 enum>", "message": "...", "detail": {}}}
```

When queued owner notices exist, add `"notices": ["Paired: ..."]` and mark them delivered.

Helper resolution (`helper_id` omitted): if exactly one online, non-revoked helper has the needed capability, use
it. Otherwise return `invalid_arguments` with `detail.candidates: [{helper_id, name, online}]`. `helper_id` also
accepts the helper's exact `name`.

### 5.1 hands_find

```json
{"name": "hands_find",
 "description": "Find a paired Mac that has what you need (for example Xcode) before calling other hands_* tools. Read-only; no approval needed.",
 "parameters": {"type": "object", "additionalProperties": false, "properties": {
   "requires": {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 64},
     "description": "All must hold. Forms: capability names (xcode.build, screen.capture, system.info), os=macos, arch=arm64, os_version>=15.0, xcode>=16."},
   "include_offline": {"type": "boolean", "default": false}}}}
```

Result: `{"ok": true, "helpers": [{"helper_id", "name", "online", "busy", "os", "os_version", "arch", "xcode",
"capabilities": ["xcode.build", ...], "workspaces": [{"workspace_id", "repo_fingerprint", "schemes", "configurations",
"destinations"}]}], "unmatched": [{"helper_id", "name", "unmet": ["xcode>=17"]}]}`. When nothing is paired:
`ok: true, helpers: [], "hint": "No Mac is paired. The owner can run /hands pair."`.

### 5.2 hands_system_info

Parameters: `{"helper_id": {"type": "string", "maxLength": 64}}`. No approval hook: the data is limited
to non-sensitive capability discovery (PROTOCOL 4.4) and only flows to the authenticated server. Contract action
`system.info`, inline wait at most 30 s. Result: `{"ok": true, "helper_id", "info": {PROTOCOL 4.4 system info}}`.

### 5.3 hands_xcode_build

```json
{"name": "hands_xcode_build",
 "description": "Build the user's Xcode project on a paired Mac. Every argument is optional: pass only what the user said (\"build my demo project\" needs none). Omitted values come from the Mac's grant when it allows exactly one choice, and an omitted commit is the latest commit on the repository's bh/* branch. Never ask the user for a commit SHA. If something is ambiguous the result has reason needs_input with options: ask the user, then call again with that field. The Mac only builds what its owner granted. Waits up to wait_seconds, then returns a job_id for hands_job.",
 "parameters": {"type": "object", "additionalProperties": false,
   "properties": {
     "helper_id": {"type": "string", "maxLength": 64, "description": "helper_id or exact name; omit when exactly one Mac fits"},
     "workspace_id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,63}$", "description": "Granted workspace (see hands_find); omit when the Mac grants one"},
     "repo_url": {"type": "string", "maxLength": 300, "description": "https URL of the granted repository; omit to reuse the one remembered for this workspace. Checked against the Mac's own remote, never used to widen a grant"},
     "commit": {"type": "string", "pattern": "^[0-9a-f]{40}$", "description": "Exact 40-hex SHA, only if the user gave one. Do not ask the user for it: omit it to build the latest commit on bh/*"},
     "branch": {"type": "string", "maxLength": 200, "pattern": "^bh/[A-Za-z0-9._/-]+$", "description": "bh/<name> branch to build the latest commit of, only when the user named one or the result asked for it"},
     "scheme": {"type": "string", "maxLength": 64, "description": "Omit when the grant has one scheme"},
     "configuration": {"type": "string", "maxLength": 64, "description": "Omit for the grant's only configuration, or Debug"},
     "destination": {"type": "string", "maxLength": 200, "description": "One of the workspace's granted destinations; omit when the grant has one"},
     "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 300, "default": 240}}}}
```

Defaults (`tools.fill_build`): the Mac is the only online Mac offering `xcode.build` (else `needs_input` with the
candidates); the workspace, scheme, configuration and destination are filled from that Mac's advertised grant only
when it allows exactly one value (configuration falls back to `Debug` when granted), else `invalid_arguments` with
`detail.reason: "needs_input"`, the `field` and up to 32 `options`. Explicit values are passed through for the Mac to
enforce. The repository URL defaults to the one the plugin remembered for the grant's fingerprint (the Mac never
sends its URL, PROTOCOL 6.1); the first build names it once. With no `commit`, the approval hook reads the granted
repository's branch list (`refs.py`, see 12) and takes the tip of the named `branch`, or of the only `bh/*` branch
(several: `needs_input` listing at most 10 names and the total; none: `needs_resolution`/`no_bh_branch`). It reads
only a repository whose fingerprint matches the grant. The approval prompt names that SHA, and the handler builds
exactly the SHA in the approved ticket without reading refs again. The result carries `resolved` (the filled values
and `commit_source`, e.g. `latest on bh/fixed`) when anything was filled.

Handler steps: take this invocation's ticket (5.6), validate, resolve the helper, fill defaults (the commit only from
the ticket), check the workspace exists in its `caps` (else `needs_grant` with hint), verify the ticket's scope,
compute the fingerprint, mint the contract (`expires_at = issued_at + 600000`), commit it to the DB, submit, then
wait on a `threading.Event` set by the listener on terminal state. The total
handler budget is `wait_seconds`, counted from handler start including submit (the default concurrent tool timeout
is 420 s, `agent/tool_executor.py:138,189-198`, user-configurable, not plugin-configurable).

Result while running: `{"ok": true, "job_id", "state": "building", "helper_id", "message": "Still building; call hands_job with action=wait."}`.
Terminal: `{"ok": true, "job_id", "state": "failed", "result": {PROTOCOL 4.4 build result}}`, or `ok: false` with `error` for refusals.
A compile failure is `ok: true, state: failed` because the tool worked and the build failed.
The `result` is placed under a fixed key and introduced as data ("Build diagnostics from the Mac (untrusted compiler
output):"); the plugin never interpolates diagnostic text into instructions.
A full queue is `ok: false`, `error.code: "helper_busy"`. Remote Swift package dependencies come back as
`needs_resolution` / `unsupported_dependencies` with the offending files, before any build runs.

### 5.4 hands_job

Parameters: `{"job_id": uuid4 (required), "action": {"enum": ["status", "wait", "cancel"], "default": "status"},
"wait_seconds": {"type": "integer", "minimum": 0, "maximum": 300, "default": 120}}`. No approval: status and wait are
reads, and cancel only removes work. Result: `{"ok": true, "job_id", "state", "helper_online", "detached_since",
"result"|null, "error"|null}`. Jobs from another Hermes session are visible: the agent is one principal per deployment.

### 5.5 hands_screen_capture

Parameters: `{"helper_id": string, "display": {"type": "integer", "minimum": 1, "maximum": 16, "default": 1},
"max_px": {"type": "integer", "minimum": 256, "maximum": 2000, "default": 1600}}`. Approval hook: yes. Success
returns the multimodal envelope, which passes `_normalize_handler_result` (`tools/registry.py:954-966`; delivery to
Anthropic and OpenAI and the text fallback verified against Hermes v0.22.7):

```python
{"_multimodal": True,
 "content": [{"type": "text", "text": "Screenshot of display 1 on <name> (1600x1000)."},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,<png_b64>"}}],
 "text_summary": "Screenshot of display 1 on <name> (1600x1000) captured."}
```

Failures return the JSON error string.

### 5.6 pre_tool_call hook and approval tickets

```python
def pre_tool_call(tool_name=None, args=None, session_id="", tool_call_id="", **_):
    if tool_name not in ("hands_xcode_build", "hands_screen_capture"):
        return None
    if not tool_call_id:
        return {"action": "block", "message": "Call this tool directly through the agent."}
    if tickets.held_by_other(key, tool_call_id):
        return CONFLICT                                   # an identical call is still waiting for approval
    scope = resolve(tool_name, args)                      # may read the branch list; Refusal -> refusal ticket
    tickets.issue(key, scope, tool_call_id)               # TTL 15 min (refusals 60 s), max 256 live
    return {"action": "approve", "message": describe(scope), "rule_key": rule_key(tool_name, scope)}
```

- `describe` example: `Build demo (scheme Demo, Debug, macOS arm64) at commit 3449717 (latest on bh/fixed) on Macbook Pro M5 Max`. The prompt shows the first 7 characters of the SHA the contract binds in full. Model-supplied values that are not plain granted-style names are shown quoted, shortened and stripped of control characters and markup. Never include URLs or paths.
- `rule_key`: `<tool>:<sha256(canonical(scope))[:32]>`, where scope includes the tool and helper plus workspace, scheme, configuration and destination for a build, or display for a capture. Hashing structured scope avoids delimiter collisions. A persistent approval ("session"/"always") therefore covers exactly that helper + workspace + scheme + configuration + destination, never the SHA, and never a broader workspace or destination. Each job still gets its own signed contract binding the SHA, the exact params, the device, a unique job id and an expiry, and the device grant is enforced independently. The default key would be the bare tool name (`plugins.py:2005`), so one "session" answer would otherwise cover every future build (verified against Hermes v0.22.7).
- `args_hash` hashes the tool and normalized execution fields from the call (excluding wait time). The handler pops the ticket matching `(session_id, tool_name, args_hash)` and checks its resolved scope. If there is none, it returns `denied` (`detail.reason: "approval_not_observed"`). This catches `ctx.dispatch_tool` / `registry.dispatch` callers that skip hooks (verified against Hermes v0.22.7). Under `--yolo`, `approvals.mode: off` or `cron_mode: approve` the hook still runs and Hermes auto-approves. That is the operator's documented choice, and the device grant still applies. This is not a security boundary (PROTOCOL preamble).
- `approval_ref` in the contract is `hermes:<ticket uuid>`. Hermes gives the handler no approval id, so the plugin mints one.
- Tickets also bind every resolved execution field, including helper, defaults and a resolved commit. The handler
  must consume with the helper and resolved scope it will put into the contract (`helper_id=...`, `scope=...`);
  changed defaults refuse the ticket. A ticket belongs to the `tool_call_id` that created it: the handler takes it
  only with that id, which it reads from Hermes's dispatch context (`tools.approval_context`, read only; v0.22.7
  passes no id to handlers, `model_tools._execute_tool`); with no id, or another call's id, it takes nothing and
  builds nothing. While a live ticket exists for a key, an identical call from another invocation is refused
  (`CONFLICT`), never allowed to replace it. At 256 live tickets a new key is refused (`BUSY`) instead of evicting
  one. `post_tool_call` removes the tickets of any non-`ok` invocation by session and invocation id, even when
  another hook rewrote the arguments; an id-less post hook removes nothing. Each hook run registers first, with
  a token, under its invocation `(session, tool_call_id, turn_id)`. If Hermes abandons a hook worker at
  `plugins.hook_callback_timeout` and runs its post hook, that run's token is marked dead and its late `issue()`
  is refused, whether the worker was resolving or had not yet registered: a post hook that finds no run of the
  invocation started or completed leaves a pending mark (at most 1024, at most 120 s) that kills the next run to
  register with that identity. Marks are per run, so a provider's deterministic tool_call_id reused in a later
  turn is unaffected. Residual: with an empty `turn_id` and a deterministic id whose earlier run completed within
  the last 120 s, a timeout before registration is not distinguished from that completed run, so its late ticket
  can be stored; it fails closed (the ticket can be taken only by that dead invocation's id, so the identical
  call is refused until it expires). Calls without an id are blocked before
  minting a ticket. Verified on v0.22.7: `model_tools.py:660-666,793-798,954-957` passes the id (empty if absent)
  through pre/post hooks, with rewritten args reaching post hooks; normal agent calls carry their id
  (`agent/tool_executor.py:298-305`).
- The hook never raises. Its only I/O beyond cached `caps` is the bounded branch-list read for a build with no
  commit (12) and the stored repository URL lookup. A raise or overrun fails closed (verified against Hermes
  v0.22.7), which is acceptable but would look like a denial. A call that cannot be
  resolved gets no prompt: the hook leaves a refusal ticket and the handler returns it.

## 6. Listener lifecycle

- **Gate** (`gate.py`): start only if (a) `listen.enabled` is true (setting, else env `BH_LISTEN=1`; `BH_LISTEN=0` forces off), and (b) the process is the gateway: some adjacent `argv` basenames equal `["gateway", "run"]` **and** `"gateway.run" in sys.modules`. `register()` also runs in `hermes plugins validate`, doctor and CLI processes; this check was verified on Railway. `BH_LISTEN=1` enables (a) but does not bypass (b).
- **Singleton:** a module global `_LISTENER`. If it is set and `pid == os.getpid()`, do not start a second one. reload-plugins calls on_unload first, then register again; this restarts cleanly (verified).
- **Start:** `threading.Thread(target=run, name="bh-listener", daemon=True)`. `run` creates `asyncio.new_event_loop()`, builds the aiohttp app (`client_max_size=16 MiB`, routes `/bh/v1/ws` and `/bh/v1/healthz`), `TCPSite(host, port)`, then `run_forever`. `register` waits for a ready event for at most 3 s and never blocks longer.
- **Bind failure is non-fatal:** catch everything in `run`, record `listener.error` (for example `OSError: [Errno 98] address in use`), log one line at WARNING, and return. Tools then answer `helper_unavailable` (`detail.reason: "listener_not_running"`, with `detail.error`). `/hands status` shows it. Multiple profiles in one process: the second bind fails and degrades the same way.
- **Calls from tools:** `asyncio.run_coroutine_threadsafe(coro, listener.loop).result(timeout)`. Never block the listener loop with SQLite writes longer than about 50 ms; WAL mode, one connection per thread.
- **Stop (on_unload):** close every WS with 1001, `runner.cleanup()`, `loop.stop()`, join for 5 s. on_unload is **not** called on SIGTERM (proven). The daemon thread dies with the process. Every state change is already committed, and helpers see 1006 and reconnect (4.5). The earlier probe transport reconnected in 0.2 s after redeploy and 8.9 s after restart; this is not integrated-helper acceptance evidence.
- **Logging:** logger `borrowed_hands`. One INFO line per connect, auth, pair, submit and terminal state, with helper_id[:8] and job_id[:8]. Never log codes, keys, signatures or contract bytes.

## 7. State storage

`ctx.state` (`hermes_cli/plugins_state.py:134-195`) is a whole-file JSON key/value store: every `set` rewrites the
file under a lock, the quota is 10 MiB, and there is no delete. That fits neither the job table nor the replay table.
So we use **only its `data_dir`** (`$HERMES_HOME/plugin-data/<namespace>/`) and keep our own files there:

| File | Content | Mode |
|---|---|---|
| `bh.sqlite3` (+ wal/shm) | tables below | 0600 |
| `server_ed25519.key` | 32-byte raw private seed, base64url, one line | 0600 |
| `server_ed25519.key.next` | only during a rotation | 0600 |

Tables (`store.py` has the exact DDL):
- `meta(k PRIMARY KEY, v)`: schema_version.
- `helpers(helper_id PK, device_pubkey, name, paired_at, paired_by, revoked_at, last_seen, caps_json)`.
- `pair_codes(code_hash PK, created_at, expires_at, used_at, issued_to)`.
- `jobs(job_id PK, helper_id, action, contract BLOB, sig, contract_hash, nonce UNIQUE, state, stale, detached_since, pending_send, session_id, created_at, updated_at, result_json, error_json)`.
- `notices(id PK, created_at, text, delivered_at)`.
- `audit(id PK, at, event, helper_id, job_id, detail_json)`: append-only, pruned at 30 days.

On Railway, `$HERMES_HOME` must be a volume (section 10), or keys and pairings vanish on redeploy (proven: "No volume yet").

## 8. Configuration

### 8.1 Plugin settings (`plugins.entries.borrowed-hands.settings` in config.yaml)

| Key | Type | Default | Env fallback | Notes |
|---|---|---|---|---|
| `listen.enabled` | bool | false | `BH_LISTEN` | |
| `listen.host` | str | `0.0.0.0` | | |
| `listen.port` | int | 8790 | `BH_PORT` | |
| `public_url` | str | `wss://$RAILWAY_PUBLIC_DOMAIN/bh/v1/ws` if that env var is set | `BH_PUBLIC_URL` | Goes into the pairing string and is checked in `auth.endpoint`. Required to pair. |
| `owners` | list[str] | [] | `BH_OWNERS` (comma-separated) | `platform:user_id`, for example `telegram:123456789`. Empty means nobody can pair over chat; the local CLI only. |
| `max_frame_bytes` | int | 16777216 | | |
| `trusted_proxy` | str or unset | unset | `BH_TRUSTED_PROXY` | Only `railway` is supported; trust the rightmost edge-appended XFF entry. Restrict ingress to the proxy when enabled. |

Pre-auth transport limits: at most 8 connections globally and 4 per source IP; 10 seconds to handshake; first
complete message at most 16 KiB before decoding. The transport parser's per-message ceiling stays at
`max_frame_bytes` (up to 16 MiB) for screenshot compatibility, so incomplete unauthenticated message payloads
are bounded at 128 MiB in aggregate. Parser copies, queued messages, decoded strings and socket buffers add
overhead; this is not an RSS limit. Build diagnostics fit within 1 MiB. Screenshot `png_b64` has a 12 MiB schema
ceiling, but its signed envelope expands it again: senders must obey the 16 MiB complete-wire-frame ceiling too.

None of these are secrets (rule 11). The plugin reads no secret at all.

### 8.2 /hands command

Owner gate: the handler takes `source` (`gateway/run_inbound_hooks.py:285-291` passes `source`, `session_id`,
`session_key`, `platform`). Allow if `source is None` (local CLI session), or if `f"{source.platform.value}:{source.user_id}"`
is in `owners`. `pair` additionally requires `source.chat_type == "dm"`. Refusal text: `Only the Borrowed Hands owner can do that.`

| Subcommand | Effect |
|---|---|
| `pair` | New code (PROTOCOL 3.2). `ctx.post_message(text)` (`plugins.py:607`) with the string in a code block plus "valid 10 minutes, single use; run on the Mac: `bh pair '<string>'`". Keep the handle for the edit (PROTOCOL 3.3). The command returns None if the post succeeded, else the same text as its reply. |
| `status` | Listener state and public_url, then for each helper: name, id[:8], online or last_seen, capabilities, busy; active jobs; pending notices. |
| `unpair <name or id>` | PROTOCOL 3.5. |
| `rotate-key` | PROTOCOL 3.6. |
| (none) or `help` | Usage. |

### 8.3 Helper config `~/Library/Application Support/BorrowedHands/config.toml` (dir 0700, file 0600)

```toml
[server]                      # written by `bh pair`; never edit by hand
url = "wss://hermes.example.com/bh/v1/ws"
pubkey = "<43 b64u>"
key_id = "<16 hex>"
helper_id = "<uuid4>"

[device]
name = "Example Mac"          # device key: device_ed25519.key beside this file (PROTOCOL 3.4)

[policy]
build_timeout_s = 900         # bh-dispatch --timeout (dispatcher deadline = this + 30 s)
disk_budget_mb = 20480        # bh-dispatch --disk-budget-mb (SANDBOX.md MEDIUM 5)
mem_budget_mb = 6144          # bh-dispatch --mem-budget-mb

[capabilities]                # ALL false after pairing; `bh caps enable` flips them
system_info = false
screen_capture = false
xcode_build = false

[[workspaces]]                # one grant; written by `bh grant add`
id = "demo"
remote_url = "https://github.com/example/demo.git"        # public https only
project = "Demo.xcodeproj"    # or "Demo.xcworkspace"; relative to the repo root
schemes = ["Demo"]
configurations = ["Debug"]
destinations = ["platform=macOS,arch=arm64", "generic/platform=iOS Simulator"]   # the only two accepted
confirm = false               # per-build dialog on this Mac
expires_at = ""               # RFC 3339 or empty
```

The helper reloads on file mtime change (poll every 2 s), re-sends `caps`, and cancels queued or running jobs whose
grant disappeared. An invalid config keeps the previous one, logs the error, and `bh status` shows it. The queue size
(3) and one active build are constants, not settings.

### 8.4 Helper CLI (`bh`)

Simplicity is a P0 requirement: `bh setup` is the one guided path, and every error says what to do next.

| Command | Does | Exit codes |
|---|---|---|
| `bh setup` | Guided first run, in order: check Xcode; install the production sandbox (`sudo bash <share>/sandbox/setup-bh-builder.sh`, **never** `--with-test-profiles`) and the pinned sudoers rule (9.2), showing each sudo command before running it; `bh pair` (prompts for the string); offer `bh grant add` for one workspace with plain-language prompts; offer screen capture (9.5) with the System Settings path; `bh service install`; finish with `bh selftest`. Re-running it is safe. | 0 ok; non-zero with the failing step's message |
| `bh selftest` | The working self-test. Checks: Python and package versions; device key and config permissions (PROTOCOL 3.4); Xcode (`xcode-select -p`, `xcodebuild -version`); the **production** sandbox install (root-owned `/opt/borrowed-hands/bin/bh-dispatch`, no `TEST_MODE`, empty `libexec/profiles/`); `sudo -n` reaches the dispatcher; server reachable and the server key proof valid; each enabled capability usable. With `--build <workspace>`, it also runs one real local build of the tip of that workspace's first `bh/*` branch through the full dispatch path, and prints the structured diagnostics. Prints PASS/FAIL per line with the fix for each FAIL. | 0 all PASS; 1 any FAIL |
| `bh pair '<bh1:...>' [--name N] [--insecure-local]` | PROTOCOL 3.3, writes `[server]`; enables nothing | 0 ok; 2 rejected or expired code ("pairing code rejected; run /hands pair again"); 3 server key proof failed; 4 cannot connect ("cannot reach <host>: <err>") |
| `bh run` | Foreground daemon (what the LaunchAgent runs; also the Terminal fallback for screen capture) | 0 on SIGTERM; 3 unpaired |
| `bh service install` / `uninstall` | Writes or removes `~/Library/LaunchAgents/dev.borrowed-hands.helper.plist` (gui domain of the owner: `RunAtLoad`, `KeepAlive` on non-zero exit only, logs to `~/Library/Logs/BorrowedHands/`), `launchctl bootstrap gui/$UID` / `bootout`, touching only that label | |
| `bh status` | Paired? connected? server, helper_id, capabilities, grants, queue, last error | |
| `bh grant add xcode.build --workspace ID --repo URL --project P --scheme S [--scheme ...] [--configuration C ...] [--destination D ...] [--confirm] [--expires 7d]` | Appends or replaces a `[[workspaces]]` entry; refuses non-https, credentials in the URL, or a destination the dispatcher does not accept | 2 on invalid input |
| `bh grant list` / `bh grant revoke ID` | | |
| `bh caps enable|disable system.info|screen.capture|xcode.build` | `enable xcode.build` requires the production sandbox install; `enable screen.capture` runs 9.5's permission check and prints the exact System Settings path on failure | |
| `bh jobs [--all]` / `bh cancel JOB_ID` | Local job table / cancel path | |
| `bh unpair` | PROTOCOL 3.5 | |
| `bh rotate-key` | PROTOCOL 3.6 | |
| `bh uninstall` | Service uninstall, unpair, `sudo bash <share>/sandbox/teardown-bh-builder.sh`, remove the sudoers rule, remove local state | |

`<share>/sandbox` is documentation shorthand for the helper's computed shared sandbox asset directory; diagnostics
print the real full path. The helper verifies the installed
`/opt/borrowed-hands` files are root-owned before trusting them, and never runs setup from a writable location as
root without showing the command first.

Helper process model: one asyncio loop (connection, heartbeat, job queue), at most one `bh-dispatch` child, and
capture/info on a separate single-slot executor. A lock file `~/Library/Application Support/BorrowedHands/bh.lock`
(`fcntl.flock`) ensures a single daemon.

## 9. Building on the Mac (integrating the built sandbox)

The sandbox is built, went through three security review fix rounds, and is documented in `docs/SANDBOX.md`. This section only says how
the helper uses it and what is still missing. Do not re-implement or re-specify its validators, plist, budgets,
sweeps or profiles.

### 9.1 Helper to dispatcher

- **Invocation:** `sudo -n /opt/borrowed-hands/bin/bh-dispatch --timeout <build_timeout_s> --disk-budget-mb <n> --mem-budget-mb <n> < job.json`, from the helper's own uid, with a clean environment. Exactly one in flight (the dispatcher also holds a lock and returns `busy`).
- **job.json** (SANDBOX.md "job.json"): `{"v":1,"job_id","remote","commit","project","scheme","configuration","destination","allowed_refs":"refs/heads/bh/*"}`, built from the **verified contract + local config**: `remote` and `project` come from `[[workspaces]]`, never from Hermes; `job_id` is the contract's; `allowed_refs` is the fixed literal shown here.
- **Output:** exactly one JSON line on stdout (SANDBOX.md "Result"). The helper parses it with the strict loader, re-validates it against `job-result.json`'s build result shape, applies the caps (50 errors/warnings, 500 chars each, 4000-char log tail), and re-runs `xcresult.py`'s scrubber on every string as hygiene (it is not a boundary).
- **Status mapping:**

| bh-dispatch `status` | job state | error code (`detail.reason`) |
|---|---|---|
| `succeeded` | succeeded | none |
| `failed` | failed | none (diagnostics in `result`) |
| `timeout` | failed | `timeout` |
| `cancelled` | cancelled | `cancelled` |
| `fetch_failed` | failed | `needs_resolution` (`commit_not_found`) |
| `not_reachable` | failed | `needs_resolution` (`not_on_bh_branch`) |
| `unsupported_dependencies` | failed | `needs_resolution` (`unsupported_dependencies`, with `files`) |
| `unsafe_tree` | failed | `invalid_arguments` (`unsafe_tree`): unsafe repository contents, no build started |
| `invalid_job` with a project-missing message | failed | `needs_resolution` (`project_missing`) |
| `invalid_job` (other) | failed | `internal_error` (`invalid_job`): the helper's own checks should have caught it, so log it as a bug |
| `disk_budget` / `memory_budget` / `budget_check_failed` | failed | `internal_error` (same reason) |
| `busy` | stays queued, retried after 5 s | none (should not happen with one in flight) |
| `usage` / `error` / unparsable / no line | failed | `internal_error` (`dispatch_error`) |

- **Phases:** none are reported mid-run, so the helper shows `building` from dispatch start (PROTOCOL 4.3).
- **Cancel:** SIGTERM to the `sudo` child (PROTOCOL 4.6). **Proven:** from the owner's uid it reaches the root dispatcher (child in the caller's process group or its own session) and yields `cancelled` with no uid-470 leftovers. For a helper that lost its child (e.g. after a restart), `sudo -n bh-dispatch --cancel` with the job id on stdin signals the in-flight dispatcher for exactly that job (`cancel_sent`, or `not_running` if that job isn't the one running; both exit 0); on helper start, `sudo -n bh-dispatch --sweep-only` kills every uid-470 process (PROTOCOL 4.5).
- **Job dirs:** the dispatcher deletes the build dirs; `logs/`, `job.json`, `result.json` and `runner.err` remain under `/Users/Shared/.bh-builder-home/jobs/<id>` (uid 470, unreadable to the owner). The pre-job sweep and a retention rule (keep the last 20) are implemented so they do not accumulate.
- **What the helper's merged `gitmat.py` is for now:** nothing at run time. Materialization happens only in the runner. Its hardening and its `NotReachableFromAllowedRefs` logic are the reference that `bh-runner` ports (stdlib only), and its tests were ported with it.

### 9.2 sudoers (pinned; installed by `bh setup`, not by the sandbox setup)

`/etc/sudoers.d/borrowed-hands`, 0440, validated with `visudo -cf` before moving into place:

```
#<owner uid> ALL=(root) NOPASSWD: /opt/borrowed-hands/bin/bh-dispatch --timeout 300 --disk-budget-mb <D> --mem-budget-mb <M>
#<owner uid> ALL=(root) NOPASSWD: /opt/borrowed-hands/bin/bh-dispatch --timeout 900 --disk-budget-mb <D> --mem-budget-mb <M>
#<owner uid> ALL=(root) NOPASSWD: /opt/borrowed-hands/bin/bh-dispatch --timeout 1800 --disk-budget-mb <D> --mem-budget-mb <M>
#<owner uid> ALL=(root) NOPASSWD: /opt/borrowed-hands/bin/bh-dispatch --timeout 3600 --disk-budget-mb <D> --mem-budget-mb <M>
#<owner uid> ALL=(root) NOPASSWD: /opt/borrowed-hands/bin/bh-dispatch --sweep-only
#<owner uid> ALL=(root) NOPASSWD: /opt/borrowed-hands/bin/bh-dispatch --cancel
```

Exact argv, no globs (sudo's `*` also matches spaces). `<D>`/`<M>` are this Mac's `[policy]` budgets written
literally by `bh setup` (`caps.dispatch_argv`). The production dispatcher (no TEST_MODE) independently accepts
only these shapes (SANDBOX.md gap 8): `--timeout` in {300, 900, 1800, 3600} followed by exactly
`--disk-budget-mb D --mem-budget-mb M` (decimal, no leading zero, within the runner's maximums), or exactly
`--sweep-only`, or exactly `--cancel` (job id on stdin). Nothing grants the owner a shell as bh-builder or root.

### 9.3 Sandbox additions for 0.1 (implemented)

1. **`bh/*` ancestry:** a new job key `"allowed_refs": "refs/heads/bh/*"` (exact value) in both validators. The runner fetches those refs with `--filter=blob:none` under `git.sb` and requires `merge-base --is-ancestor`; new status `not_reachable`. Port from `helper/borrowed_hands_helper/gitmat.py`.
2. **Remote package detection:** after checkout and before xcodebuild, scan the checkout for `Package.resolved` files containing remote pins and for `XCRemoteSwiftPackageReference` in any `project.pbxproj`; new status `unsupported_dependencies` with up to 20 relative paths in `files`. No build starts.
3. **iOS Simulator:** the probe passed with builder.sb unchanged. The runner's destination allowlist (bh-runner around line 194, and the dispatcher's shared validator) accepts exactly `generic/platform=iOS Simulator` besides `platform=macOS,arch=arm64`, and the fixed build argv (around line 649) adds `-sdk iphonesimulator` for it. Compile only: no simulator boot, install or test, and no CoreSimulator mach access (keep it denied).
4. **Cancel from the owner's uid** proven end to end (9.1).
5. **Job dir retention** (9.1).
6. A `status: "succeeded"` result from the dispatcher must still carry `errors: []` and the counts, so the helper does not need special cases.

### 9.4 Production gate

- Before **any** real, remotely triggered job, the build Mac must run the **production**
  sandbox install: `sudo bash sandbox/setup-bh-builder.sh` with no flags, so there is no `/opt/borrowed-hands/TEST_MODE`
  and `libexec/profiles/` is empty. `bh selftest` checks this and the helper refuses to advertise `xcode.build`
  otherwise (PROTOCOL 6.1).
- After any test run (`--with-test-profiles`), re-run the production setup before reconnecting the helper.
- **Hostile security probes** (`scripts/probe-suite.sh`, `tests/hog-app`, `tests/hostile-app`) run hostile code and
  modify the machine: **run them only on a dedicated test Mac**, never on a machine with data you care about.

### 9.5 Screen capture

**Preferred design:** a small user-session companion, `BorrowedHands Capture.app`, with a stable app identity
(bundle id `dev.borrowed-hands.capture`, built from `capture/` Swift source on the Mac at enable time and ad-hoc
signed, so no binary is committed). It holds the TCC Screen Recording permission. It runs as a LaunchAgent in the
owner's gui domain, because a LaunchDaemon cannot reach the GUI. It listens on
`~/Library/Application Support/BorrowedHands/capture.sock` (0600 in the 0700 dir), checks the peer's uid with
`getpeereid`, and captures **only** when the helper forwards an authorized request: a contract that passed PROTOCOL
4.2. It reports `{"granted": bool}` for the caps check. The permission may stay granted, but nothing is captured
without a request. It downscales to `max_px` (CoreGraphics) and returns PNG bytes.

**Status in 0.1:** not implemented. `hands_screen_capture` is a registered placeholder, its handler is a stub,
and the helper has no capture executor. The design above is the plan for a later version.

## 10. Railway deploy recipe

`deploy/railway/Dockerfile`:

```dockerfile
FROM nousresearch/hermes-agent:v0.22.7@sha256:e6d81ae52ebf6a43efa529701405509f20054ba818f179ddd748bed4f8af584c
USER root
COPY --chmod=0755 borrowed-hands /opt/bh/plugins/borrowed-hands
COPY --chmod=0644 deploy/railway/config.yaml /opt/bh/config.default.yaml
COPY --chmod=0755 deploy/railway/bh-start.sh /opt/bh/bh-start.sh
ENV BH_PORT=8790 BH_LISTEN=1 BH_TRUSTED_PROXY=railway
EXPOSE 8790
ENTRYPOINT ["/opt/bh/bh-start.sh"]
CMD ["gateway", "run"]
```

`bh-start.sh` (volume handling verified in a Railway spike) makes sure that (1) `config.default.yaml` is copied to `$HERMES_HOME/config.yaml` **only if none exists** (the
first spike overwrote it on every boot, which would wipe user config), and (2) the plugin dir is replaced on each
boot so the built source version wins. A missing enabled list is merged atomically; malformed/empty policies are
refused unchanged. Do not start a background root log tail: let stock stage2 create logs as `hermes`, repair legacy
root-owned logs before handoff, then `exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"`. This keeps stock `/init`,
main-wrapper and the s6 gateway service intact. Read listener evidence from `agent.log` as `hermes`; see INSTALL.md.

`config.default.yaml`: `plugins: {enabled: [borrowed-hands]}` plus the messaging platform the owner uses.

Railway service settings:
- **Volume** at `/opt/data` (= `HERMES_HOME`, `Dockerfile:491`). Verified in a Railway spike: a stable install id across first boot, redeploy and restart.
- **Public networking:** a generated domain with target port 8790.
- **Healthcheck path:** `/bh/v1/healthz`, timeout 120 s, `PORT=8790`.
- **Restart policy:** `ON_FAILURE`, maximum 5 retries.
- **Variables:** model provider key; `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS` (or another platform); `BH_OWNERS=telegram:<id>`; `BH_PUBLIC_URL` optional (defaults from `RAILWAY_PUBLIC_DOMAIN`).
- **Proxy trust:** the Railway Dockerfile sets `BH_TRUSTED_PROXY=railway` for edge-only ingress. Generic plugin
  installs leave proxy trust off. The base image digest pins Hermes;
  the plugin comes from the source commit Railway actually builds.
- **One replica**, one profile.
- With a volume, the historical Railway spike saw about 18 to 19 s of 502s per redeploy. Helper backoff
  (PROTOCOL 1.2) and running-job survival are required behavior, not yet verified end to end.

## 11. Install and uninstall

### 11.1 Server (any Hermes >= 0.22.7)

```
hermes plugins install alext8900/borrowed-hands#borrowed-hands --ref <40-hex sha> --enable
# then in $HERMES_HOME/config.yaml:
plugins:
  entries:
    borrowed-hands:
      settings:
        listen: {enabled: true, port: 8790}
        public_url: wss://<your public host>/bh/v1/ws
        owners: ["telegram:<your user id>"]
# restart the gateway once
```

The `owner/repo#subdir` form and `--ref` are supported (`hermes_cli/plugins_cmd.py:301-331`,
`hermes_cli/subcommands/plugins.py:18-47`). The host must expose the port over TLS (a reverse proxy, or Railway).

For a manually installed server, uninstall with `hermes plugins remove borrowed-hands`, then remove its data
namespace from `$HERMES_HOME/plugin-data/`. For the Railway template, remove the service and volume instead:
its boot script recopies and re-enables the plugin, so deleting only the plugin directory will not stick.
An unpair is intended to stop the helper; verify this in the integrated pairing acceptance run.

### 11.2 Mac

```
uv tool install 'git+https://github.com/alext8900/borrowed-hands@<sha>#subdirectory=helper'
bh setup        # guided: production sandbox + sudoers, pair, one grant, optional capture, service, selftest
```

`bh setup` is equivalent to: production `setup-bh-builder.sh` (no test flags) + the sudoers line (9.2), then
`bh pair '<string from /hands pair in your DM>'`, `bh caps enable xcode.build`, `bh grant add xcode.build ...`,
optionally `bh caps enable screen.capture`, `bh service install`, `bh selftest --build <workspace>`.

Permission guidance shown by `bh setup` (exact text):
- sudo: "Borrowed Hands needs your admin password twice: once to create the hidden build user 'bh-builder' and
  install the build sandbox in /opt/borrowed-hands, once to add one sudo rule that lets this account start a
  build. It never gets root for anything else."
- Screen Recording: "To let Hermes see your screen on request, allow Terminal in System Settings > Privacy &
  Security > Screen Recording. Nothing is captured unless a request you granted arrives."

When uv is missing, step 1's install command fails with the shell's command-not-found message; the helper has not
run and does not emit a uv diagnostic. Install uv from https://docs.astral.sh/uv/.

Helper failure messages (stderr, then non-zero exit; `<share>` and `<host>/<error>` are documentation placeholders):
- `Xcode not found: install Xcode and run 'sudo xcode-select -s /Applications/Xcode.app'`
- `build sandbox is a TEST install (TEST_MODE present): run 'sudo bash <share>/sandbox/setup-bh-builder.sh' with no flags`
- `sudo rule missing: run 'bh setup'` (selftest, when `sudo -n` cannot reach the dispatcher)
- `pairing code rejected; run /hands pair again` / `server failed to prove its key; check the pairing string` / `cannot reach <host>: <error>`
- `Screen Recording not granted to Terminal: System Settings > Privacy & Security > Screen Recording`

`<share>` means the actual computed install path to the helper's shared sandbox assets, printed in the diagnostic,
not a literal path.

Uninstall: `bh uninstall && uv tool uninstall borrowed-hands-helper`. The first command prompts `[y/N]`; only `y`
or `yes` proceeds. Enter/no prints `Nothing removed.`, exits 1, and stops the command chain. Successful uninstall
leaves `~/Library/Logs/BorrowedHands` for separate removal. Clean-user Mac install/uninstall and total install time
still require device acceptance; server health alone does not establish them.

## 12. Catalog compliance

| Rule | How we comply |
|---|---|
| 2, 4 | The catalog entry pins the SHA; `subdir: borrowed-hands`. |
| 3 | No self-update: the helper never fetches code; `uv tool install` at a SHA is user-run. |
| 6 | `capabilities.provides_tools` lists the 5 tools, `provides_hooks: [pre_tool_call]`, `requires_env: []`. |
| 7 | Run `hermes plugins validate borrowed-hands --install-deps` in CI from day 1. The sudoers, dscl and sandbox sources live outside `borrowed-hands/`, so the scanner's `sudo`/ssh patterns do not fire on the plugin tree, but they are in the same repo, linked from the README and disclosed. |
| 9 | Public `ctx.*` only. No core module is imported or patched; `gateway.run` is only checked for membership in `sys.modules`, and the build handler reads (never sets) the dispatch `tool_call_id` context variable of the already-loaded `tools.approval_context`, through `sys.modules`. |
| 10 | Bounded ranges in `python_dependencies`. |
| 11 | No secrets read. Our own generated keys live in the plugin data dir; disclosed. |
| 12 | `approve` on mutating tools, fail closed; never auto-approve; the per-workspace device confirm times out at 120 s instead of hanging; no spawned Hermes or shell children. |
| 13 | Disclosure lines below go in the README and the PR. |
| 14 | `requires_hermes: ">=0.22.7"` in both plugin.yaml and the entry. |
| 17 | Python, SBPL and Swift source only; no binaries, no .pyc (a CI check); the capture companion is built on the user's Mac from source. |

Disclosure lines (README "What this plugin does on your machine" and the PR body):
- Opens a long-running network listener (HTTP + WebSocket, default port 8790) inside the Hermes gateway process when `listen.enabled` is true.
- Stores an Ed25519 server private key, paired devices' public keys and a job database in `$HERMES_HOME/plugin-data/`.
- Sends build, screenshot and system-info requests to Macs you paired, over connections those Macs open.
- Makes one outbound call of its own: when a build request names no commit, the Hermes server reads the branch list of the granted public repository from its host over HTTPS (`GET <repo>/info/refs?service=git-upload-pack`, as `git ls-remote` does), before the approval prompt. Only a repository whose fingerprint matches a grant the Mac advertised; DNS names only (no IP literals or single labels; lossless IDNA only); one lookup, every address public (loopback, private, link-local, site-local, CGNAT, multicast, reserved, unspecified and embedding IPv6 forms refused); a direct connection to the checked address with TLS verified for the name; no proxy, credentials, cookies or redirects; the call stops waiting after 8 s across DNS, connect, TLS and the response (1 MiB cap), and no request is sent after that. A lookup stuck in the OS resolver cannot be cancelled; at most 4 may be outstanding. Builds with an explicit commit make no outbound call. No other network or third-party calls.
- The Mac side (separate install, not loaded by Hermes, source in the same repo) creates a hidden macOS user `bh-builder`, installs a root-owned dispatcher in `/opt/borrowed-hands`, and adds a sudoers file that permits your numeric uid to run six exact dispatcher command lines as root: the four timeout tiers with fixed local disk/memory budgets, plus `--sweep-only` and `--cancel` (9.2). The dispatcher starts each build as a system LaunchDaemon running as `bh-builder` inside a macOS sandbox profile, and runs `git` (https only) and `xcodebuild`. The helper runs as you, as a LaunchAgent, and stores a device key on the Mac. Screen capture, if enabled, needs the Screen Recording permission.
- No telemetry.

## 13. P0 acceptance tests

"Device" means a real Mac with Xcode: **with the production sandbox install for anything remotely triggered;
hostile probes only on a dedicated test Mac** (9.4).

**Protocol (PR-)**
- PR-1 `bhproto` reproduces every vector in `protocol/test-vectors/vectors.json`: canonical bytes, all rejects, the three signatures (and the tampered ones fail), fingerprints, key ids, the pairing string.
- PR-2 Frame signature verified over the received bytes; a single flipped byte fails; a seq gap, a repeated id, a wrong sid, or an `exp` in the past (beyond 60 s skew) or more than 120 s after `ts` closes 4400.
- PR-3 Contract checks 2 to 12 each produce the documented error code, one test per check, in order (a contract failing two checks reports the earlier).
- PR-4 The same job_id with the same bytes is idempotent; a different hash gives `replay`; a reused nonce gives `replay`.
- PR-5 Pairing string parse: valid, wrong prefix, `#` in URL, bad lengths, `ws://` remote host rejected.
- PR-6 Every schema in `protocol/schemas` validates its examples from PROTOCOL.md; `error.json` accepts `helper_busy`.

**Plugin (PL-)**
- PL-1 The listener starts only in `gateway run` with `listen.enabled`; not in `plugins validate`; a port in use leaves the gateway up and the tools say `listener_not_running`.
- PL-2 reload-plugins: exactly one listener afterwards, and connected helpers reconnect.
- PL-3 The five tools exist when the listener is off and return `helper_unavailable`.
- PL-4 The hook returns approve with the scoped `rule_key` (section 5.6); a different scheme, configuration, destination or helper re-prompts after a "session" answer, a new SHA does not; `dispatch_tool` without the hook gives `denied/approval_not_observed`.
- PL-5 Pair code: single use, 10 min TTL, superseded by a new one, stored hashed only (grep the DB and the logs for the plaintext), rate limit enforced.
- PL-6 The `/hands pair` message is edited to "Paired: ..." from the notifier's `bh-owner-notices` worker thread, never the listener thread (real gateway + fake platform, or Telegram on Railway).
- PL-7 Plugin restart mid-job: the job becomes `stale`, then is reconciled by `jobs.sync`; a result delivered twice is stored once.
- PL-8 `/hands` refuses non-owners and `pair` outside a DM.
- PL-9 The system prompt section renders at 4000 chars or fewer and names all five tools.
- PL-10 Diagnostics reach the model only as data under a fixed key; a diagnostic containing instruction-like text is passed through verbatim within its caps and changes nothing the plugin does.

**Helper (HE-)**
- HE-1 Pair against a local test server; config written only after the server proof; every capability is off after pairing.
- HE-2 Reconnect backoff with jitter; stops on 4401, 4403 and 4410; survives a 19 s window of 502s.
- HE-3 Helper restart mid-build: the job becomes `failed/helper_restarted`, no uid-470 processes remain, and the result is delivered after reconnect.
- HE-4 A grant revoke mid-build cancels it; a revoke while queued gives `cancelled/grant_revoked` on dequeue; a config edit re-sends `caps`.
- HE-5 Half-open: drop packets (pfctl or a proxy pause) and the helper reconnects within 45 s.
- HE-6 Key file rules (PROTOCOL 3.4): a symlinked dir or file, mode 0644, a pre-existing tmp file, `nlink 2`, or another owner each make the helper refuse to start.
- HE-7 `bh setup` on a clean macOS user ends with `bh selftest` all PASS; `bh selftest` FAILs with the documented fix text when TEST_MODE exists, when the sudoers rule is missing, and when the server key proof fails.
- HE-8 Queue: the 5th concurrent build request gets `helper_busy/queue_full`; a queued job past `expires_at` becomes `expired` on dequeue; a cancelled queued job never reaches the dispatcher.

**Capability (CA-)**
- CA-1 (device) `xcode.build` happy path, `platform=macOS,arch=arm64`, from Hermes on Railway to a build Mac and back: result `succeeded`, duration reported.
- CA-1s (device) `generic/platform=iOS Simulator` compile-only through the **real dispatcher path** (uid 470 LaunchDaemon, not uid 501), on the demo app and on an **asset-catalog fixture** (an `.xcassets` with an app icon and a color set): `succeeded`; any other simulator destination string is refused by the helper and by the runner.
- CA-2 (device) Compile error: 1-based line and column, relative file path, no absolute paths in the result JSON, caps applied.
- CA-3 (device) Commit not on `bh/*` gives `needs_resolution/not_on_bh_branch`; repo mismatch gives `needs_resolution/repo_mismatch`; a project with `XCRemoteSwiftPackageReference` or a remote `Package.resolved` gives `needs_resolution/unsupported_dependencies` with the files and no build started.
- CA-4 (device) Cancel mid-build from the helper (owner's uid): `cancelled`, and no uid-470 process remains within 10 s.
- CA-5 (device; capture is not implemented in 0.1) `screen.capture` returns a PNG of 2000 px or less that the model can see (Anthropic) and a text_summary for non-vision models; nothing is captured without a verified contract.
- CA-6 `system.info` returns exactly the narrowed fields (PROTOCOL 4.4); needs no approval.
- CA-7 `hands_find` requirement grammar: each form, the AND, offline exclusion, `invalid_arguments` on bad input.

**Security (SE-)** (each rule is mutation-checked; device rows on a dedicated test Mac only)
- SE-1 (device) Already covered by the merged sandbox suite (`tests/run-tests.sh`, `scripts/probe-suite.sh`, 112/112, 18/18 mutants): re-run once on the final install and attach the output; do not rebuild it.
- SE-2 (device) Same: hooks, filters, submodules and `ext::` never fire (sandbox suite git fixtures); plus the new `bh/*` ancestry path.
- SE-3 A forged contract (wrong key), an expired contract, wrong helper_id, a scheme or destination off the allowlist, or a URL in params: all refused before the dispatcher starts (assert `bh-dispatch` was never spawned).
- SE-4 An edge-injected frame (no signature, a replayed old signed frame, a frame from another session, an expired frame) is rejected.
- SE-5 A revoked helper cannot authenticate; an unpaired Mac deletes its keys.
- SE-6 No code, key or signature appears in any log line at any level (log capture + grep).
- SE-7 (device) The installed sudoers file admits only the six exact dispatcher argv forms in 9.2 (four timeout tiers with fixed local budgets, `--sweep-only`, and `--cancel`): `sudo -n -u bh-builder /bin/sh`, `sudo -n /bin/sh`, and `sudo -n /opt/borrowed-hands/bin/bh-dispatch --profile allow-all.sb` are all refused.
- SE-8 Production gate: with `/opt/borrowed-hands/TEST_MODE` present or test profiles installed, the helper does not advertise `xcode.build` and refuses build contracts.
- SE-9 Approval independence: under `approvals.mode: off` and `--yolo`, an ungranted workspace, scheme or destination is still refused by the Mac.
