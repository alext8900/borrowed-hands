# Borrowed Hands quickstart

Borrowed Hands lets a Hermes agent running in the cloud compile Xcode projects on a Mac you lend it. There are two
lanes, and they can be the same person:

- **Run Hermes** (the operator): a Hermes gateway with the Borrowed Hands plugin, reachable over public WSS.
- **Lend your Mac** (the Mac owner): the `bh` helper on an Apple silicon Mac with Xcode.

The Mac only dials out. It needs no inbound port, no SSH and no remote shell. Pairing grants nothing; the Mac owner
decides exactly which repository, schemes, configurations and destinations Hermes may build.

**Source:** everything here is in [`alext8900/borrowed-hands`](https://github.com/alext8900/borrowed-hands). The demo
project, [`alext8900/borrowed-hands-demo`](https://github.com/alext8900/borrowed-hands-demo), is a separate public
repository.

## Lane 1: Run Hermes

You need Hermes v0.22.7 or newer, a chat platform Hermes supports (the examples use Telegram), a model provider key,
and a public HTTPS host name for the gateway. The gateway also needs outbound HTTPS to your Git host: it reads a
granted repository's branch list (anonymously) to find the latest `bh/` commit when a request names none.

### Option A: Railway (recommended; this repository ships the template)

1. Create a Railway service from this repository and set, in the service settings:

   | Setting | Value |
   |---|---|
   | Dockerfile path | `deploy/railway/Dockerfile` |
   | Volume | mounted at `/opt/data` (keeps the server key and pairings across redeploys) |
   | Networking | generate a domain, target port `8790` |
   | Healthcheck path | `/bh/v1/healthz` |
   | Replicas | 1 |

2. Add these variables in the service's variable settings (never in the repository):

   | Variable | Value |
   |---|---|
   | `PORT` | `8790` |
   | your model provider key | for example `ANTHROPIC_API_KEY` |
   | `TELEGRAM_BOT_TOKEN` | from @BotFather |
   | `TELEGRAM_ALLOWED_USERS` | your numeric Telegram user id |
   | `BH_OWNERS` | `telegram:<your numeric Telegram user id>` |
   | `HERMES_MEDIA_DELIVERY_STRICT` | `1` (recommended; see below) |

   The image turns the listener on and derives the public URL (`wss://<your domain>/bh/v1/ws`) from Railway's
   domain. Set `BH_PUBLIC_URL` only to override it.

   `HERMES_MEDIA_DELIVERY_STRICT=1` reduces the risk of a prompt-injected reply attaching the server key to the
   chat. By default Hermes attaches files that a reply names with a `MEDIA:` tag or, for common file types
   including `.key`, by bare absolute path, and the plugin's data directory is not on its denylist. Strict mode
   only attaches files from Hermes's trusted artifact directories (its caches and similar),
   `HERMES_MEDIA_ALLOW_DIRS`, and files modified within a recency window (600 seconds by default). The key is
   written once, so strict mode blocks it except in the window after it is created (including a key made during
   recovery) or restored with a fresh modification time, and whenever an allowed directory contains it. Keep
   `/opt/data` and the plugin's data directory out of `HERMES_MEDIA_ALLOW_DIRS`. The job database (`bh.sqlite3`:
   no private keys or tokens, but results, diagnostics and the audit log) can still be attached within the
   window after a write, and strict mode does not limit the agent's terminal or file tools. It applies to every
   file the gateway would attach, so an agent that sends files from other places needs them in
   `HERMES_MEDIA_ALLOW_DIRS`.

3. Deploy, then check: `curl https://<your domain>/bh/v1/healthz` prints `{"ok": true, "v": 1}`.

### Option B: an existing Hermes

1. Install and enable the plugin at an exact commit:

   ```sh
   hermes plugins install alext8900/borrowed-hands#borrowed-hands --ref <40-character commit SHA> --enable
   ```

2. Add the settings to Hermes `config.yaml`:

   ```yaml
   plugins:
     entries:
       borrowed-hands:
         settings:
           listen: {enabled: true, host: 127.0.0.1, port: 8790}
           public_url: wss://hermes.example.com/bh/v1/ws
           owners: ["telegram:123456789"]
   ```

3. Put a TLS reverse proxy in front: `https://hermes.example.com/bh/v1/` must reach `127.0.0.1:8790` with WebSocket
   upgrades allowed on `/bh/v1/ws`. The plugin itself speaks plain HTTP; `public_url` only tells Macs where to
   connect, it does not create the endpoint.
4. Recommended: set `HERMES_MEDIA_DELIVERY_STRICT=1` in the gateway's environment (or `gateway: {strict: true}` in
   `config.yaml`). It blocks the server key only once the key is older than the configured recency window (600
   seconds by default) and while no allowed directory contains it. It gives the job database no confidentiality
   guarantee and does not limit the terminal and file tools. Option A has the exact conditions.
