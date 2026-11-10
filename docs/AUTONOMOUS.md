# Hermes edits, the Mac compiles, Hermes opens the pull request

GitHub operations use Hermes's existing Git tooling and a repository-scoped credential. Borrowed Hands handles Mac discovery, locally authorized Xcode execution, and structured build results.

This page is for operators who want a Hermes agent to change a public repository itself, have Borrowed Hands
compile the change on a paired Mac, and open a pull request once it builds. Borrowed Hands adds no tool for this.
The agent uses Hermes's own file and terminal tools to edit, push and open the pull request, and uses
`hands_xcode_build` and `hands_job` only to compile and to check, just before the pull request, that the branch
still points at the commit that compiled.

This workflow ran end to end on 2026-11-10 against a public demo repository: a real model, asked from Telegram,
pushed to a `bh/hermes/` branch, the first build failed with a compiler error, the agent fixed it from the
diagnostics, the second build passed, and it opened a pull request whose head was the commit the Mac built. In
that run the agent built by commit with no branch and did not call `check_pr_ready`: it was following a skill it
had written for itself, which said a succeeded build was enough. Asked afterwards, `check_pr_ready` answered
`ready: false` (`no_branch`) for that build. A later run on the same day built by branch and, by the agent's own
report, got `ready: true` before it opened the pull request; the head again equalled the built commit. The
bundled skill exists because of this. The check is advice the agent must ask for: Borrowed Hands holds no GitHub
credential and cannot stop a pull request. A cancelled or stale build being refused is covered by tests only.

## How a run goes

1. Hermes clones the public repository in its own cloud environment, creates a task branch such as
   `bh/hermes/fix-title`, edits, commits and pushes it, and notes the pushed SHA. One writer per task branch: the
   agent does not push to it again, rebase it or force-push it while a build of it is waiting or running.
2. Hermes calls `hands_xcode_build` with `branch` set to that branch, so the approval prompt shows it. It passes
   `commit` (the full pushed SHA) only together with `branch`; the prompt then shows the branch as "requested,
   unchecked" and the plugin does not read the branch. Without `commit`, the plugin reads the branch tip once,
   before the approval prompt. Either way the prompt names the commit, and the signed job builds exactly that
   commit even if the branch moves afterwards.
3. The result's `provenance` gives the commit the signed job builds, the branch the call named, and how the commit
   was chosen. Hermes compares `provenance.commit` with the SHA it pushed:

   ```json
   {"ok": true, "job_id": "<uuid>", "state": "failed",
    "provenance": {"commit": "<40-hex SHA>", "branch": "bh/hermes/fix-title",
                   "commit_source": "latest on bh/hermes/fix-title"},
    "result": {"status": "failed", "error_count": 1, "errors": [{"file": "...", "line": 12, "message": "..."}]}}
   ```

   `commit_source` is `latest on <branch>`, `explicit commit`, or `explicit commit; branch not checked` (a call
   that named both a commit and a branch builds the commit and does not compare it with the branch).
   `hands_job` status, wait and cancel views of the job return the same `provenance`, including after a gateway
   restart (an error reply such as an unconfirmed cancel has none). `branch` and `commit_source` record what the
   call asked for; they are plugin metadata, not signed proof that the commit is on that branch.
4. Hermes reads `state`, not `ok`: a compile failure is `ok: true, state: "failed"`. A job that is still running
   is followed with `hands_job` `action=wait`, which does not start another build.
5. On a failure, Hermes fixes what the diagnostics show, pushes again and builds again. The prompt section the
   plugin adds asks it to stop after three builds, when the same errors repeat, or when the cause is not the code
   (grant, dependency, service), and tell the user.
