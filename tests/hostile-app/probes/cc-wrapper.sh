#!/bin/bash
# Project-controlled compiler wrapper (CC build setting). A hostile repo can point CC at any
# script it ships; this one runs the probe suite once per build, then hands off to the real clang.
HERE=$(cd "$(dirname "$0")" && pwd)
STAMP="${TARGET_TEMP_DIR:-${TMPDIR:-/tmp}}/bh-cc-probed"
if [ ! -e "$STAMP" ]; then
  : >"$STAMP" 2>/dev/null
  "$HERE/probe.sh" cc
fi
exec "${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang" "$@"