5. Restart `hermes gateway run`. The listener only starts in the gateway process. Check
   `curl https://hermes.example.com/bh/v1/healthz`.

More operator detail (all settings, logs, failure table): [operator reference](INSTALL.md).

## Lane 2: Lend your Mac

You need an Apple silicon Mac, an admin account, [uv](https://docs.astral.sh/uv/getting-started/installation/), and
Xcode installed at `/Applications/Xcode.app` and opened once. Builds always use that exact copy of Xcode, whatever
`xcode-select` points at; if another copy or the Command Line Tools are selected, setup just notes it.

1. **Install the helper** from a checkout of this repository:

   ```sh
   git clone https://github.com/alext8900/borrowed-hands.git
   cd borrowed-hands
   uv tool install ./helper
   uv tool update-shell
   ```

   Then **open a new terminal window** so `bh` is on your PATH (uv installs it in `~/.local/bin`). Check with
   `bh --version`.

2. **Run the guided setup:**

   ```sh
   bh setup
   ```

   It checks Xcode, then installs the build sandbox and one exact sudo rule. It prints every admin command before
   running it, and sudo may ask for your password. Then:

   - **Pairing (step 4).** In a direct message with your Hermes bot, send `/hands pair`. Paste only the part of the
     reply that starts with `bh1:` (not `bh pair` or the quotes). It is single use and valid for 10 minutes; keep it
     out of screenshots.
   - **What Hermes may do (step 5).** Answer yes to system info and to builds, then grant the demo project, a
     small SwiftUI app with a single `bh/main` branch
     ([borrowed-hands-demo](https://github.com/alext8900/borrowed-hands-demo)):

     | Prompt | Answer |
     |---|---|
     | Short name | `demo` |
     | Public https URL | `https://github.com/alext8900/borrowed-hands-demo.git` |
     | Project inside the repo | `BorrowedHandsDemo.xcodeproj` |
     | Scheme(s) | `BorrowedHandsDemo` |
     | Configuration(s) | `Debug` (press Return) |
     | Destination | `1` (macOS on Apple silicon) |
     | Ask before every build | your choice (default no) |

   Setup then starts the background helper, runs a self-test and ends with `This Mac is ready.` and a suggested
   request.

3. **Watch for the readiness message.** In the same chat, Hermes replies on its own:
   `<your Mac> is ready. System information. Xcode builds: demo / BorrowedHandsDemo / Debug / macOS arm64.`
   If it does not arrive, `/hands status` shows the paired Macs.

4. **Ask Hermes:**

   > Build my demo project on my Mac

   The first time, Hermes asks for the repository's URL once (`https://github.com/alext8900/borrowed-hands-demo.git`):
   the Mac shares only a fingerprint of it, and Hermes remembers the URL after that. Approve the build when Hermes
   asks; the approval names the exact commit, for example `Build demo (scheme BorrowedHandsDemo, Debug, macOS arm64)
   at commit 5f2eee7 (latest on bh/main) on <your Mac>`. You get the build state, duration, Xcode version, and any
   errors and warnings with file, line and column. A failed compile is still a successful answer: read `state`
   (`succeeded` or `failed`), not just `ok`.

   An explicit request with a full commit SHA always works too. Get the SHA with
   `git ls-remote https://github.com/alext8900/borrowed-hands-demo.git refs/heads/bh/main`, then:

   > Build workspace demo from https://github.com/alext8900/borrowed-hands-demo.git at commit <full 40-character
   > SHA>, scheme BorrowedHandsDemo, configuration Debug, destination platform=macOS,arch=arm64.

### How Hermes fills in a build request

The build tool's own description: "Every argument is optional: pass only what the user said ("build my demo project"
needs none). Omitted values come from the Mac's grant when it allows exactly one choice, and an omitted commit is
the latest commit on the repository's bh/* branch." In detail:

- **Workspace, scheme, destination:** filled in only when the Mac's grant allows exactly one. Otherwise Hermes asks
  you, with the granted choices as options.
- **Configuration:** the grant's only configuration; with several, `Debug` if it is one of them, otherwise Hermes
  asks.
- **Commit:** the latest commit of the repository's only `bh/` branch, or of the branch you name ("on bh/release").
  If the repository has several `bh/` branches and you name none, Hermes asks which, listing up to 10 of them. A
  full 40-character commit SHA that you give is used as is.
- **Repository URL:** asked once, then remembered for that workspace's repository.
- **Approval:** the approval prompt names the resolved commit and how it was chosen, and the build runs exactly that
  commit.
- **Duplicates:** while a build request is waiting for your approval, an identical request in the same chat is refused. Once you approve it, the guard is released, so an identical request made while that build runs asks for its own approval and starts a separate build.

Keep the Mac awake and your user logged in: the helper runs as a LaunchAgent in your login session.

## Use your own project

Hermes can only build what the Mac owner granted, and only commits reachable from a branch named `bh/<something>`
in the granted repository. Pushing a `bh/` branch vouches for its **entire history**: every commit reachable from
it is buildable, including commits that are also on `main` or other branches. Branch membership is not exclusive in
Git, so the prefix decides which history is buildable, not which branch a commit "belongs" to. Only push to `bh/`
what you are willing to have built.

Your project must be in a **public HTTPS** Git repository, have a **shared** scheme (Xcode: Product > Scheme > Manage
Schemes > Shared), and use no remote Swift packages (vendor them as local path packages). Builds are unsigned.

Worked example for an app at `https://github.com/you/WeatherApp.git` with `WeatherApp/WeatherApp.xcodeproj`:

1. Push the commit you want built to a `bh/` branch:

   ```sh
   git push origin HEAD:bh/hermes
   ```

2. On the Mac, grant it (paths are relative to the repository root, with no leading `./`):

   ```sh
   bh grant add xcode.build --workspace weather \
     --repo https://github.com/you/WeatherApp.git \
     --project WeatherApp/WeatherApp.xcodeproj \
     --scheme WeatherApp --configuration Debug \
     --destination 'platform=macOS,arch=arm64' --destination 'generic/platform=iOS Simulator'
   ```

   Add `--confirm` to be asked on the Mac before every build, or `--expires 7d` for a time-limited grant.

3. Ask Hermes: "Build my weather project on my Mac." With `bh/hermes` as the only `bh/` branch, Hermes builds its
   latest commit. This grant allows two destinations, so Hermes asks which (or say "for the iOS Simulator"). The
   first build also asks for the repository URL. To build newer work, push it to the `bh/` branch again; to build an
   exact commit, give its full SHA.

Manage grants with `bh grant list`, `bh grant revoke weather` (cancels its queued and running jobs within a few
seconds), and `bh caps disable xcode.build` (turns building off entirely). The two supported destinations are
`platform=macOS,arch=arm64` and `generic/platform=iOS Simulator` (compile only).

## Uninstall

### On the Mac

```sh
bh uninstall
```

Answer `y`. It stops the background helper first and waits (up to about two minutes) for a running build to stop.
If the helper is still running, for example `bh run` in another terminal, it removes nothing else and asks you to
stop it and run `bh uninstall` again. Otherwise it unpairs (telling the server when it can reach it), removes the
sudo rule (first, so nothing can start a new build while the build user is checked and removed; if that fails it
stops there), then the build sandbox and the `bh-builder` user, then **checks each item** and prints a checklist:

- `ok`: verifiably gone.
- `LEFT`: still there, with the exact command that removes it.
- `UNKNOWN`: the check itself failed, with the command to check by hand. This never counts as removed.

Any `LEFT` or `UNKNOWN` item makes the command exit nonzero. The teardown only removes a `bh-builder` account,
group, home and caches it can verify are Borrowed Hands' own (uid and gid 470 with the attributes setup gives
them). If another account already uses uid 470 or that home, its data is left in place and reported.

Once the build user has run a build, macOS keeps a per-user folder for it under `/private/var/folders`. The
teardown removes everything it can in that folder. Only two kinds of entry may stay, and only when they are
verified empty:

- Folders that carry a system flag (`sunlnk`) not even root can override, and a system data vault inside them.
- The two `TemporaryItems` folders, `T/TemporaryItems` and `T/com.apple.trustd/TemporaryItems`. macOS protects
  them with Full Disk Access, so nobody can list them, root included. The teardown checks them from their
  metadata instead: on APFS an empty directory reads size 64 and link count 2, and each entry adds 32 bytes and
  one link. That is observed APFS behavior, not a documented interface, so the teardown accepts it only for a
  directory at exactly that place, owned by 470:470, mode 0700, with no flags, whose listing is refused with
  "Operation not permitted", on an APFS volume. It then tries `rmdir`; if macOS refuses, the empty folder stays.
  On a real Mac (macOS 27.2) root read exactly that shape for both folders, its listing was refused with exactly
  that error, and the teardown reported both as verified empty.

The checklist shows these folders as `ok` with a `left by macOS` note: there is nothing to do, and `rm` cannot remove them.
After the account is deleted they show owner `470` instead of `bh-builder`. The checklist runs as you, and you
cannot look inside `T` and `C` (only uid 470 and root can), so it relies on the teardown for them: it shows `ok`
only when the teardown verified them in the same `bh uninstall` run. If the account was already gone before the
run, for example deleted by hand, nothing verified them, and the checklist shows `UNKNOWN` with a `sudo ls -laO`
command to look. Rerunning `bh uninstall` will not change that line, and the command still exits nonzero. If the
listing shows only macOS's protected folders, there is nothing to remove. If it shows anything else, run
`bh setup`: it checks them as root before it creates `bh-builder` and prints the one safe removal command
(described below).

Deleting the account itself can stall. Observed on macOS 27.2, with the cause not proven: a per-user `distnoted`
process running as uid 470 appeared the moment `dscl . -delete /Users/bh-builder` started, launchd restarted it
when it was killed, and the delete hung until opendirectoryd gave up after about five minutes ("ODRecordDelete
... forcing a timeout") and `dscl` failed with DS Error -14987 (eUndefinedError). With uid 470's processes stopped
first, the delete finished in 37 seconds. So, only after it has verified the account is ours, the teardown boots
out uid 470's launchd domains, kills its processes and waits until none are left, then deletes the account with a
two-minute limit. If that fails it first checks whether the account is gone anyway (opendirectoryd can finish a
delete after `dscl` gave up; only a listing that works and no longer shows `bh-builder` counts), and otherwise
stops the processes again and retries once. If the retry also fails, it keeps the account and
`/opt/borrowed-hands`, exits nonzero and says that opendirectoryd timed out the delete; run `bh uninstall` again
later. On the same Mac, with this change and a uid 470 `distnoted` still lingering, `bh uninstall` finished in 34
seconds with every checklist line `ok`, and a later `bh setup` passed its check of the folder the removed account
had left behind.

If a `TemporaryItems` folder cannot be verified empty (it has entries, or any of the checks above fails), the
teardown fails closed: it keeps the `bh-builder` account and `/opt/borrowed-hands`, exits nonzero and prints the
recovery path. Restart the Mac (macOS clears these temporary folders at startup), then run `bh uninstall` again
before deleting anything by hand. If it still cannot be verified, temporarily give your Terminal Full Disk Access
(System Settings > Privacy & Security > Full Disk Access), rerun `bh uninstall`, then remove that access. Never
give Full Disk Access to `/bin/sh`, `bh-runner` or `bh-dispatch`: builds would then be able to read your protected
data.

Reinstalling reuses uid 470. macOS names the per-user folder after the account's generated UUID as well as its
uid, and setup never sets that UUID, so a new `bh-builder` gets a new folder. The old folder stays, owned by uid
470, and the new account could open anything in it by path. Before it creates the account, setup therefore checks
every folder under `/private/var/folders` owned by uid 470, read-only and by the same rules, and installs nothing
if one is not verifiably the empty protected skeleton. It then prints how to recover: restart and run `bh setup`
again; if that does not help (macOS clears these folders at startup for existing accounts, and whether it does so
for a deleted account is not verified), give your Terminal Full Disk Access, run the `sudo rm -rf` command setup
prints, remove that access and run `bh setup` again. That command names only the per-user folder itself, single
quoted, and only when its name has the form macOS generates. Names inside the folder were chosen by build code, so
setup and the teardown print them only escaped, in diagnostics, and never in a command; do not paste one after
`rm`.

When it reports `Uninstalled and verified`, finish with:

```sh
uv tool uninstall borrowed-hands-helper
rm -rf ~/Library/Logs/BorrowedHands        # optional: the helper's logs are kept until you delete them
```

What it removes, and where:

| Item | Path or name |
|---|---|
| Background helper | LaunchAgent `dev.borrowed-hands.helper`, `~/Library/LaunchAgents/dev.borrowed-hands.helper.plist` |
| Pairing, device key, grants, job history | `~/Library/Application Support/BorrowedHands` |
| Build runner job | LaunchDaemon `com.borrowedhands.runner` |
| Build sandbox | `/opt/borrowed-hands` |
| Hidden build user and group | `bh-builder` (uid and gid 470), home `/Users/Shared/.bh-builder-home` |
| Sudo rule | `/etc/sudoers.d/borrowed-hands` (and setup's staging file `/etc/sudoers.d/.borrowed-hands.new`) |
| Logs (kept) | `~/Library/Logs/BorrowedHands` |

Run `uv tool uninstall` last: `bh uninstall` uses the teardown script that ships inside the helper.
`uv tool update-shell` added `~/.local/bin` to your shell profile; leave it (other uv tools use it) or remove that
line by hand.

### On the Hermes side

1. In your DM with the bot, `/hands status`, then `/hands unpair <name or id>` for each Mac. A connected Mac is
   disconnected and refused from then on.
2. **Railway:** delete the service and its volume. Deleting only the plugin directory does not work: the startup
   script copies and enables the plugin again on the next boot. Deleting the volume erases the server key,
   pairings and job history.
3. **Existing Hermes:** stop the gateway, run `hermes plugins remove borrowed-hands`, remove the
   `borrowed-hands` entry from `config.yaml` if it is still there, then delete only this plugin's data directory.
   Find it with `ls "$HERMES_HOME/plugin-data"`: on Hermes v0.22.7 it is named `agent-plugin-borrowed-hands-`
   followed by eight hex characters, and it holds `server_ed25519.key`, its lock file and `bh.sqlite3`. Do not
   delete the whole `plugin-data` directory; other plugins keep their data there. Remove the reverse-proxy route to
   port 8790, then start the gateway again.
4. Revoke any bot token or provider key you created only for this deployment.

## If the server key leaks

`/hands rotate-key` is not available in 0.1. Re-pair instead:

1. **Unpair every Mac first.** `/hands status` lists them. Run `bh unpair` on each Mac while the gateway still
   runs, so the server records it. A Mac that has not unpaired keeps trusting the leaked key. Note the current
   key id from the gateway log line `borrowed_hands: listener up on ... (server key <id>)`.
2. **Move the plugin's data aside** (rename it rather than delete it, so the audit log in `bh.sqlite3` is kept).
   Its directory is `$HERMES_HOME/plugin-data/agent-plugin-borrowed-hands-<eight hex characters>`. Add a suffix
   with the date and time, for example `.leaked-20261109T1530Z`, so an earlier recovery's copy is never replaced.
   - Existing Hermes: stop the gateway, rename the directory, start the gateway.
   - Railway: the gateway is the container's main process, so a `railway ssh` shell cannot stop it, and
     renaming the directory under a running gateway can break its open database. Rename it at startup instead,
     before the gateway starts. Railway never runs two deployments of a service with a volume at the same time,
     so the old gateway has stopped by then. In the service settings, set the custom start command to the line
     below, with `L=` set to a new suffix for this recovery:

     ```sh
     /bin/sh -c "L=leaked-20261109T1530Z; P=/opt/data/plugin-data/agent-plugin-borrowed-hands-; for d in $P????????; do [ -d $d ] || continue; if [ -e $d.$L ]; then [ -e $d/server_ed25519.key ] || continue; a=$(cat $d/server_ed25519.key) && b=$(cat $d.$L/server_ed25519.key) && [ X$a != X ] && [ X$b != X ] && [ X$a != X$b ] || { echo 'bh-recovery: cannot confirm the live key differs from the moved key; not starting' >&2; exit 1; }; else mv $d $d.$L || { echo 'bh-recovery: rename failed; not starting' >&2; exit 1; }; fi; done; ls -d $P????????.$L >/dev/null 2>&1 || { echo 'bh-recovery: no plugin data was moved; not starting' >&2; exit 1; }; exec /opt/bh/bh-start.sh gateway run"
     ```

     and deploy. The command refuses to start the gateway if the rename fails, if there was nothing to move, or
     if a copy with this suffix exists and it cannot read both keys and confirm they differ. A restart after the first successful boot keeps the
     new key, because the copy with this suffix already exists. For the same reason, a suffix reused from an
     earlier recovery looks like a restart and moves nothing; the key id check in step 3 catches that. Remove
     the custom start command right after that first successful boot. **This recipe has not been tested on
     Railway.** It was run only locally, against a fake `/opt/data` with a stand-in for the startup script, so
     Railway's handling of the command and the full container start are unverified. Watch the deploy logs. The
     startup script keeps what is in `plugin-data`: it never replaces or deletes the plugin's key or database.
3. **Check the key changed, then pair again.** On start the plugin creates a new key and an empty database. The
   `listener up` log line must show a different server key id from the one you noted; if it does not, stop and
   do not pair. Then send `/hands pair` in your DM and run `bh setup` on each Mac. Pairing resets grants, so
   grant again. Even in strict mode the new key can be attached to a chat until it is older than the configured
   recency window (600 seconds by default).

## Troubleshooting

| You see | Do this |
|---|---|
| `zsh: command not found: bh` | run `uv tool update-shell` and open a new terminal |
| `Xcode is not installed at /Applications/Xcode.app` | install Xcode from the App Store (or move your copy to that path) and open it once |
| `the license of the Xcode at /Applications/Xcode.app has not been accepted` | the exact command setup prints: `sudo /usr/bin/env DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer /Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild -license accept` |
| `Note: builds use /Applications/Xcode.app; xcode-select points at ...` | nothing to do; builds ignore the global selection |
| Hermes asks which branch | the repository has several `bh/` branches; name one, or give a full commit SHA |
| `pairing code rejected; run /hands pair again` | the code expired or was used; send `/hands pair` for a fresh one |
| `cannot reach <host>` | check the gateway's health URL; a redeploy causes a short outage |
| `Setup stopped at [n/7] ...` | fix the error shown and run `bh setup` again; it skips finished steps |
| a build says `sudo asked for a password for the Borrowed Hands build command ...` | another sudo rule makes the build command need a password. `bh selftest` cannot see this (it checks that sudo permits the commands, not that they are password-free). Remove or reorder that rule, then run `bh selftest` |
| selftest `FAIL sudo rule: installed` | run `bh setup` again; it compares the installed rule with the exact one and replaces it if it differs |
| setup or selftest says another sudo rule overrides ours (`FAIL sudo rule: permitted`) | a later rule in `/etc/sudoers` or `/etc/sudoers.d` matches `/opt/borrowed-hands/bin/bh-dispatch`; change that rule (Borrowed Hands never edits other sudo rules) |
| a build fails with `not_on_bh_branch` | push the commit to a `bh/` branch |
| a build fails with `unsupported_dependencies` | vendor remote Swift packages as local path packages |
| to re-pair a Mac | `bh unpair`, then `bh setup`, then re-add grants (pairing again resets them) |

`bh selftest` checks everything on the Mac with one PASS/FAIL line per item and the fix for each failure.
`bh selftest --build demo` also runs one real local build. Logs are in `~/Library/Logs/BorrowedHands/helper.log`.
