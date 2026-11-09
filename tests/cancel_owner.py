#!/usr/bin/python3 -I
"""Cancel from the owner's uid, the way the helper does it (PROTOCOL 4.6, SPEC 9.1).

    /usr/bin/python3 -I tests/cancel_owner.py <job.json> <mode> [dispatch args...]

Runs `sudo -n /opt/borrowed-hands/bin/bh-dispatch ... < job.json` as a child of THIS process (the
owner's uid, no root), waits until the build is running (uid-470 processes exist), then cancels:
  mode "sigterm-sudo"      : SIGTERM to the sudo child, child started in the caller's process group
  mode "sigterm-sudo-sess" : SIGTERM to the sudo child, child started in its own session
  mode "cancel-cmd"        : a second `sudo -n bh-dispatch --cancel` with the job id on stdin
  mode "cancel-wrong-id"   : `--cancel` with a different job id first (must answer not_running and
                             leave the build alone), then SIGTERM to finish the test
Prints one JSON line: {"mode", "status", "elapsed_s", "dispatch_line"}; exit 0 if status == cancelled.
"""
import json
import os
import signal
import subprocess
import sys
import time

DISPATCH = "/opt/borrowed-hands/bin/bh-dispatch"


def builder_running():
    """True once the hang-app's Run Script is executing, i.e. mid-build (not during fetch)."""
    r = subprocess.run(["/bin/ps", "-U", "470", "-o", "command="], capture_output=True, text=True)
    return "bh-hang-marker-script" in r.stdout


def main():
    job_path, mode, extra = sys.argv[1], sys.argv[2], sys.argv[3:]
    job_id = json.load(open(job_path))["job_id"]
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    with open(job_path, "rb") as stdin:
        p = subprocess.Popen(["/usr/bin/sudo", "-n", DISPATCH] + extra, stdin=stdin, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, cwd="/", env=env,
                             start_new_session=(mode == "sigterm-sudo-sess"))
    t0 = time.monotonic()
    while not builder_running() and time.monotonic() - t0 < 90:
        time.sleep(0.5)
    mid_build = builder_running()
    t_cancel = time.monotonic()
    wrong = None
    cancel_rc = None
    if mode in ("sigterm-sudo", "sigterm-sudo-sess"):
        p.send_signal(signal.SIGTERM)          # the helper's uid signalling its own sudo child
    elif mode in ("cancel-cmd", "cancel-wrong-id"):
        cid = job_id if mode == "cancel-cmd" else "00000000-0000-4000-8000-000000000000"
        c = subprocess.run(["/usr/bin/sudo", "-n", DISPATCH, "--cancel"], input=cid.encode(), cwd="/", env=env,
                           capture_output=True, timeout=30)
        cancel_rc = c.returncode
        wrong = c.stdout.decode("utf-8", "replace").strip()
        if mode == "cancel-wrong-id":
            time.sleep(3)
            still = builder_running()
            p.send_signal(signal.SIGTERM)
            wrong = {"reply": wrong, "build_still_running": still}
    try:
        out, err = p.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        out, err = b"", b"(no result within 120 s)"
    line = out.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
    try:
        status = json.loads(line[0]).get("status")
    except ValueError:
        status = None
    print(json.dumps({"mode": mode, "status": status, "cancelled_mid_build": mid_build, "cancel_reply": wrong, "cancel_returncode": cancel_rc,
                      "elapsed_s": round(time.monotonic() - t_cancel, 1), "dispatch_line": line[0][:300], "stderr": err.decode("utf-8", "replace")[-300:]}))
    if mode == "cancel-wrong-id":
        return 0 if cancel_rc == 0 and '"not_running"' in wrong["reply"] and wrong["build_still_running"] and status == "cancelled" and mid_build else 1
    return 0 if status == "cancelled" and mid_build and (mode != "cancel-cmd" or cancel_rc == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
