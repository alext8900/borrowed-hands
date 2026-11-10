# Borrowed Hands operator reference

Start with the [quickstart](QUICKSTART.md). This page has the detail behind it: every Railway setting, the plugin
settings for an existing Hermes, what to expect in the logs, the failure messages and how to recover.

Two parts: the **server** (Hermes with the plugin) and the **Mac** (the `bh` helper, which dials out to the server).
The target is about 15 minutes for a first install, not counting the Xcode download.

## Prerequisites

| Need | Where | Check |
|---|---|---|
| Railway account with a volume (Hobby plan is fine), or an existing Hermes v0.22.7+ with a public HTTPS host name | railway.com | can create a project |
| A model provider key | your provider | you know the variable name Hermes uses for it |
| A Telegram bot token and your numeric Telegram user id | @BotFather, @userinfobot | token looks like `123456:ABC...` |
| An Apple silicon Mac, an admin account | the build Mac | `sw_vers` |
| Xcode at `/Applications/Xcode.app`, opened once, license accepted | App Store | `bh selftest` shows `PASS  Xcode` |
| `uv` (it provides Python 3.11+ for the helper) | https://docs.astral.sh/uv/ | `uv --version` |

Builds always use `/Applications/Xcode.app`, whatever else is installed or selected: the runner calls that copy by
absolute path with its own `DEVELOPER_DIR`. `bh setup` and `bh selftest` check that exact copy the same way, so a
passing check means builds use the Xcode it reported. A different `xcode-select` choice is shown as a note only.

## Server option A: Railway

The image is built from `deploy/railway/Dockerfile`: Hermes `nousresearch/hermes-agent:v0.22.7` pinned by digest
`sha256:e6d81ae52ebf6a43efa529701405509f20054ba818f179ddd748bed4f8af584c`, plus the `borrowed-hands/` plugin from the
commit Railway builds. Record that commit. Plugin code is copied from the image on every boot; nothing is
downloaded at run time.

Create a **new** project and service from the repository (or run `railway up` from a checkout after linking the
Railway CLI to the service), then set, in the service settings:

| Setting | Value |
|---|---|
| Dockerfile path | `deploy/railway/Dockerfile` |
| Volume | mount path `/opt/data` (this is `HERMES_HOME`; without it keys and pairings vanish on redeploy) |
| Networking | generate a domain, target port `8790` |
| Healthcheck path | `/bh/v1/healthz`, timeout 120 s |
| Replicas | 1 |
| Restart policy | `ON_FAILURE`, maximum 5 retries |
| Variable `PORT` | `8790` (makes Railway check the same port the plugin listens on) |

Set the Dockerfile path in the service settings: `railway.json` carries the same values, but Railway does not
always apply it to CLI deploys. Do **not** reuse a volume that held another Hermes plugin config: its persisted
`config.yaml` is never overwritten, and a stale enabled plugin can hold port 8790.

### Variables you enter (never put these in the repository)

| Variable | Value | Needed for |
|---|---|---|
| a model provider key (for example `ANTHROPIC_API_KEY` or `OPENROUTER_API_KEY`; optional `HERMES_INFERENCE_PROVIDER`) | your key | Hermes answering chat |
| `TELEGRAM_BOT_TOKEN` | from @BotFather | Telegram |
| `TELEGRAM_ALLOWED_USERS` | your numeric Telegram id | who the gateway answers at all |
| `BH_OWNERS` | `telegram:<your numeric id>` | who may run `/hands` (the plugin owner allowlist) |
| `BH_PUBLIC_URL` | optional; defaults to `wss://$RAILWAY_PUBLIC_DOMAIN/bh/v1/ws` | the address in the pairing string |

The image sets `BH_LISTEN=1` and `BH_TRUSTED_PROXY=railway`. The proxy setting trusts Railway's edge to append the
real client address to `X-Forwarded-For`; the listener then uses the rightmost address of the first header field.
Expose this deployment only through Railway's edge, with no public bypass to the listener, and do not reuse this
setting behind another proxy. Railway redeploys when variables change (about a minute).

### Expected after deploy

- Railway shows the deployment healthy. `curl https://<your-domain>/bh/v1/healthz` prints `{"ok": true, "v": 1}`.
  If your network blocks `*.up.railway.app`, test from another network.
