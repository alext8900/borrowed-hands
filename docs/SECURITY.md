# Security design

Borrowed Hands lets an AI agent in the cloud run Xcode builds on your Mac. A build runs code from a repository:
build scripts, compiler plugins, anything the project file names. So the design starts from three assumptions.

- The agent can be talked into asking for the wrong thing.
- The server that carries its requests can be taken over.
- The repository being built can be hostile.

This page explains what stops each of those from reaching your account, your files or your network. It also says
where the protection ends. Borrowed Hands is containment with known gaps, and the gaps are listed at the bottom.

The reviews described here were adversarial reviews by AI security reviewers that I ran against frozen checkouts.
No independent human audit has been done.

## The short version

| What could go wrong | What stops it |
|---|---|
| The agent asks to build something you never allowed | Grants live on the Mac, and only the Mac's owner can change them. Approving a request in Hermes cannot add a project, scheme, configuration or destination. |
| A prompt injection in compiler output steers the agent | Compiler output is capped and passed as data. No security decision depends on what the model concludes from it. The Mac checks every request against its grants whatever the model was told. |
| Someone forges or replays a build request | Every request is a contract signed with the server's Ed25519 key, which the Mac pinned at pairing. The contract expires within 10 minutes, and the Mac refuses a reused job id or nonce. |
| The server or its signing key is stolen | The thief gets what your grants already allow: compile-only builds of granted projects, inside the sandbox. `bh unpair` on the Mac ends that within a fixed time, whatever the server does. |
| The repository runs hostile code during the build | The build runs as a separate hidden user, started by the system and not from your login session, in a sandbox with no network and a read allowlist. |
| A build tries to stay behind or exhaust the Mac | The root dispatcher enforces time, disk and memory budgets from outside the build. It kills every process of the build user and deletes the build directories after each job. |
| The agent opens a pull request for code that did not compile | Each result names the commit that was built. A readiness check confirms the build succeeded, the grant still covers it and the branch tip is still that commit. |
| GitHub credentials leak to the Mac | The plugin holds none and the Mac receives none. The Mac fetches public repositories anonymously. |

## Nothing listens on your Mac

The helper opens one outbound WSS connection to your Hermes gateway. The Mac has no inbound port and needs no SSH
and no VPN. Closing the helper closes the only path in.

## Pairing proves two keys and grants nothing

`/hands pair` gives the owner a pairing string in their own authenticated chat. It holds a 128-bit code and the
server's public key. The code works once and expires after 10 minutes. The server stores only its hash and rate
limits attempts by source address and globally.

During the exchange the Mac proves it holds a new device key, and the server proves it holds the key named in the
string. The Mac writes its configuration only after that second check passes.

A freshly paired Mac advertises no capabilities and has no grants. Until the owner runs `bh grant add` on the Mac,
a paired server can do nothing with it.

## Two approvals that cannot stand in for each other

A build needs a yes from two places.

1. **Hermes approval.** The plugin's `pre_tool_call` hook resolves the request to an exact scope and asks through
   Hermes's own approval prompt. The prompt names the commit. The approval is bound to that one tool call. A call
   that reaches the handler without having passed the hook is refused. A permanent approval covers exactly one
   Mac, project, scheme, configuration and destination, and never a wider set.
2. **Mac grant.** The helper checks the signed request against the owner's local grants. The project, scheme,
   configuration and destination must each match an entry exactly. The repository URL and project path come from
   the grant, not from the request.

The agent cannot widen a grant. A request outside the grants gets `needs_grant` and the exact `bh grant add`
command for the owner to run, or not. `bh grant add` never changes an existing grant unless the owner passes
`--replace`, and then it prints what changed. A grant can expire, and a grant can require a confirmation dialog
on the Mac for every build.

## What the Mac checks before anything runs