6. Before opening the pull request, Hermes calls `hands_job` with `check_pr_ready: true` for the build. The
   plugin answers with its own verdict, computed at that moment:

   ```json
   {"pr_ready": {"ready": true, "reason": "ready", "commit": "<40-hex SHA>", "branch": "bh/hermes/fix-title",
                 "tip": "<40-hex SHA>", "checked_at": "2026-11-09T18:04:11Z", "checked_at_ms": 1794247451000,
                 "confirmed_at": "2026-11-09T18:04:12Z", "confirmed_at_ms": 1794247452000,
                 "repo_url": "https://github.com/OWNER/REPO", "open_from": "bh/hermes/fix-title",
                 "pr_head_must_equal": "<40-hex SHA>", "message": "Ready as of ... point-in-time check ..."},
    "ok": true, "job_id": "<uuid>", "state": "succeeded", "provenance": {"commit": "<40-hex SHA>", ...},
    "note": "Only pr_ready above is the verdict. The Mac's diagnostics (result, error) are left out ..."}
   ```

   The check response leaves out the Mac's `result` and `error`, so nothing the build printed sits beside the
   verdict; only this top-level `pr_ready` counts. A plain `hands_job` status call shows the diagnostics.

   `ready` is true only if the job's Mac reported the build succeeded, its provenance names a branch, and the
   plugin's own fresh, anonymous read of the granted repository's branch list (the same hardened reader it uses to
   resolve a build) shows that branch's tip equal to the commit the job's signed contract named. Otherwise `ready` is false and
   `reason` says why: `build_not_succeeded` (failed, cancelled, expired, refused or still running; `state` says
   which; a result that lists errors counts as not succeeded), `superseded` (a newer build of the same commit,
   workspace and repository finished without success, on any Mac or destination, so this older success is stale),
   `no_branch` (the build named no branch), `branch_moved` (`tip` is the commit the branch points at now),
   `branch_missing`, `refs_unavailable` (the read failed or timed out; `refs_reason`), `repo_not_granted` (the
   job's Mac was unpaired, or the caps it last advertised do not grant that repository), `grant_disallows_build`
   (those caps do not allow the scheme, configuration or destination built), `repo_url_unknown`, `no_provenance`, `not_a_build` or `check_failed`. Any error is "not ready". The plugin reads only the repository remembered for that build's
   signed contract, and only while the caps the job's Mac last advertised grant it (an offline Mac's last caps
   count); the call cannot name another URL. After the
   read it checks the job, the grant and the URL again and requires the same result, including the grant's
   allowed values and expiry, so a Mac unpaired or a grant revoked, expired or replaced while the read was in
   flight is not ready. A grant revoked and re-added unchanged during the read cannot be seen: caps carry no
   grant generation. Without
   `check_pr_ready` no job view makes a network call. The verdict is not stored: every call reads again.

   Only on `ready: true` does Hermes open the pull request, at once, with `head` set to that branch (see
   [Commands the agent uses](#commands-the-agent-uses)), and say that it was a compile check only (no tests, no
   app run). It is a point-in-time check: the branch can move after it, and Borrowed Hands cannot see pull
   requests. So right after opening, Hermes reads the pull request's `head.sha` and compares it with
   `provenance.commit`. If they differ, it closes the pull request (or stops) and tells the user. On any other
   reason it builds again or stops. The head comparison detects a pull request on unbuilt code right after it is
   opened; it does not prevent one, and neither the check nor the one-writer rule stops the grant from changing
   afterwards.

Each build asks for approval. **Allow Once** approves one build, so the next push asks again. **Allow Session** or
**Always Allow** covers later commits and branches for the same Mac, workspace, scheme, configuration and
destination, and never a broader scope. **Always Allow** is stored for the whole Hermes deployment, so it covers
every chat on it. The approval reason says this right after the commit and the Mac, so a chat platform that
shortens long reasons (Hermes cuts from the end) still shows all three; only the settings at the end can be cut.
For autonomous runs, prefer Allow Once. A grant with `--confirm` also asks on the Mac for every build, whatever was
answered in chat.

## Starting a new app

The Hermes host has no Xcode, and a hand-written `project.pbxproj` is easy to get wrong. The plugin ships a
known-good project for this: `templates/swiftui-app/` and the script `templates/new_swiftui_app.py` (Python 3
standard library only) in the plugin directory, which an installed plugin keeps at
`$HERMES_HOME/plugins/borrowed-hands/templates/`. If the agent's terminal runs on a backend that cannot see the
plugin directory, the prompt tells the agent to stop and tell the user rather than look for a copy. The operator
can then supply the same files from the canonical repository, https://github.com/alext8900/borrowed-hands, under
`borrowed-hands/templates/`, taken at the commit of the installed plugin version (the catalog pin), not from a
branch tip or any other copy.

The template is one multiplatform SwiftUI app target with a shared scheme, for macOS (arm64) and the iOS
Simulator: no signing team, Swift packages, Run Script phases, asset catalog or Swift macros. Its app folder is an
Xcode 16+ file-system-synchronized group, so the agent adds or removes `.swift` files in `<AppName>/` without
touching the project file. The script substitutes the app name and bundle identifier, refuses names that are not
UpperCamelCase ASCII letters and digits (no path separators, dots or shell characters) or that shadow a Swift
module, produces the same bytes for the same arguments, and never overwrites a file. If something fails after it
has created anything (a file appears meanwhile, a disk or permission error), it stops, deletes nothing, exits 1 and
prints JSON with `written` (completed files), `incomplete` (created but not completed, such as a truncated
`project.pbxproj`) and `created_dirs`. These are a history of what this run created by name at the time, not proof
of what is at those paths now, and must not be used as authority to delete anything. The report is delivered
when stdout is usable; if it is not, the exit code is still correct (1 for a stop, and 1 for a success that could
not be reported) and nothing is deleted.

1. The user creates a new, empty public repository. The token in this page covers one existing repository and
   cannot create one. On the Railway image the user also points `BH_GITHUB_REPO` and the token at the new
   repository and redeploys, or the agent's push has no credential.
   The Mac fetches public repositories only.
2. In a clone of it, the agent runs

   ```sh
   python3 "$HERMES_HOME/plugins/borrowed-hands/templates/new_swiftui_app.py" \
     --name HelloApp --bundle-id com.example.helloapp \
     --repo https://github.com/OWNER/REPO.git .
   ```

   It prints JSON with `project`, `scheme`, `destinations`, `workspace`, the files written, `grant_command`
   and `grant_note`.
3. The agent commits and pushes to its task branch, for example `bh/hermes/new-app`.
4. The agent gives the user `grant_command` and `grant_note` to use on their Mac, and waits. The command looks
   like

   ```sh
   bh grant add xcode.build --workspace owner-repo-helloapp-52714e --repo https://github.com/OWNER/REPO.git \
     --project HelloApp.xcodeproj --scheme HelloApp --configuration Debug \
     --destination 'platform=macOS,arch=arm64' --destination 'generic/platform=iOS Simulator'
   ```

   A new repository, project or scheme needs a new grant, and only the Mac's owner can add one. The agent cannot
   grant itself anything; until the owner runs the command, a build returns `needs_grant`.

   `bh grant add` never changes an existing grant: with a name that already exists it changes nothing and prints
   what that grant allows, unless the owner adds `--replace` (which prints what changes). The workspace name is
   derived from the repository and the project (owner, name, app and a short hash) to make accidental reuse
   unlikely. Another `--workspace` name adds a second, separate grant with only the settings in the command:
   pass `--confirm` and `--expires` if that one should have them too. `grant_note` says this to the owner and the
   agent relays it.
5. The agent builds as in [How a run goes](#how-a-run-goes), passing `workspace_id` (the printed `workspace`),
   `repo_url` (the Mac shares only a fingerprint of the repository, so the first build of a workspace names it),
   `branch`, and `destination` set to one of the two granted destinations (the grant allows both, so a build that
   names neither returns `needs_input`).

## Where the GitHub credential lives

GitHub write access belongs to the Hermes deployment's own Git setup, never to Borrowed Hands:

- The Borrowed Hands plugin never reads, stores or sends a GitHub credential. Its only network call of its own is
  the anonymous read of a granted repository's branch list, made to resolve a build with no commit and for a
  `check_pr_ready` check (see the plugin README's disclosures).
- The Mac never receives one. The runner fetches the public repository with credential helpers, global Git
  configuration and prompts turned off, and builds only commits reachable from `bh/*`.
- Use a fine-grained personal access token limited to the one repository, with **Contents: read and write** and
  **Pull requests: read and write**, owned by an account with write access to that repository. Do not use a
  classic token with `repo` or `workflow` scope. Do not let the agent edit `.github/workflows`; that needs more
  permission than this workflow should have.
- A branch the agent pushes, and a pull request from it, run the repository's GitHub Actions workflows with that
  repository's secrets, and the agent can change what they execute without touching a workflow file: use a
  repository whose Actions hold no secrets, or keep secret-using workflows to `main` and protected environments.
- Contents write is not limited to `bh/*` branches. Protect `main`, and also the `bh/*` branches people build
  from (for example a ruleset for `bh/**` that excludes `bh/hermes/**`), so the agent's token can push only its
  own task branches. A Session-approved "build my demo project" builds whatever the tip of such a branch is, so a
  token that can move it can choose what gets built without a new prompt. Configure the ruleset with no bypass
  actors (or, with classic branch protection, turn on "Do not allow bypassing the above settings"): the token acts
  as its owner, and on a personal repository the owner is an administrator who would otherwise walk through the
  rule. Nothing can tell the token from its owner, so an empty bypass list also blocks the owner's own pushes and
  merges to those branches; the owner disables the ruleset to change them and turns it back on afterwards.
- A rule that only requires a pull request on `main` does not stop the token: merging a pull request needs only
  Contents write, which it has, so it can open and merge its own. Use restrictions that refuse updates outright: a
  ruleset targeting all branches except `bh/hermes/**` with Restrict creations, Restrict updates, Restrict
  deletions and Block force pushes, no bypass actors. Branch rules do not cover tags, and Contents write can
  create tags and releases: add a tag ruleset with the same three restrictions.
- Keep the token out of this repository, the plugin settings, a chat, the Mac and every process environment. On
  the Railway image it arrives as the sealed `BH_GITHUB_TOKEN` service variable, which the start script writes to
  two files and removes from the environment before Hermes starts (see
  [below](#railway-how-this-repositorys-image-delivers-the-token)).

Recommended for a demo or any short autonomous run:

- A dedicated, short-lived fine-grained token, created for this run only, with a near expiry date.
- One repository, and only **Contents** and **Pull requests** write (plus the read access they imply).
- Stored only in the two credential files below, never in prompts, source, logs, build results or an
  environment variable the agent's commands inherit.
- Pushes limited to `bh/hermes/*` by repository rules where feasible (the ruleset above, no bypass actors).
- Revoked as soon as the run is over.

### The agent's shell can read the Borrowed Hands key

Borrowed Hands does not hold the GitHub credential, but the reverse is not true. Hermes runs the agent's terminal
commands with its `local` backend unless configured otherwise (`TERMINAL_ENV`, default `local`: Hermes v0.22.7
`tools/terminal_tool.py:4-6`, `:452`, `:817`), and file and code-execution tools share that environment
(`tools/code_execution_tool.py:406-432`). In this repository's Railway image the gateway runs as the `hermes` user
with `HOME=/opt/data`, which is `HERMES_HOME` (Hermes `docker/main-wrapper.sh:31`, `:65`). So every command the
agent runs is the same user that owns this plugin's data directory, including the server signing key
(`server_ed25519.key`) and `bh.sqlite3`. Hermes's dangerous-command checks do not cover those files, and a plain
`git push` is not gated. Anything that can steer the agent (text in the repository, compiler output, a crafted
branch name) could have it copy the key into the checkout and push it, using the very token this page asks you to
provision.

What a stolen key buys: its holder can sign contracts and frames for every paired Mac. To deliver them it also
needs the Mac's connection (the Mac dials only its pinned `wss://` endpoint, verifies TLS and checks the pinned
server key), and the Mac still enforces its grants, so at most it gets builds of granted `bh/*` commits and
system info until the Macs are re-paired. It does not give a shell on the Mac. See the plugin README's
[Server key](../borrowed-hands/README.md#limits-and-residual-risks) paragraph, which also covers the chat route
below. The plugin cannot hide its key from the process it runs in.

Mitigations:

- Give the token the narrowest scope above: one repository, Contents and Pull requests only.
- Where you can, run the agent's tools on a non-local terminal backend, so its commands do not run as the
  gateway user on the gateway host. Hermes v0.22.7 supports `docker`, `modal`, `daytona`, `vercel_sandbox` and
  `ssh` besides `local` (`tools/terminal_tool.py:4-6`), selected with `terminal.backend` in `config.yaml`
  (default `local`, `hermes_cli/config_defaults.py:285-286`) or `TERMINAL_ENV`. Each
  needs its own infrastructure (a Docker daemon, a sandbox account or an SSH host). Put the GitHub login on that
  backend, not on the gateway. Alternatively, run the GitHub-writing agent on a separate Hermes deployment that
  does not have Borrowed Hands installed and does not hold its key.
  - The backend moves the agent's tools, not everything the gateway does. The gateway still attaches files from
    its own filesystem to chat replies, checking the host path before any sandbox
    (`gateway/platforms/base.py:1104-1114`). In Hermes's default media mode any existing regular file outside its
    denylist is sent (`:1055-1095`), and the denylist (system paths, `~/.ssh`-style directories, named
    `HERMES_HOME` credential files: `:767-773`, `:784-791`) does not include `plugin-data`. A reply with a
    `MEDIA:<path>` tag attaches that file; for the key, a reply that just names its absolute path is enough. So a
    steered reply could post the key or the database into the chat (to that chat's members, not to GitHub), with
    no tool call and no approval. `HERMES_MEDIA_DELIVERY_STRICT=1` (`:748-750`) reduces this without closing it:
    it limits attachments to Hermes's own caches, `HERMES_MEDIA_ALLOW_DIRS`, and files modified within a recency
    window (600 seconds by default, `:763`). The exceptions are as the plugin README lists them: the key during the
    window after it is created (including the new key made during recovery) or restored with a fresh modification
    time, any allowed directory that contains it, and `bh.sqlite3` with its `-wal`/`-shm` files within the window
    after a write.
  - With the `docker` backend, never mount `HERMES_HOME`, `plugin-data` or a directory containing them into the
    sandbox: not through `terminal.docker_volumes`, and not through `docker_mount_cwd_to_workspace` with a working
    directory above them (`tools/terminal_tool_backends.py:63`, `:70`; `tools/environments/docker.py:850-914`).
    None of Hermes's own mounts covers `plugin-data`: the read-only skills, credential-file and cache mounts
    (`docker.py:545-572`), and in persistent mode the writable sandbox home and workspace under
    `HERMES_HOME/sandboxes/docker/<task>/` (`docker.py:900-914`).
- Prefer Allow Once for builds the agent asks for, and protect `main` and the people's `bh/*` branches as above.
- After any suspected exposure, follow [If the server key leaks](QUICKSTART.md#if-the-server-key-leaks) (also in
  the plugin README): `bh unpair` on every Mac first, while the gateway still runs; then move the plugin's data
  aside with a dated suffix (never under a running gateway; it keeps the audit log in `bh.sqlite3`); then check
  the new server key id, send `/hands pair` in a DM for a fresh pairing string and run `bh setup` on each Mac,
  granting again. Unpairing comes first because `bh unpair` removes the Mac's pinned server key, grants and
  capabilities at once, while `bh grant revoke <workspace>` stops builds only and leaves other capabilities on.
  For this workflow, also revoke the GitHub token right after the Macs are unpaired.

**Hermes strips GitHub tokens from the agent's commands.** Checked in the pinned Hermes v0.22.7 source
(`b200f5bc`): `GH_TOKEN` is on the static credential blocklist (`tools/environments/local_env_policy.py:38`);
`GITHUB_TOKEN` is a registered tool credential (`hermes_cli/config_defaults.py:2916`), and tool credentials join
that blocklist (`local_env_policy.py:63-84`); both are Tier-1 `_ALWAYS_STRIP_KEYS`, removed from every spawned
subprocess (`local_env_policy.py:415-417`). The terminal's command environment is built through that filter
(`tools/environments/local.py:264`, `:311`, `:776`), and `terminal.env_passthrough` refuses blocklisted names
(`tools/env_passthrough.py:46-60`). So a variable named `GITHUB_TOKEN` or `GH_TOKEN` never reaches the agent's
`git`. The filter is by name only: a variable with any other name (`GITHUB_PAT`, `BH_DEMO_GIT_TOKEN`, ...) reaches
every command the agent runs, every repository script it starts, and the session snapshot Hermes writes after each
command. Do not hand the token over either way. The Railway image in this repository delivers it as files
instead; see the next section.

The pinned image installs `git` and `curl` but not `gh` (Hermes `Dockerfile:81`), so on v0.22.7 pushes go through
Git with a credential helper and the pull request through the GitHub REST API with `curl`. Do not use the bundled
GitHub skill's own commands for this: its REST route reads the token from `.env` or a script path that does not
exist in v0.22.7, puts the token in `curl`'s argument list (`-H "Authorization: token $GITHUB_TOKEN"`) and exports
it, and its auth notes suggest a token in the remote URL and classic `repo`/`workflow` scopes. The commands below
replace it. This is a source-level check; run the preflight below on the deployment you will use.

## Railway: how this repository's image delivers the token

Two service variables:

- `BH_GITHUB_TOKEN`: the fine-grained token. Add it, then **Seal** it from the variable's menu (a sealed value is
  never shown again in the UI or returned by the API or CLI). Do not name it `GITHUB_TOKEN` or `GH_TOKEN`.
- `BH_GITHUB_REPO`: the repository as `owner/name`, exact case, no URL, no `.git`. Not secret.

At every container start, before anything else, `deploy/railway/bh-start.sh` takes both variables out of the
environment (Hermes, s6 and every agent command start without them) and passes them on stdin to
`deploy/railway/bh-github-creds.py`, which writes the files below. This holds while the image's own entrypoint
runs: do not set a custom start command on the service. A shell you open into the container yourself still shows
both variables.

- `/etc/bh-github/creds/git-credentials` and `/etc/bh-github/creds/api-header`: mode 0600, owned by `hermes`, in
  a `hermes` 0700 directory whose parent `/etc/bh-github` is root's. This is the container filesystem, not the
  volume: the files are written again by every start, vanish with the container, and are not in `hermes backup`
  archives.
- `/etc/bh-github/gitconfig`: root-owned, no secret. It offers the credential only for
  `https://github.com/owner/name` and `…/name.git`, through a read-only helper. Git reads it only from the
  `GIT_CONFIG_*` variables that the start script hands to Hermes (nothing in the system config names it); git
  reads those after the user's own files, so a credential helper the agent later adds to a git config file never
  receives the token. One passed with `git -c credential.helper=…` or through `GIT_CONFIG_*` still does.

A token that is not a fine-grained token, a repository value that is not a plain `owner/name` (or contains a
GitHub token or a token prefix such as `ghp_`), a symlink or a wrong owner in that directory: nothing is written, any earlier credential is removed, the
log line names the variable but never its value, and the gateway still starts. Every start also removes a
credential left on the volume by an earlier image (`/opt/data/.config/bh-github`), if it is still laid out as
that image wrote it; otherwise the log says what was left, except files of other names in that directory, which
are kept without a log line.

The deploy log shows one of these lines:

- `github credentials: configured for the repository named in BH_GITHUB_REPO`
- `github credentials: refused: <reason>; starting without GitHub credentials`
- `github credentials: BH_GITHUB_TOKEN not set; no credential installed` (every start without the token,
  including the first start of a new container after you delete the variable)
- `github credentials: BH_GITHUB_TOKEN not set; removed the stored credential` (only when the same container
  restarts and still had the files)
- `github credentials: removed a credential from the old location $HERMES_HOME/.config/bh-github`
- `github credentials: warning: <path> holds a github.com credential this script did not write and does not
  remove; delete it and revoke the token at GitHub` (a copy outside the managed files, for example from an
  agent that ran git with `-c credential.helper=store`)
- `github credentials: helper failed; git config and <dir>/creds removed, but the old location … was NOT
  checked and may still hold an old token`: the script that writes the files failed. Hermes starts without a
  credential. Check the old location yourself, and revoke the token at GitHub if in doubt.

### What these files do not protect against

- The agent runs as `hermes`, the user that owns the files. It can read the token (it must, for `git` and
  `curl` to use it). Hermes masks the token's normal shape in tool output (while `security.redact_secrets` is on, the
  default); an encoded copy passes. Treat the
  token as readable by the agent and keep it narrow and short-lived.
- Git gets the credential only where the `GIT_CONFIG_*` variables are present. Hermes's `execute_code` tool and
  stdio MCP servers start their children without them, and so does anything that clears its environment
  (`env -i`): git there has no credential and a push fails. Run git from the terminal tool.
- A helper named on the command line (`git -c credential.helper=store`) or added to `GIT_CONFIG_*` is applied
  after the reset and receives the token; it can store a copy. The next start reports a copy at git's default
  store paths, not one a helper wrote elsewhere (`store --file=…`).
- `api-header` is not tied to a host. curl sends it to whatever URL the command names, and with
  `--location-trusted` to redirect targets too.
- Hermes refuses to attach files under `/etc` to chat replies in both media modes (`gateway/platforms/base.py`
  `:767-768`, `:1055-1091`, symlinks resolved first), unless `HERMES_MEDIA_ALLOW_DIRS` names a directory that
  contains them (that allowlist is checked first). A copy the agent makes elsewhere is not covered.

### Instructions for the agent

Put these in the task prompt or the agent's instructions:

- A Git credential helper for `https://github.com/OWNER/REPO` is already installed. Do not run
  `git config … credential.helper` or `git -c credential.helper=…`, do not change `credential.useHttpPath`, do not run `gh auth` or the GitHub
  skill's authentication setup, and do not unset or change `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_*` or
  `GIT_CONFIG_VALUE_*`.
- Run `git` only from the terminal tool, never from an `execute_code` script or an MCP server: only the terminal
  tool has the credential.
- Clone and push with the plain URL `https://github.com/OWNER/REPO.git`, to branches under `bh/hermes/`. Never
  put a token in a URL or a command, never `export` one, never set `GIT_TRACE*`, `GIT_CURL_VERBOSE` or
  `GIT_TRACE_REDACT=0`.
- Open the pull request only with the commands under [Commands the agent uses](#commands-the-agent-uses): the
  title and body go in a JSON file, never on the command line.
- Use `-H @/etc/bh-github/creds/api-header` only with URLs that start with `https://api.github.com/`. Never add
  `-v`, `--trace`, `-L`, `--location-trusted`, or a curl config file.
- Never `cat`, copy, move, encode, attach or commit anything under `/etc/bh-github/`.

### Commands the agent uses

Give the agent these, in place of the bundled GitHub skill's. Replace `OWNER/REPO` and `<task>`; `<task>` uses
only letters, digits, `.`, `_` and `-`. The token never appears in a command line, an environment variable or a
URL: Git reads it through the helper, `curl` reads the header from the file (`-H @file`).

Free text never goes on a command line. Inside double quotes the shell still runs backticks and `$(...)`, and an
apostrophe ends a single-quoted string, so a commit message or title copied from an issue, a file or compiler
output could run commands as the user that can read the token. The agent writes the commit message and the pull
request's JSON body with its file tool, into a directory outside the clone (`<dir>` below), and the commands only
name those files:

- `<dir>/message.txt`: the commit message, as plain text.
- `<dir>/body.json`: a JSON object written as JSON (quotes and backslashes escaped by JSON's rules, not the
  shell's): `{"title": "<title>", "head": "bh/hermes/<task>", "base": "main", "body": "Compile-checked by Borrowed
  Hands at <provenance.commit> (compile only: no tests, no app run)."}`

```sh
# clone with the plain URL (never put a token in it), branch, commit with the message from the file
git clone https://github.com/OWNER/REPO.git && cd REPO
git switch -c bh/hermes/<task>
git config user.name 'Hermes (Borrowed Hands)' && git config user.email '<an address the owner chooses>'
git commit -a -F <dir>/message.txt
GIT_TERMINAL_PROMPT=0 git push origin HEAD:refs/heads/bh/hermes/<task>
git rev-parse HEAD                      # the pushed SHA, to compare with provenance.commit

# after hands_job check_pr_ready=true returned ready: true, open the pull request from that branch
curl -sS --fail-with-body -X POST -H @/etc/bh-github/creds/api-header \
  -H 'Accept: application/vnd.github+json' \
  https://api.github.com/repos/OWNER/REPO/pulls \
  --data-binary @<dir>/body.json -o <dir>/pr.json
python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(d["number"], d["head"]["sha"])' <dir>/pr.json

# if head.sha is not provenance.commit: close it (a fixed body, no free text) and tell the user
curl -sS --fail-with-body -X PATCH -H @/etc/bh-github/creds/api-header \
  -H 'Accept: application/vnd.github+json' \
  https://api.github.com/repos/OWNER/REPO/pulls/<number> -d '{"state":"closed"}' -o <dir>/close.json
```

The replies go to `<dir>` too, outside the clone, so a later `git add -A` cannot stage them.

`--fail-with-body` makes a request GitHub rejects exit non-zero (curl 7.76 or newer; the image is Debian
trixie): check the exit status, and treat a failed close as "stop and tell the user". Never `export` the token,
`cat` either file, add `-v` to `curl`, or set `GIT_TRACE`, `GIT_CURL_VERBOSE` or `GIT_TRACE_CURL`. Hermes masks
token-shaped strings in command output, but only by shape: an encoded copy passes through. `curl` and `python3` are in the same package line of the image as `git` (Hermes `Dockerfile:81`); the
clone, push and open commands ran on the live container on 2026-11-10; the close command has not been run there.

## Credential preflight

Run these through the agent's own terminal tool (ask Hermes to run them), not in a container shell: the
credential is named only in the environment Hermes hands its terminal tool, so a shell you open yourself has no
git credential and check 3 fails there on a healthy deployment. Do not add a system-level include to "fix" that;
it would hand the token to other child processes. Replace `OWNER/REPO` with the repository. None of them prints the
token; if one does, revoke the token and fix the setup.

```sh
# 1. No process this user can see holds a GitHub token (prints a process id and a NAME, never a value). Names
#    the deployment sets on purpose for the gateway, such as TELEGRAM_BOT_TOKEN, are listed and are fine. A
#    name starting BH_GITHUB_, GITHUB_ or GH_ is the failure. So is any other name without "token" in it: it
#    is listed because its value is shaped like a GitHub token. The output does not say which of the two
#    matched, so check any name you do not recognise: a GitHub token under a name of your own that contains
#    "token" looks the same here as a harmless one.
python3 - <<'EOF'
import glob, re
NAME = re.compile(rb"token|github|^gh_|_pat$|^pat$", re.I)
VALUE = re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")
for path in sorted(glob.glob("/proc/[0-9]*/environ")):
    try:
        blob = open(path, "rb").read()
    except OSError:
        continue
    for item in blob.split(b"\0"):
        name, sep, value = item.partition(b"=")
        if sep and (NAME.search(name) or VALUE.search(value)):
            print(path.split("/")[2], name.decode("ascii", "replace"))
EOF
#    and the git include is present in this environment (expect 3)
python3 -c 'import os; print(sum(k.startswith("GIT_CONFIG_") for k in os.environ))'
# 2. Files in place, owner-only (expect hermes 700 for the directory, hermes 600 for both files)
stat -c '%U %a %n' /etc/bh-github/creds /etc/bh-github/creds/*
# 3. Git offers the credential to this repository only (expect "scoped", then "other: none")
git config --get-urlmatch credential.helper https://github.com/OWNER/REPO.git >/dev/null && echo scoped
git config --get-urlmatch credential.helper https://github.com/OWNER/other.git >/dev/null || echo 'other: none'
# 4. The API accepts the token: status line and expiry date only
curl -sS -o /dev/null -D - -H @/etc/bh-github/creds/api-header https://api.github.com/repos/OWNER/REPO \
  | grep -i -E '^(HTTP/|github-authentication-token-expiration:)'
# 5. Git authenticates for a push without sending anything (expect exit 0 and no password prompt)
cd "$(mktemp -d)" && git init -q && git -c user.name=preflight -c user.email=preflight@example.invalid \
  commit -q --allow-empty -m preflight && GIT_TERMINAL_PROMPT=0 git push --dry-run \
  https://github.com/OWNER/REPO.git HEAD:refs/heads/bh/hermes/preflight
# 6. Real pushes, once the rulesets are active: one accepted, two refused, then clean up
git push https://github.com/OWNER/REPO.git HEAD:refs/heads/bh/hermes/preflight     # accepted
git push https://github.com/OWNER/REPO.git HEAD:refs/heads/preflight-denied        # refused by the ruleset
git push https://github.com/OWNER/REPO.git HEAD:refs/heads/bh/preflight-denied     # refused by the ruleset
git push https://github.com/OWNER/REPO.git :refs/heads/bh/hermes/preflight         # delete the test branch
#    If either push that should be refused is accepted, delete that branch too and fix the ruleset before going on.
```

`git ls-remote` is not a credential check: it succeeds anonymously on a public repository. A dry run does not
evaluate repository rules: step 6 is the only one that proves the rulesets.

Step 1 reads each process's environment as `/proc/<pid>/environ` holds it (its starting environment, which a later
`unset` inside that process does not change), split on NUL so a multi-line value cannot spill into the output. It
does not see processes of other users or other containers, variables a process set after it started, files, or a
token stored under a name it does not match and in a form other than GitHub's own `ghp_`/`github_pat_` shapes.
It has not yet been run on the container.

### Rotate and revoke

- Rotate: create the new token, replace the value of `BH_GITHUB_TOKEN` (delete and re-add it, sealed, if the UI
  does not offer an edit), redeploy, check the log line, then revoke the old token at GitHub.
- Revoke: revoke the token at GitHub first (Settings, Developer settings, Fine-grained tokens); that takes effect
  at once. Then delete `BH_GITHUB_TOKEN` and redeploy; the log shows `BH_GITHUB_TOKEN not set; no credential installed`. Revoke at GitHub whatever
  the log says: removing files does not prove that no copy exists.
- Any `hermes backup` archive or volume backup made while an earlier image kept the files on the volume
  (`/opt/data/.config/bh-github`) contains the token. Treat such archives as holding it.

## Limits

- Compile only: no signing, tests, simulator boot or app run.
- Pushing to a `bh/` branch makes every commit reachable from it buildable, including history shared with `main`.
- With Hermes's default local terminal backend, the agent that holds the GitHub token can read and push the
  Borrowed Hands signing key; see [The agent's shell can read the Borrowed Hands
  key](#the-agents-shell-can-read-the-borrowed-hands-key).
- "Latest on a branch" means the tip when the approval prompt was prepared. Comparing `provenance.commit` with
  the pushed SHA detects a mismatch; it does not prevent the mismatched build.
- `check_pr_ready` is a fact about the branch at `checked_at`, read anonymously from the public repository. It
  cannot see pull requests and cannot stop a push that lands after it: the one-writer rule and the agent's
  comparison of the pull request's `head.sha` with `provenance.commit` cover that window. The plugin does not
  enforce that the agent calls it, opens the pull request only on `ready: true`, or closes a mismatched one; the
  prompt section and this page instruct it to.
- A ready verdict rests on the job's own Mac reporting success. The build result carries no proof of which commit
  was compiled; the plugin itself verifies only the branch tip. Grants are the capabilities the Mac last
  advertised: an offline Mac's last caps count, and a grant removed on the Mac while it is offline is not seen.