- Railway logs show `[bh-start] config.yaml created` on first boot, or `borrowed-hands already enabled, untouched`
  on later boots, and one `[bh-start] seeded ...` line.
- `/opt/data/logs/agent.log` (read it as the `hermes` user) shows
  `borrowed_hands: listener up on 0.0.0.0:8790 (server key <16 hex>)` and
  `borrowed_hands: registered (v0.2.0, listener up) in <n> ms`, once per gateway boot. With local Docker:
  `docker exec --user hermes <container> grep 'borrowed_hands:' /opt/data/logs/agent.log`.
- The listener starts only in `hermes gateway run`. `hermes plugins validate`, doctor and other CLI processes never
  open the port.

### Startup config policy

The startup script adds `borrowed-hands` to `plugins.enabled` in an existing valid `config.yaml`, keeping comments
and other settings; a config that already lists it is left byte-identical. `plugins` must be a mapping and
`plugins.enabled` a list of strings. Empty or comment-only files, explicit nulls, a scalar `enabled:` and malformed
YAML stop startup with `[bh-start] ERROR: config merge refused (...)`, leaving the original file untouched. A
needed merge writes a private temporary file, fsyncs it, keeps the original mode and owner, and atomically
replaces the file. Stock Hermes then manages config permissions and may run its own migrations.

## Server option B: an existing Hermes

Install and enable the plugin at an exact commit:

```sh
hermes plugins install alext8900/borrowed-hands#borrowed-hands --ref <40-character commit SHA> --enable
```

Hermes asks for consent to install the plugin's Python dependencies (`aiohttp`, `cryptography`); `--yes-deps`
answers it for non-interactive installs. Then set `plugins.entries.borrowed-hands.settings` in `config.yaml`:

```yaml
plugins:
  entries:
    borrowed-hands:
      settings:
        listen: {enabled: true, host: 127.0.0.1, port: 8790}
        public_url: wss://hermes.example.com/bh/v1/ws
        owners: ["telegram:123456789"]
```

| Setting | Meaning | Environment fallback |
|---|---|---|
| `listen.enabled` | open the listener (only in `hermes gateway run`) | `BH_LISTEN=1`; `BH_LISTEN=0` always disables |
| `listen.host`, `listen.port` | where the listener binds (default `0.0.0.0:8790`) | `BH_PORT` |
| `public_url` | the `wss://<host>/bh/v1/ws` address put in pairing strings | `BH_PUBLIC_URL`, `RAILWAY_PUBLIC_DOMAIN` |
| `owners` | `platform:user_id` list allowed to run `/hands` | `BH_OWNERS` (comma-separated) |
| `max_frame_bytes` | largest frame (default and maximum 16 MiB) | none |
| `trusted_proxy` | `railway` only behind Railway's edge; otherwise leave unset | `BH_TRUSTED_PROXY` |

Values in `config.yaml` win over environment fallbacks, and settings are read once when the gateway starts, so
restart it after a change. The plugin speaks plain HTTP and WebSocket: provide the public endpoint yourself with a
TLS reverse proxy that forwards `https://<host>/bh/v1/` to the listener and allows WebSocket upgrades on
`/bh/v1/ws`. Then check `curl https://<host>/bh/v1/healthz`.

## Server failures

| You see | Cause and fix |
|---|---|
| Railway build: Railpack, "no Dockerfile" | Dockerfile path not set in the service settings |
| Deploy never turns healthy; no `registered` line in `agent.log` | check boot errors and file ownership, a stale plugin holding 8790, and the `/opt/data` volume mount |
| `[bh-start] ERROR: config merge refused (...)` | startup stopped before Hermes init and the original config is unchanged; fix the persisted config (see the startup config policy) |
| `/hands status`: "Listener: off (not the gateway process ...)" | not started by `hermes gateway run`; do not override the start command |
| `/hands status`: "Public URL: not set" | set `public_url` / `BH_PUBLIC_URL` (or generate the Railway domain) and restart |
| "Only the Borrowed Hands owner can do that." | your `platform:user_id` is not in `owners` / `BH_OWNERS`; `/hands whoami` in a DM prints the exact value to add |
| "No owner is configured ..." | `owners` / `BH_OWNERS` is empty; `/hands whoami` in a DM prints your value, set it and restart |
| "Run /hands pair in a direct message with me ..." | pairing only works in a DM, never in a group |
| 502 for 10 to 20 s after a redeploy | expected while the new container starts; the Mac helper is designed to reconnect on its own |

