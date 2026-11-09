#!/bin/bash
# Stand-in macro plugin: swift-frontend runs it as an executable plugin (-load-plugin-executable).
# It records what it can reach next to itself (the checkout is writable under builder.sb), then
# exits without answering, so the macro expansion and the build fail.
# probe-config (written by the test into the checkout, never committed):
#   line 1: a directory outside the job (holds canary.txt; the plugin tries to write there too)
#   line 2: the owner's real home directory
#   line 3: a TCP port on 127.0.0.1 the test is listening on
here=$(cd "$(dirname "$0")" && pwd -P)
out="$here/probe-results.txt"
{ read -r outside; read -r owner_home; read -r port; } < "$here/probe-config"
r() { if "$@" >/dev/null 2>&1; then echo "$label=ALLOWED"; else echo "$label=DENIED"; fi; }
{
  echo "ran=1 pid=$$ ppid=$PPID parent=$(/bin/ps -o comm= -p "$PPID" 2>/dev/null || echo unknown)"
  label=read_outside_canary r /bin/cat "$outside/canary.txt"
  label=write_outside r /bin/sh -c "echo x > '$outside/written-by-plugin'"
  label=list_owner_home r /bin/ls "$owner_home"
  label=tcp_localhost r /bin/bash -c "exec 3<>/dev/tcp/127.0.0.1/$port"
  label=write_checkout r /bin/sh -c "echo x > '$here/written-by-plugin'"
} > "$out" 2>&1
exit 0
