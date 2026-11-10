"""`bh`: the Borrowed Hands helper CLI (SPEC 8.4). Thin: parse arguments, call the module that does the work,
print a sentence that says what to do next."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import signal
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import __version__, account, config, keyfile
from ._shared import SANDBOX
from .config import ACTIONS, ConfigError, Workspace
from .env import Env, Uninspected, Unverified, describe_unsafe, paste_command, show, shown_path
from .jobstore import JobStore

DB = "jobs.sqlite3"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bh", description="Borrowed Hands helper: lend this Mac's hands to Hermes, "
                                                       "narrowly. Start with 'bh setup'.")
    p.add_argument("--version", action="version", version=f"bh {__version__}")
    p.add_argument("--home", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", metavar="command")
    sub.add_parser("setup", help="guided first run: sandbox, sudo rule, pairing, one grant, service, self-test")
    st = sub.add_parser("selftest", aliases=["doctor"], help="check everything; PASS/FAIL per line with the fix")
    st.add_argument("--build", metavar="WORKSPACE", help="also run one real local build of that workspace")
    pr = sub.add_parser("pair", help="pair with the string from /hands pair")
    pr.add_argument("pairing")
    pr.add_argument("--name")
    pr.add_argument("--insecure-local", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("run", help="run the helper in the foreground (what the background service runs)")
    sv = sub.add_parser("service", help="install or remove the background helper (LaunchAgent)")
    sv.add_argument("action", choices=["install", "uninstall"])
    sub.add_parser("status", help="pairing, connection, capabilities, grants, queue")
    g = sub.add_parser("grant", help="grants: what Hermes may build here")
    gs = g.add_subparsers(dest="gcmd", metavar="add|list|revoke")
    ga = gs.add_parser("add")
    ga.add_argument("capability", choices=["xcode.build"])
    ga.add_argument("--workspace", required=True)
    ga.add_argument("--repo", required=True, help="public https URL of the git repo")
    ga.add_argument("--project", required=True, help="App.xcodeproj or App.xcworkspace, relative to the repo root")
    ga.add_argument("--scheme", action="append", required=True)
    ga.add_argument("--configuration", action="append")
    ga.add_argument("--destination", action="append")
    ga.add_argument("--confirm", action="store_true", help="ask on this Mac before every build")
    ga.add_argument("--expires", help="7d, 12h, 30m or an RFC 3339 time; kept to whole seconds (fractions dropped)")
    ga.add_argument("--replace", action="store_true",
                    help="replace an existing grant with the same --workspace name (prints what changes)")
    gs.add_parser("list")
    gr = gs.add_parser("revoke")
    gr.add_argument("workspace")
    c = sub.add_parser("caps", help="turn a capability on or off")
    c.add_argument("action", choices=["enable", "disable"])
    c.add_argument("capability")
    j = sub.add_parser("jobs", help="recent jobs on this Mac")
    j.add_argument("--all", action="store_true")
    cn = sub.add_parser("cancel", help="cancel a queued or running job")
    cn.add_argument("job_id")
    sub.add_parser("unpair", help="forget the pairing (and tell the server if reachable)")
    sub.add_parser("rotate-key", help="replace this Mac's device key")
    sub.add_parser("uninstall", help="remove the service, the pairing, the sandbox and the sudo rule")
    return p


def _ms(ms: Optional[int]) -> str:
    if ms is None:
        return "never"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def _now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def _load(env: Env) -> config.Config:
    keyfile.ensure_private_dir(env.home)
    return config.load(env.home)


def _utc(ms: Optional[int]) -> str:
    """Unambiguous: UTC with seconds, as stored (refusal and replacement output)."""
    return "never" if ms is None else config.ms_rfc3339(ms)


def _grant_line(w: Workspace, when=_ms) -> str:
    return (f"{w.id}: {w.remote_url} {w.project}; schemes {', '.join(w.schemes)}; configurations "
            f"{', '.join(w.configurations)}; destinations {' | '.join(w.destinations)}; "
            f"confirm {'on' if w.confirm else 'off'}; expires {when(w.expires_at)}")


# ------------------------------------------------------------------ commands
def cmd_grant(env: Env, a) -> int:
    cfg = _load(env)
    if a.gcmd == "list":
        if not cfg.workspaces:
            env.say("No grants. Add one with: bh grant add xcode.build --workspace ID --repo URL --project P --scheme S")
        for w in cfg.workspaces:
            env.say(_grant_line(w))
        return 0
    if a.gcmd == "revoke":
        found = []

        def revoke(c: config.Config) -> config.Config:
            found.append(c.workspace(a.workspace) is not None)
            return c.without_workspace(a.workspace) if found[0] else c
        config.update(env.home, revoke)
        if not found[0]:
            env.err(f"no grant named {a.workspace!r} (see 'bh grant list')")
            return 2
        env.say(f"Revoked {a.workspace}. Its queued and running jobs are cancelled within a few seconds.")
        return 0
    if a.gcmd == "add":
        seen = {}

        def add(c: config.Config) -> config.Config:
            seen["old"] = c.workspace(a.workspace)
            seen["new"] = c.with_workspace(ws, replace_existing=a.replace)
            seen["same"] = seen["new"] is c
            return seen["new"]
        try:
            expires = config.parse_expires(a.expires, _now_ms()) if a.expires is not None else None
            ws = Workspace(id=a.workspace, remote_url=a.repo, project=a.project, schemes=tuple(a.scheme),
                           configurations=tuple(a.configuration or ["Debug"]),
                           destinations=tuple(a.destination or [config.DESTINATIONS[0]]), confirm=a.confirm,
                           expires_at=expires)
            new_cfg = config.update(env.home, add)       # load, check and write as one locked transaction
            old = seen["old"]
        except config.GrantExists as e:
            env.err(f"a grant named {a.workspace!r} already exists; nothing was changed. It allows:")
            env.err(f"  {_grant_line(e.existing, _utc)}")
            env.err("To add a separate grant, pick another --workspace name. To change this one, rerun the command "
                    f"with --replace (it prints what changes), or run 'bh grant revoke {a.workspace}' first.")
            return 2
        except ConfigError as e:
            env.err(str(e))
            return 2
        if seen["same"]:
            env.say(f"Grant {a.workspace} already exists with exactly these settings; nothing changed.")
            return 0
        tail = ("Hermes can build commits on bh/* branches of that repo"
                + ("" if new_cfg.capabilities.xcode_build else "; turn builds on with 'bh caps enable xcode.build'")
                + ".")
        if old is None:
            env.say(f"Granted xcode.build for {a.workspace}. {tail}")
            return 0
        env.say(f"Replaced grant {a.workspace}.")
        env.say(f"  before: {_grant_line(old, _utc)}")
        env.say(f"  after:  {_grant_line(new_cfg.workspace(a.workspace), _utc)}")
        for text, wider in config.grant_changes(old, new_cfg.workspace(a.workspace), when=_utc):
            env.say(f"  {'MORE PERMISSIVE' if wider else 'changed'}: {text}")
        env.say(tail)
        return 0
    env.err("usage: bh grant add|list|revoke")
    return 2


def cmd_caps(env: Env, a) -> int:
    if a.capability not in ACTIONS:
        env.err(f"unknown capability {a.capability!r}; one of {', '.join(ACTIONS)}")
        return 2
    _load(env)
    on = a.action == "enable"
    if on and a.capability == "xcode.build":
        st = env.install_status()
        if not st.ok:
            for prob in st.problems:
                env.err(prob)
            return 1
    if on and a.capability == "screen.capture":
        if "screen.capture" not in env.get_executors():
            env.err("screen capture is not available in this version")
            return 1
        if not env.screen_granted():
            env.err("Screen Recording not granted to Terminal: System Settings > Privacy & Security > Screen Recording")
            return 1
    config.update(env.home, lambda c: c.with_capability(a.capability, on))
    env.say(f"{a.capability} {'enabled' if on else 'disabled'}.")
    return 0


def cmd_status(env: Env) -> int:
    from .daemon import daemon_running, read_status
    cfg = _load(env)
    if cfg.server is None:
        env.say("Status: not paired. Run 'bh setup'.")
        return 0
    st = read_status(env.home)
    running = daemon_running(env.home)
    env.say(f"Paired as '{cfg.device_name}' (helper {cfg.server.helper_id[:8]}) to {cfg.server.url}")
    env.say(f"Helper: {'running' if running else 'not running (bh service install, or bh run)'}"
            + (f", {st.get('state')}" + (f" ({st.get('detail')})" if st.get("detail") else "") if running and st else ""))
    if running and st.get("builds_blocked"):
        env.say(f"Builds paused: {st['builds_blocked']} (the build user could not be cleaned up after a restart; "
                f"retrying; see ~/Library/Logs/BorrowedHands/helper.log)")
    caps = [x for x in ACTIONS if cfg.capabilities.enabled(x)]
    env.say(f"Capabilities on: {', '.join(caps) if caps else 'none'}")
    env.say(f"Grants: {', '.join(w.id for w in cfg.workspaces) if cfg.workspaces else 'none'}")
    if (env.home / DB).exists():
        s = JobStore(env.home / DB)
        try:
            active = s.active()
        finally:
            s.close()
        env.say(f"Jobs: {len(active)} active" + "".join(f"\n  {r.job_id[:8]} {r.action} {r.state}" for r in active))
    return 0


def cmd_jobs(env: Env, a) -> int:
    _load(env)
    if not (env.home / DB).exists():
        env.say("No jobs yet.")
        return 0
    s = JobStore(env.home / DB)
    try:
        rows = s.recent(200 if a.all else 20, include_terminal=True)
    finally:
        s.close()
    for r in rows:
        why = f" ({r.error['code']})" if r.error else ""
        env.say(f"{r.job_id}  {_ms(r.updated_at)}  {r.action:<14} {r.state}{why}")
    if not rows:
        env.say("No jobs yet.")
    return 0


def cmd_cancel(env: Env, a) -> int:
    _load(env)
    if not (env.home / DB).exists():
        env.err("no such job")
        return 2
    s = JobStore(env.home / DB)
    try:
        ok = s.request_cancel(a.job_id)
    finally:
        s.close()
    if not ok:
        env.err("no such job, or it already finished")
        return 2
    env.say("Cancel requested; the helper stops it within a few seconds.")
    return 0


def cmd_run(env: Env) -> int:
    from .daemon import Daemon, default_deps
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    async def main() -> int:
        task = asyncio.create_task(Daemon(env.home, default_deps()).run())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            return await task
        except asyncio.CancelledError:
            return 0
    rc = asyncio.run(main())
    if rc == 3 and os.environ.get("XPC_SERVICE_NAME") == "dev.borrowed-hands.helper":
        # Unpaired: stop the LaunchAgent instead of letting launchd restart an unpaired helper forever.
        env.svc().uninstall()
    return rc


SUDOERS_FILES = ("/etc/sudoers.d/borrowed-hands", "/etc/sudoers.d/.borrowed-hands.new")   # rule + setup staging
SANDBOX_PREFIX = "/opt/borrowed-hands"
BUILDER_HOME = "/Users/Shared/.bh-builder-home"
BUILDER_UID = 470
RUNNER_JOB = "system/com.borrowedhands.runner"
LOGS = "~/Library/Logs/BorrowedHands"
NO_RERUN = "rerunning 'bh uninstall' will not change this line"
ORPHAN_FOLDERS_RC = 3     # the teardown's exit: all removed but uid 470's folders, checked and not verifiably empty
# Same text as the teardown's RECOVERY. Never Full Disk Access for /bin/sh, bh-runner or bh-dispatch.
RECOVERY = ("Restart this Mac (macOS clears these temporary folders at startup), then run 'bh uninstall' again "
            "before deleting anything by hand. If it still cannot be verified, temporarily give your Terminal Full "
            "Disk Access (System Settings > Privacy & Security > Full Disk Access), rerun 'bh uninstall', then "
            "remove that access.")


def cmd(argv: list[str], sudo: bool = False) -> str:
    """A command the user copies and runs: shell-quoted, or a description when a path in it is unsafe to paste."""
    return paste_command(argv, sudo) or describe_unsafe(argv)


def uninstall_checklist(env: Env, teardown_rc: Optional[int] = None) -> list[tuple[str, str, str]]:
    """(what, state, next step) for everything Borrowed Hands puts on this Mac, checked after removal so a success
    report is a verified one (audit #6). state is "ok" (verifiably gone), "ok-note" (only what macOS keeps and
    nobody can remove is left; the step is the note), "LEFT" (still there) or "UNKNOWN" (the check itself failed,
    which never counts as removed). teardown_rc: the root teardown's exit code in this run (None: it did not run);
    0 means it checked uid 470's per-user folders and found them clean, ORPHAN_FOLDERS_RC that it checked them and
    they are not (the account was already gone, so nothing removes them)."""
    from . import service
    teardown = cmd(["bash", str(SANDBOX / "teardown-bh-builder.sh")], sudo=True)
    agent = f"gui/{env.uid}/{service.LABEL}"
    plist = service.plist_path(env.user_home)

    def launchd(target: str, fix: str) -> tuple[str, str]:
        argv = [service.LAUNCHCTL, "print", target]
        rc = env.run(argv)[0]
        st = service.launchd_state(rc)
        return (("ok", fix) if st is False else ("LEFT", fix) if st else
                ("UNKNOWN", f"could not check (exit {rc}); check with: {cmd(argv)}"))

    def record(kind: str, name: str) -> tuple[str, str]:
        argv = ["/usr/bin/dscl", ".", "-list", f"/{kind}"]
        rc, out = env.run(argv)
        if rc != 0:
            return "UNKNOWN", f"could not check (exit {rc}); check with: {cmd(argv)}"
        return ("LEFT" if name in out.split() else "ok"), teardown

    def path(p: str, fix: str) -> tuple[str, str]:
        st = env.exists(p)
        return (("ok", fix) if st is False else ("LEFT", fix) if st else
                ("UNKNOWN", f"could not check; check with: {cmd(['ls', '-ld', p])}"))

    def caches(teardown_cmd: str) -> tuple[str, str]:
        """Inspected on every run, so a retry cannot inherit success from an earlier failed or skipped cleanup."""
        found = env.cache_dirs(BUILDER_UID)
        if found is None:
            return "UNKNOWN", f"could not check; check with: find /private/var/folders -maxdepth 2 -user {BUILDER_UID}"
        if not found:
            return "ok", teardown_cmd
        # Numeric ownership alone does not make these ours. Offer deletion only when both lookups worked, the
        # bh-builder account is verifiably gone and no other account holds the uid (the teardown's own rule).
        argv = ["/usr/bin/dscl", ".", "-list", "/Users", "UniqueID"]
        rc, out = env.run(argv)
        if rc != 0:
            return "UNKNOWN", (f"could not check the user accounts (exit {rc}), so these folders were not touched; "
                               f"check with: {cmd(argv)}")
        rows = [line.split() for line in out.splitlines() if line.split()]
        if any(r[0] == "bh-builder" for r in rows):
            return "LEFT", teardown_cmd        # the account still exists: teardown verifies it and removes them
        holders = sorted({r[0] for r in rows if str(BUILDER_UID) in r[1:]})
        if holders:
            return "UNKNOWN", (f"uid {BUILDER_UID} belongs to another account ({', '.join(holders)}); these folders "
                               f"are its files and were not touched")
        # macOS keeps a per-user skeleton here that root cannot remove (sunlnk dirs): never offer rm for it
        removable = env.cache_removable(found, BUILDER_UID)
        if removable is None:
            return "UNKNOWN", f"could not check what is left in them; check with: {cmd(['ls', '-lRO', *found], True)}"
        # T and C are 0700: this process cannot look inside. Only the root teardown can, and it vouches for them by
        # its exit code in this same run: 0 means it checked them and they are clean (on every path, also when the
        # account was already gone), 3 that it checked them and they are not; anything else, or no run, says nothing.
        uninspected = [p for p in removable if isinstance(p, Uninspected)]
        removable = [p for p in removable if not isinstance(p, Uninspected)]
        look = cmd(['ls', '-laO', *uninspected], True)
        if not removable and uninspected and teardown_rc == ORPHAN_FOLDERS_RC:
            # Fail closed, but not a loop: without the account no teardown removes them; say what does.
            return "UNKNOWN", (f"the bh-builder account was already gone, and the teardown looked inside "
                               f"{', '.join(map(shown_path, uninspected))} as root and could not verify them empty "
                               f"(see its message above): {NO_RERUN}; look with: {look}; if that shows only "
                               f"macOS's protected folders there is nothing to remove. Otherwise run 'bh setup': "
                               f"it checks them as root before it creates bh-builder again and prints the one safe "
                               f"removal command (docs/QUICKSTART.md, Uninstall)")
        if not removable and uninspected and teardown_rc != 0:
            return "UNKNOWN", (f"only root can look inside {', '.join(map(shown_path, uninspected))}, and the "
                               f"teardown did not verify them in this run (it failed or did not run; see above); "
                               f"look with: {look}")
        if not removable:
            return "ok-note", (f"left by macOS (protected per-user folders, nothing removable left"
                               f"{', verified by the teardown in this run' if uninspected else ''}): "
                               f"{', '.join(map(shown_path, found))}; they stay owned by uid {BUILDER_UID} and setup "
                               f"checks them before it creates bh-builder again; nothing to do")
        unverified = [p for p in removable if isinstance(p, Unverified)]
        if unverified:      # TCC-protected TemporaryItems not provably empty: never an rm next to it
            return "LEFT", (f"{RECOVERY} (could not verify that these protected TemporaryItems folders are empty: "
                            f"{', '.join(map(shown_path, unverified))})")
        # The account is gone, so the teardown can no longer authorize this; names below T/ were chosen by build
        # code, so the command is quoted, or not shown at all for a name with control characters.
        return "LEFT", cmd(["rm", "-rf", *removable], sudo=True)

    def paths(ps: tuple, fix: str) -> tuple[str, str]:
        states = [path(p, fix) for p in ps]
        return next((s for s in states if s[0] == "LEFT"),
                    next((s for s in states if s[0] == "UNKNOWN"), ("ok", fix)))
    return [
        ("background helper stopped", *launchd(agent, f"launchctl bootout {agent}")),
        (f"LaunchAgent removed ({plist})", *path(str(plist), cmd(["rm", str(plist)]))),
        (f"pairing, device key, grants and job history removed ({env.home})",
         *path(str(env.home), cmd(["rm", "-rf", str(env.home)]))),
        ("build runner stopped", *launchd(RUNNER_JOB, f"sudo launchctl bootout {RUNNER_JOB}")),
        (f"build sandbox removed ({SANDBOX_PREFIX})", *path(SANDBOX_PREFIX, teardown)),
        ("bh-builder user removed", *record("Users", "bh-builder")),
        ("bh-builder group removed", *record("Groups", "bh-builder")),
        (f"build user home removed ({BUILDER_HOME})", *path(BUILDER_HOME, teardown)),
        (f"build user caches removed (uid {BUILDER_UID} under /private/var/folders)", *caches(teardown)),
        (f"sudo rule removed ({SUDOERS_FILES[0]})", *paths(SUDOERS_FILES, cmd(["rm", "-f", *SUDOERS_FILES], True))),
    ]


def _report(env: Env, steps: tuple = (), teardown_rc: Optional[int] = None) -> int:
    """`steps`: (what, state, next step) for each admin command that ran. A failed one stays LEFT even when every
    inspected path is gone: the teardown also cleans what the checklist cannot see (uid 470's caches)."""
    checklist = uninstall_checklist(env, teardown_rc) + list(steps)
    env.say("Removal check:")
    for what, state, step in checklist:
        env.say(f"  ok      {what}" if state == "ok" else f"  ok      {what}; {step}" if state == "ok-note" else
                f"  LEFT    {what}; remove it with: {step}" if state == "LEFT" else f"  UNKNOWN {what}; {step}")
    left = [c for c in checklist if c[1] not in ("ok", "ok-note")]
    stuck = [c for c in left if NO_RERUN in c[2]]       # no rerun can settle these (see caches() in the checklist)
    if left and len(stuck) == len(left):
        env.err(f"{len(left)} item(s) could not be checked, and rerunning 'bh uninstall' will not change that (see "
                f"the note above). Everything else is verified removed. You can finish with: uv tool uninstall "
                f"borrowed-hands-helper; if you install again, 'bh setup' checks these folders first.")
        return 1
    if left:
        env.err(f"{len(left)} item(s) remain or could not be checked. Run the commands shown, or fix the error "
                f"above and run 'bh uninstall' again. Keep the helper installed until this passes: the teardown "
                f"script ships with it." + (" The build user caches line will stay UNKNOWN on reruns (see its "
                                            "note); the others can clear." if stuck else ""))
        return 1
    env.say(f"Uninstalled and verified. Logs are kept in {LOGS} (delete with: rm -rf {LOGS}). "
            f"Finish with: uv tool uninstall borrowed-hands-helper")
    return 0


def _wait_for_helper_exit(env: Env) -> Optional[bool]:
    """True once no helper holds the lock; False if one still does at the deadline; None if the lock cannot be
    checked. The helper drains or cancels a running build before it exits (LaunchAgent ExitTimeOut is 120 s)."""
    deadline = time.monotonic() + env.stop_wait_s
    waiting = False
    while True:
        state = env.helper_running(env.home)
        if state is None:
            return None
        if not state:
            return True
        if time.monotonic() >= deadline:
            return False
        if not waiting:
            env.say(f"  waiting up to {int(env.stop_wait_s)} s for the helper to finish stopping ...")
            waiting = True
        time.sleep(0.5)


def cmd_uninstall(env: Env) -> int:
    if env.ask("Remove the background helper, this Mac's pairing, the build sandbox, the bh-builder user and the "
               "sudo rule? [y/N] ").strip().lower() not in ("y", "yes"):
        env.say("Nothing removed.")
        return 1
    ok, msg = env.svc().uninstall(user_home=env.user_home, uid=env.uid, run=env.run, say=env.say)
    (env.say if ok else env.err)(f"  {msg}")
    # Fence: nothing below may run under a live helper. It could be mid-build, and removing the sandbox, the
    # sudo rule or its state would take away its own cancel and sweep authority (R4).
    stopped = _wait_for_helper_exit(env) if ok else False
    if stopped is None:
        env.err(f"Could not check whether the helper is running (its lock file in {env.home} cannot be opened), so "
                f"nothing else was removed. Fix that file's permissions, then run 'bh uninstall' again.")
        _report(env)
        return 1
    if not stopped:
        env.err("The helper is still running (the background service, or 'bh run' in a terminal), so nothing else "
                "was removed. Stop it (Ctrl-C for 'bh run', or the command above), then run 'bh uninstall' again.")
        _report(env)
        return 1
    if env.home.exists():
        try:
            if config.load(env.home).server is not None:       # never paired: nothing to tell the server
                asyncio.run(account.unpair(env.home, connect=env.connect, timing=env.timing, out=env.say))
        except (keyfile.UnsafeKeyStorage, ConfigError) as e:
            env.err(f"  local pairing state is damaged ({e}); deleting it anyway")
        except OSError as e:          # e.g. an immutable key or job database: report it, keep cleaning up (R5)
            env.err(f"  could not remove part of the local pairing state ({e}); continuing")
    # The sudo rule goes first: while it exists any process running as this user can start a uid-470 build, which
    # could write into the build user's folders between the teardown's checks and the account's deletion. If it
    # cannot be removed, the teardown is not run (that window would stay open); the checklist reports both.
    steps = []
    # Only the teardown's own exit code says whether it checked uid 470's per-user folders (see caches() in the
    # checklist); a snapshot taken here could be stale by the time it runs (an open prompt can delete the account).
    teardown_rc: Optional[int] = None
    for argv, why, done in ((["/bin/rm", "-f", *SUDOERS_FILES], "removing the sudo rule", "sudo rule removal finished"),
                            (["bash", str(SANDBOX / "teardown-bh-builder.sh")], "removing the build user and sandbox",
                             "teardown of the build user, home, caches and sandbox finished")):
        if argv[0] == "bash" and any(s[1] != "ok" for s in steps):
            steps.append(("teardown of the build user, home, caches and sandbox not run (the sudo rule could not be "
                          "removed)", "LEFT", show(argv)))
            break
        env.say(f"  {why}:")
        if argv[0] == "bash":       # deleting the account: macOS asks on behalf of this terminal's app (TCC)
            env.say("  macOS may ask whether your terminal app may \"administer your computer\". Click Allow while "
                    "this waits: removing the build user or its group waits for that answer, and a prompt still open "
                    "after this command ends deletes it whenever it is clicked.")
        env.say(f"  $ {show(argv)}")
        try:
            rc = env.sudo(argv)
        except KeyboardInterrupt:
            # The delete can wait minutes for macOS's prompt; stopping here does not withdraw that request.
            env.err("  interrupted. If macOS showed a prompt asking whether your terminal app may \"administer your "
                    "computer\", it is still open: clicking Allow later deletes bh-builder at that moment. So run "
                    "'bh uninstall' again first, and click Allow while it waits.")
            steps.append((f"{done.rsplit(' finished', 1)[0]} interrupted", "LEFT", show(argv)))
            shutil.rmtree(env.home, ignore_errors=True)
            _report(env, tuple(steps), None)
            return 130
        if argv[0] == "bash" and rc == ORPHAN_FOLDERS_RC:
            env.err("  the teardown removed the rest, but could not verify uid 470's per-user folders; see the "
                    "checklist below")
        elif rc != 0:
            env.err("  that command failed; see the checklist below")
        if argv[0] == "bash":
            teardown_rc = rc
        if argv[0] == "bash" and rc == ORPHAN_FOLDERS_RC:     # all else removed; the caches line says the rest
            steps.append(("teardown of the build user, home and sandbox finished", "ok-note",
                          "uid 470's per-user folders could not be verified (see the build user caches line)"))
            continue
        steps.append((done if rc == 0 else f"{done.rsplit(' finished', 1)[0]} failed (exit {rc}; see its message above)",
                      "ok" if rc == 0 else "LEFT", show(argv)))
    shutil.rmtree(env.home, ignore_errors=True)
    return _report(env, tuple(steps), teardown_rc)


def cmd_unpair(env: Env) -> int:
    # The background service is stopped first, so its helper is already draining when the key is deleted. A helper
    # that outlives this ('bh run' in a terminal, or a failed stop) still has the key, pin and grants in memory:
    # the local state is deleted regardless (a restarted helper then cannot authenticate), but only a free helper
    # lock lets the command say the pairing is gone.
    ok, msg = env.svc().uninstall(user_home=env.user_home, uid=env.uid, run=env.run, say=env.say)

    def helper_stopped(wait: bool) -> bool:
        if not ok:
            return False
        return (_wait_for_helper_exit(env) if wait else env.helper_running(env.home) is False) is True
    return asyncio.run(account.unpair(env.home, connect=env.connect, timing=env.timing, out=env.say,
                                      helper_stopped=helper_stopped, stop_hint="" if ok else msg))


def main(argv: Optional[list] = None, env: Optional[Env] = None) -> int:
    a = _parser().parse_args(argv)
    env = env or Env()
    if a.home:
        env = replace(env, home=Path(a.home))
    try:
        if a.cmd == "setup":
            from . import setup
            return setup.run(env)
        if a.cmd in ("selftest", "doctor"):
            from . import selftest
            return selftest.run(env, a.build)
        if a.cmd == "pair":
            return asyncio.run(account.pair(env.home, a.pairing, a.name, a.insecure_local, connect=env.connect,
                                            timing=env.timing, host=env.host, out=env.say))
        if a.cmd == "run":
            return cmd_run(env)
        if a.cmd == "service":
            ok, msg = env.svc().install() if a.action == "install" else env.svc().uninstall(say=env.say)
            (env.say if ok else env.err)(msg)
            return 0 if ok else 1
        if a.cmd == "status":
            return cmd_status(env)
        if a.cmd == "grant":
            return cmd_grant(env, a)
        if a.cmd == "caps":
            return cmd_caps(env, a)
        if a.cmd == "jobs":
            return cmd_jobs(env, a)
        if a.cmd == "cancel":
            return cmd_cancel(env, a)
        if a.cmd == "unpair":
            return cmd_unpair(env)
        if a.cmd == "rotate-key":
            return asyncio.run(account.rotate_key(env.home, connect=env.connect, timing=env.timing, out=env.say))
        if a.cmd == "uninstall":
            return cmd_uninstall(env)
    except keyfile.UnsafeKeyStorage as e:
        env.err(str(e))
        return 1
    except ConfigError as e:
        env.err(f"config.toml: {e}")
        return 1
    _parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