The helper runs these checks in order, and the first failure ends the request
([protocol section 4.2](PROTOCOL.md#42-helper-checks-in-order-first-failure-wins-nothing-executes-before-step-11)):

- the frame signature, session id and sequence number;
- the contract signature against the pinned server key;
- the schema, with unknown keys rejected;
- that the contract names this Mac;
- that it has not expired, with a lifetime of at most 10 minutes;
- that the job id and nonce are new;
- that the capability is enabled locally;
- that a grant exists and has not expired;
- the scheme, configuration and destination allowlists;
- that the repository fingerprint matches the grant's own URL.

The helper checks the expiry and the grant again when the job leaves the queue.

## What can be built

Only a commit reachable from a `bh/*` branch of the repository the owner granted. The runner fetches those
branches itself, requires the commit to be in their history, checks out by full SHA and verifies that the tree it
got is the tree of that commit. Git runs with hooks, filters, submodules and repository config disabled.

The runner refuses a checkout that contains a symlink leading outside it or an entry that imitates `.git`. It also
refuses projects with remote Swift package dependencies, because the build has no network to fetch them and should
not have one.

Builds are compile-only, with code signing off. Nothing is installed, run or tested.

## Root, limited to six command lines

The helper runs as your user. To start a build it needs one step as root, and the sudo rule that allows it lists
six exact command lines: four timeout tiers with that Mac's fixed disk and memory budgets, a sweep and a cancel.
The rule has no wildcards, because sudo's `*` also matches spaces. The dispatcher rejects any other argument list
itself, so the two checks do not depend on each other. The job description goes in on stdin and passes the same
validator in the dispatcher and in the runner.

## The build sandbox

The first prototype started builds with `sudo -u bh-builder` from my login session. A probe showed that a build
started that way kept my launchd session, and one `launchctl submit` from the build ran a command as me, with my
admin group. A separate user gave no protection at all. I found that out on my own machine, and
[the sandbox notes](SANDBOX.md#why-builds-never-start-from-the-owners-session) record what it broke.

So builds now start only as a system LaunchDaemon, bootstrapped per job by the root dispatcher, running as the
hidden `bh-builder` user in its own session. Inside a build, launchd reports the system domain, and nothing a
build submitted to launchd ever ran, as any user.

On top of the separate user, Xcode runs under a sandbox profile ([`builder.sb`](../sandbox/builder.sb)):

- **No network.** No TCP, UDP or unix sockets. Fetching the repository is a separate earlier step under its own
  profile, which allows outbound HTTPS on port 443 and nothing else.
- **Reads by allowlist.** The system, Xcode and the job's own directory. Your home folder is not on the list.
- **Writes confined** to the job's build directories and the build user's temporary folders. The build cannot
  write its own result file, so it cannot forge a result.
- **One system service** reachable (`com.apple.bsd.dirhelper`). The Keychain, Launch Services, Apple Events, the
  pasteboard and the preferences daemon are not reachable.
- **Signals** only to processes inside the sandbox. The build cannot stop or kill the runner that watches it.
- **No hardware identifiers.** The serial number, platform UUID and MAC addresses are denied.

Git materialization runs under a second profile ([`git.sb`](../sandbox/git.sb)) that denies everything by default.
The remote must resolve to public addresses only, and git is pinned to the address the runner checked.

## Limits and cleanup

The root dispatcher measures the build from outside, because the runner shares a user with the build and could be
interfered with. Every second it checks free disk space, the summed memory of the build user's processes and a
wall-clock deadline. A breach ends the job. The build user is capped at 256 processes and 1,024 open files per
process.

After every job, on every exit path, the dispatcher boots the job out, kills every process of the build user and
deletes the build directories, clearing file flags and permissions first so a build cannot leave files that
resist deletion. One build runs at a time.

## Taking access back

- `bh grant revoke` cancels that project's queued and running builds.
- `bh unpair` deletes the device key, the pinned server key, the grants and the job history. Every wait on the
  server in that command has a time limit, so a server that stalls cannot keep the pairing on the Mac.
- `/hands unpair` on the server revokes the Mac's id, which can never authenticate again.
- `bh uninstall` stops the helper and removes the build user, the dispatcher and the sudo rule. It verifies each
  removal and lists anything it could not confirm.

## Credentials

The plugin stores one secret, its own signing key. It never pushes, never opens pull requests and holds no GitHub
credential. When the agent edits code and opens a pull request, it uses Hermes's own git tooling and whatever
credential the owner of that Hermes installed. The Mac compiles public commits and sees none of it.

## How it was tested

- **Hostile builds with a control.** The probe suite builds a hostile app whose build script and compiler wrapper
  try each escape. It runs first with no sandbox and then under the real profile. A row passes only if the escape
  worked without the sandbox and failed with it. A row where both runs were blocked proves nothing and is
  reported as inconclusive.
- **Breaking the rules on purpose.** `tests/mutation-check.sh` removes one protection at a time and requires a
  test to fail. On the test Mac it killed 35 of 35 mutants. A review before 0.2 found one test that still passed
  with its fix removed, and that test was rewritten.
- **Review rounds.** The sandbox went through three security review rounds before the helper used it. Later
  rounds covered uninstall, the install flow, the pull request workflow and the 0.2 release. The last round before 0.2
  refused the release over one finding: a paired server could hold `bh unpair` open before it deleted anything.
  That is fixed and has tests for both ways of doing it.
- **Live runs.** A real model on a deployed gateway, over public WSS, to a production sandbox install on a real
  Mac. Every cloud job record matched the Mac dispatcher's own output.

The recorded output and the commands to reproduce it are in [SANDBOX.md](SANDBOX.md). Run the sandbox suite only
on a dedicated test Mac, because it creates a hidden user and runs hostile builds.

## What it does not protect against

- **Malware running as your Mac user** can read the device key and edit the grants. The sandbox protects you from
  builds, not from your own account.
- **A stolen server key** can request anything the existing grants allow until you unpair. There is no revocation
  list in 0.2.
- **A paired server that stops reading its connection** can delay a revoke or cancel until it reads again.
  Stopping the helper always works.
- **The hosting provider** can see build results and drop or delay messages. It cannot forge or replay them.
- **A build can read file metadata** anywhere: whether a path it already knows exists, its size and its
  modification time. It cannot read contents or list directories outside the allowlist.
- **Disk and memory budgets are polled.** macOS has no per-user quota, so a build can overshoot by what it writes
  or allocates within one interval.
- **Cleanup is not complete.** macOS keeps a few empty protected folders for the build user that not even root
  can delete.
- **Hermes approvals can be switched off.** With approvals off or in yolo mode, Hermes approves automatically
  and only the Mac's grants decide.
- **The readiness check is advice.** The plugin holds no GitHub credential, so it cannot stop an agent from
  opening a pull request anyway.
- **One test machine.** All sandbox results come from one Apple silicon Mac on macOS 27.2 and Xcode 27.2.

The full lists are in [protocol residual risks](PROTOCOL.md#8-residual-risks-accepted-for-02) and
[sandbox remaining gaps](SANDBOX.md#remaining-gaps-precise).
