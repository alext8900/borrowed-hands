#!/bin/sh
# Tests for deploy/railway/bh-start.sh . Needs a python with ruamel.yaml, so run it in the pinned image:
#   tests/deploy/run-in-image.sh
# or set BH_PY to such a python. Everything runs against temp dirs; no network, no Hermes.
set -u
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SCRIPT="$ROOT/deploy/railway/bh-start.sh"
PY="${BH_PY:-/opt/hermes/.venv/bin/python}"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
fail=0
ok()  { echo "PASS $1"; }
bad() { echo "FAIL $1"; fail=1; }

# Fake image layout: $T/bh holds the plugin and the default config; the fake entrypoint records its argv.
mkdir -p "$T/bh/plugins/borrowed-hands"
echo "v1" > "$T/bh/plugins/borrowed-hands/__init__.py"
printf 'plugins:\n  enabled:\n    - borrowed-hands\n  disabled: []\n' > "$T/bh/config.default.yaml"
cp "$ROOT/deploy/railway/bh-github-creds.py" "$T/bh/bh-github-creds.py"
cat > "$T/entry.sh" <<'E'
#!/bin/sh
echo "ENTRY $*" > "$BH_TEST_OUT"
E
chmod +x "$T/entry.sh"

run() { # run <home>
  HERMES_HOME="$1" BH_ROOT="$T/bh" BH_PY="$PY" BH_ENTRYPOINT="$T/entry.sh" BH_TEST_OUT="$T/out" \
    sh "$SCRIPT" gateway run 2> "$T/err"
}

# 1. empty volume: config copied, plugin installed, entrypoint gets the args
H1="$T/h1"; mkdir -p "$H1"
run "$H1" || bad "1 exit"
grep -q 'borrowed-hands' "$H1/config.yaml" && ok "1 config created" || bad "1 config created"
[ "$(cat "$H1/plugins/borrowed-hands/__init__.py")" = v1 ] && ok "1 plugin copied" || bad "1 plugin copied"
[ "$(cat "$T/out")" = "ENTRY gateway run" ] && ok "1 entrypoint args" || bad "1 entrypoint args"
[ -d "$H1/plugin-data" ] && ok "1 plugin-data dir" || bad "1 plugin-data dir"

# 2. second start with a user-modified config: never overwritten; user keys survive; plugin stays enabled once
cat > "$H1/config.yaml" <<'E'
# my comment
model:
  default: my-model
plugins:
  enabled:
    - borrowed-hands
    - other
E
cp "$H1/config.yaml" "$T/before"
run "$H1" || bad "2 exit"
cmp -s "$H1/config.yaml" "$T/before" && ok "2 config untouched when already enabled" || bad "2 config untouched"

# 3. user config without the plugin enabled: only that list gains the entry, comments and other keys kept
cat > "$H1/config.yaml" <<'E'
# keep me
model:
  default: my-model
plugins:
  enabled:
    - other
E
run "$H1" || bad "3 exit"
grep -q 'keep me' "$H1/config.yaml" && grep -q 'my-model' "$H1/config.yaml" && grep -q '    - other' "$H1/config.yaml" \
  && grep -q '    - borrowed-hands' "$H1/config.yaml" && ok "3 merge keeps user config" || bad "3 merge keeps user config"

# 4. config with no plugins section at all
printf 'model:\n  default: x\n' > "$H1/config.yaml"
run "$H1" || bad "4 exit"
grep -q 'borrowed-hands' "$H1/config.yaml" && grep -q 'default: x' "$H1/config.yaml" && ok "4 adds plugins.enabled" || bad "4 adds plugins.enabled"

# 5. image wins for plugin code: new image replaces the plugin dir, stale files removed; plugin-data kept
echo v2 > "$T/bh/plugins/borrowed-hands/__init__.py"
echo stale > "$H1/plugins/borrowed-hands/old.py"
echo keep > "$H1/plugin-data/state"
run "$H1" || bad "5 exit"
[ "$(cat "$H1/plugins/borrowed-hands/__init__.py")" = v2 ] && [ ! -e "$H1/plugins/borrowed-hands/old.py" ] \
  && ok "5 plugin replaced" || bad "5 plugin replaced"
[ "$(cat "$H1/plugin-data/state")" = keep ] && ok "5 plugin-data kept" || bad "5 plugin-data kept"

# 6. unparseable user config: left byte for byte, fail before handing off
printf 'plugins: [unclosed\n  enabled: {\n' > "$H1/config.yaml"
cp "$H1/config.yaml" "$T/before"
rm -f "$T/out"
if run "$H1"; then bad "6 must refuse startup"; else ok "6 refuses startup"; fi
cmp -s "$H1/config.yaml" "$T/before" && ok "6 bad config untouched" || bad "6 bad config untouched"
grep -q "ERROR: config merge refused" "$T/err" && ok "6 warns" || bad "6 warns"
[ ! -e "$T/out" ] && ok "6 no handoff on invalid policy" || bad "6 no handoff"

# 7. other-profile guard: a different plugin dir in the volume is not touched
mkdir -p "$H1/plugins/other"; echo o > "$H1/plugins/other/x"
cp "$T/bh/config.default.yaml" "$H1/config.yaml"
run "$H1" || bad "7 exit"
[ "$(cat "$H1/plugins/other/x")" = o ] && ok "7 other plugins untouched" || bad "7 other plugins untouched"