## Pairing

In a **direct message** with your bot, send `/hands pair`. The bot replies:

```
Pair your Mac: run this on it (valid 10 minutes, single use):
bh pair '<pairing string>'
```

Use it at the pairing step of `bh setup` (paste only the part starting with `bh1:`), or run that command on the Mac
yourself. Each new `/hands pair` invalidates earlier unused strings. Once the Mac connects, the bot edits the
pairing message to remove the secret and later replies `<Mac> is ready.` with the capabilities the Mac granted.
`/hands status` (also `/hands doctor`) shows the listener, the public URL, owners and paired Macs.
`/hands whoami`, in a DM, answers anyone with their own `platform:user_id` and whether it is an owner; it changes
nothing.

## Mac helper reference

Install with `uv tool install "git+https://github.com/alext8900/borrowed-hands@v0.2.0#subdirectory=helper"`
(or `uv tool install ./helper` from a checkout of this repository), then `uv tool update-shell` and open a
new terminal. The helper needs the whole checkout: its package includes the protocol schemas and the sandbox
scripts. `bh setup` then runs these steps, skipping any that are already done:

1. Xcode check (the copy at `/Applications/Xcode.app`, license accepted, first launch done).
2. Production build sandbox: hidden user `bh-builder` (uid and gid 470), `/opt/borrowed-hands`. Never test mode.
3. The exact sudo rule in `/etc/sudoers.d/borrowed-hands`. If a root:wheel 0440 file is already there, setup compares
   its bytes with the exact rule (`sudo /usr/bin/cmp -s ...`, printed first) and keeps it only if they match;
   otherwise it stages the rule, checks it with `visudo -cf` and moves it into place. Then it asks sudo itself
   (`sudo -n -l -- <command>`) whether each of the six exact commands is permitted. If one is not, another rule
   on this Mac overrides ours; setup names the command and stops without editing other sudo rules.
   Permitted is not the same as password-free: a later rule that only adds a password requirement (for example
   `PASSWD:` on `/opt/borrowed-hands/bin/` or on one of the commands) still passes this check and `bh selftest`.
   It shows when a build runs: the build fails with reason `dispatch_error` and the message below. Setup cannot
   detect it without running the build command itself, which it does not do.
   It is installed even when your account already has broader sudo rights.
4. Pairing.
5. Capabilities and one workspace grant. Everything is off until you say yes; per-build confirmation defaults off.
6. The background helper (user LaunchAgent `dev.borrowed-hands.helper`).
7. Self-test.

Setup prints each admin command before running it; sudo may ask for your password. If a step fails, setup says
which steps are already done and that nothing was rolled back: fix the problem and run `bh setup` again, or run
`bh uninstall` to remove everything. Screen capture has no executor in this version, so setup skips it and does
not request Screen Recording.

Manual equivalents after pairing: `bh caps enable system.info`, `bh caps enable xcode.build`, `bh grant add ...`,
`bh service install`. Other commands: `bh status`, `bh grant list`, `bh grant revoke <workspace>`,
`bh caps disable xcode.build`, `bh jobs`, `bh cancel <job id>`, `bh unpair`, `bh rotate-key`, `bh run` (foreground).
`bh grant add` never silently replaces a grant with the same `--workspace` name: it refuses and shows the existing
one, unless the settings are identical or you pass `--replace` (which prints the before and after).

### Helper messages

| Message | Fix |
|---|---|
| `zsh: command not found: bh` | `uv tool update-shell`, then open a new terminal |
| `zsh: command not found: uv` | install uv from https://docs.astral.sh/uv/ |
| `Note: builds use /Applications/Xcode.app; xcode-select points at ...` | informational; builds ignore the global selection |
| `Xcode is not installed at /Applications/Xcode.app ...` | install Xcode from the App Store (or move your copy there), open it once |
| `builds use /Applications/Xcode.app, but the selected Xcode is ...` | install or rename Xcode to `/Applications/Xcode.app` |
| `the license of the Xcode at /Applications/Xcode.app has not been accepted` | the printed command, which targets that copy: `sudo /usr/bin/env DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer /Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild -license accept` |
| `Xcode at /Applications/Xcode.app did not report its version` | open that Xcode once, or run the printed `... xcodebuild -runFirstLaunch` command for that copy |
| `Setup stopped at [n/7] ...` | fix the error shown, then `bh setup` again |
| `build sandbox is a TEST install (TEST_MODE present): ...` | run the production setup command it prints |
| selftest `FAIL  sudo rule: installed` | `bh setup` (it replaces a missing or different rule file) |
| `another sudo rule on this Mac overrides it`, or selftest `FAIL  sudo rule: permitted` | change the later rule in `/etc/sudoers` or `/etc/sudoers.d` that matches `/opt/borrowed-hands/bin/bh-dispatch` |
| a build returns `sudo asked for a password for the Borrowed Hands build command: another sudo rule on this Mac overrides its password-free rule. Remove or reorder that rule, then run bh selftest.` | a later rule in `/etc/sudoers` or `/etc/sudoers.d` makes a build command need a password; remove or reorder it so the Borrowed Hands rule comes last, then `bh selftest` |
| `pairing code rejected; run /hands pair again` | the string expired or was used |
| `server failed to prove its key; check the pairing string` | the string was altered, or the server key changed; re-pair |
| `cannot reach <host>: <error>` | wrong public URL, server down, or a redeploy in progress (retry in 30 s) |
| `the helper is running; stop it first ...` | to re-pair, use `bh unpair`, then `bh setup` (grants are reset; add them again) |

### Build failures returned to Hermes

Each failure keeps its reason code (`detail.reason`) and carries a fixed sentence saying what to do:

| Reason | Meaning |
|---|---|
| `not_on_bh_branch` | the commit is not reachable from any `bh/*` branch of the granted repository; push it there |
| `commit_not_found` | the Mac could not fetch that commit; check it is pushed to the public repository |
| `unsupported_dependencies` | remote Swift packages; vendor them as local path packages (`detail.files` lists where) |
| `project_missing` | the granted project path is not in that commit; check `bh grant list` |
| `unsafe_tree` | a symlink out of the checkout, an extra `.git` entry, or too many files |
| `timeout`, `disk_budget`, `memory_budget` | the build exceeded its time, disk (default 20 GiB) or memory (default 6 GiB) budget |
| `dispatch_error` | the build could not start or finish; run `bh selftest` on the Mac and read `~/Library/Logs/BorrowedHands/helper.log` |

### Self-test

`bh selftest` prints one PASS/FAIL line per check with the fix for each failure. `bh selftest --build <workspace>`
also runs one real local build of a `bh/*` tip of that workspace (chosen by sorting SHA/ref pairs, so not
necessarily the newest). Read the returned `state`: a real compile failure with valid diagnostics still passes the
interface check. The self-test is local; the end-to-end check is a build requested through Hermes.

## Uninstall

See the [complete checklist](QUICKSTART.md#uninstall). In short: `bh uninstall` on the Mac (it stops the helper
first and removes nothing while one still runs, verifies each removal, and lists anything left or uncheckable with
the exact command; the empty, macOS-protected per-user folders under `/private/var/folders`, owned by uid 470 once
the account is gone, are reported as `ok` with a note because nobody can remove them, but only when the teardown
verified them in the same run; otherwise they are `UNKNOWN`, since you cannot look inside them. Their `TemporaryItems`
folders cannot be listed even by root, so their emptiness is checked from size and link count on APFS; if that
check fails, the account and `/opt/borrowed-hands` are kept and it tells you to restart and rerun, or to give your
Terminal Full Disk Access for one rerun. A reinstall reuses uid 470, so `bh setup` checks those old folders first
and refuses if they are not verifiably empty), then
`uv tool uninstall borrowed-hands-helper`; on Hermes,
`/hands unpair` each Mac, then delete the Railway service and volume, or stop the gateway and remove the plugin and
only its own data directory.

How this was tested: see [How we tested it](../README.md#how-we-tested-it).