# 8. Fresh boot never creates root-owned logs or starts a lifetime log mirror.
[ ! -e "$H1/logs" ] && ok "8 fresh logs left to stock stage2" || bad "8 root logs created"
if [ "$(id -u)" = 0 ]; then
  for path in plugins plugin-data config.yaml; do
    [ "$(stat -c %U:%G "$H1/$path")" = hermes:hermes ] && ok "8 $path owned by hermes" || bad "8 $path ownership"
  done
  mkdir -p "$H1/logs"; echo legacy > "$H1/logs/agent.log"
  run "$H1" || bad "9 repair exit"
  [ "$(stat -c %U:%G "$H1/logs")" = hermes:hermes ] && \
    [ "$(stat -c %U:%G "$H1/logs/agent.log")" = hermes:hermes ] && ok "9 legacy log ownership repaired" || bad "9 legacy log ownership"
fi

# 10. Legacy log repair must not recurse through a top-level symlink.
mkdir -p "$T/outside"; echo keep > "$T/outside/probe"
rm -rf "$H1/logs"; ln -s "$T/outside" "$H1/logs"
rm -f "$T/out"
if run "$H1"; then bad "10 symlink logs refused"; else ok "10 symlink logs refused"; fi
[ ! -e "$T/out" ] && grep -q 'logs must not be a symlink' "$T/err" && ok "10 no handoff" || bad "10 no handoff"
[ "$(cat "$T/outside/probe")" = keep ] && ok "10 outside untouched" || bad "10 outside untouched"

# 11. Root startup must reject config links before reading, copying, chmodding or handing off.
for kind in directory file dangling; do
  home="$T/config-link-$kind"; mkdir -p "$home"
  target="$T/config-target-$kind"
  case "$kind" in
    directory) mkdir "$target"; echo keep > "$target/probe"; chmod 755 "$target" ;;
    file) printf 'token: FAKE-ROOT-YAML-CANARY\n' > "$target"; chmod 600 "$target"; cp "$target" "$T/target-before" ;;
  esac
  ln -s "$target" "$home/config.yaml"
  rm -f "$T/out"
  if run "$home"; then bad "11 $kind config link refused"; else ok "11 $kind config link refused"; fi
  [ -L "$home/config.yaml" ] && [ ! -e "$T/out" ] && \
    grep -q 'config.yaml must be a regular file, not a symlink' "$T/err" && \
    ! grep -q 'config.yaml created\|FAKE-ROOT-YAML-CANARY' "$T/err" \
    && ok "11 $kind no read or handoff" || bad "11 $kind no read or handoff"
  case "$kind" in
    directory) [ "$(stat -c %a "$target")" = 755 ] && [ "$(ls -A "$target")" = probe ] && \
      [ "$(cat "$target/probe")" = keep ] && ok "11 directory target untouched" || bad "11 directory target untouched" ;;
    file) cmp -s "$target" "$T/target-before" && [ "$(stat -c %a "$target")" = 600 ] && \
      ok "11 file target untouched" || bad "11 file target untouched" ;;
    dangling) [ ! -e "$target" ] && ok "11 dangling target absent" || bad "11 dangling target absent" ;;
  esac
done

# 12. A gateway-owned parent link must not redirect root's plugin replacement.
chmod 711 "$T"
for entry in plugins plugin-data; do
  home="$T/parent-link-$entry"; target="$T/parent-target-$entry"
  mkdir -p "$home" "$target/borrowed-hands"
  chmod 700 "$home" "$target" "$target/borrowed-hands"
  echo KEEP_ROOT_CANARY > "$target/borrowed-hands/secret"; chmod 600 "$target/borrowed-hands/secret"
  echo KEEP_OTHER > "$target/other"
  if [ "$(id -u)" = 0 ]; then
    chown hermes:hermes "$home"
    su hermes -s /bin/sh -c "ln -s '$target' '$home/$entry'" || bad "12 $entry gateway plant"
    if su hermes -s /bin/sh -c "test -r '$target/borrowed-hands/secret'"; then
      bad "12 $entry root canary private"
    else
      ok "12 $entry root canary private"
    fi
  else
    ln -s "$target" "$home/$entry"
  fi
  rm -f "$T/out"
  if run "$home"; then bad "12 $entry parent link refused"; else ok "12 $entry parent link refused"; fi
  [ -L "$home/$entry" ] && [ ! -e "$T/out" ] && [ ! -e "$home/config.yaml" ] && \
    grep -q "$entry must not be a symlink" "$T/err" && ! grep -q 'seeded' "$T/err" \
    && ok "12 $entry no handoff or seeding" || bad "12 $entry no handoff or seeding"
  [ "$(cat "$target/borrowed-hands/secret" 2>/dev/null)" = KEEP_ROOT_CANARY ] && \
    [ "$(cat "$target/other")" = KEEP_OTHER ] && [ "$(stat -c %a "$target")" = 700 ] && \
    [ ! -e "$target/borrowed-hands/__init__.py" ] \
    && ok "12 $entry outside tree unchanged" || bad "12 $entry outside tree unchanged"
done

# 13. Pointing plugins at the image's source must not erase that source before copying.
home="$T/source-link-home"; mkdir "$home"
ln -s "$T/bh/plugins" "$home/plugins"
rm -f "$T/out"
if run "$home"; then bad "13 source-parent link refused"; else ok "13 source-parent link refused"; fi
[ "$(cat "$T/bh/plugins/borrowed-hands/__init__.py" 2>/dev/null)" = v2 ] && \
  [ ! -e "$T/out" ] && grep -q 'plugins must not be a symlink' "$T/err" \
  && ok "13 image plugin source unchanged" || bad "13 image plugin source unchanged"

exit $fail
