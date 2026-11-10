#!/bin/sh
# Tests for the optional GitHub push credential in deploy/railway/bh-start.sh (BH_GITHUB_TOKEN + BH_GITHUB_REPO).
# Run inside the pinned image as root (tests/deploy/run-in-image.sh): it needs the hermes user, git and a python.
# Uses only fake tokens built at runtime, temp dirs, and `git credential fill`; no network.
set -u
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SCRIPT="$ROOT/deploy/railway/bh-start.sh"
PY="${BH_PY:-/opt/hermes/.venv/bin/python}"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
chmod 711 "$T"
fail=0
ok()  { echo "PASS $1"; }
bad() { echo "FAIL $1"; fail=1; }

if [ "$(id -u)" != 0 ] || ! id hermes >/dev/null 2>&1; then
  echo "FAIL needs root and a hermes user (run tests/deploy/run-in-image.sh)"; exit 1
fi

# Fake tokens of the fine-grained shape, assembled so no token-shaped literal sits in the source.
P=github
A74=$(printf 'A%.0s' $(seq 74)); B74=$(printf 'B%.0s' $(seq 74))
TOK1="${P}_pat_11BHTEST${A74}"
TOK2="${P}_pat_11BHTEST${B74}"
REPO=octo-org/demo.repo_1
GH="https://github.com/$REPO"

mkdir -p "$T/bh/plugins/borrowed-hands"
echo v1 > "$T/bh/plugins/borrowed-hands/__init__.py"
printf 'plugins:\n  enabled:\n    - borrowed-hands\n  disabled: []\n' > "$T/bh/config.default.yaml"
cp "$ROOT/deploy/railway/bh-github-creds.py" "$T/bh/bh-github-creds.py"
# The fake entrypoint is exec'd in place of Hermes: record argv and the exact environment it was given.
cat > "$T/entry.sh" <<'E'
#!/bin/sh
echo "ENTRY $*" > "$BH_TEST_OUT"
/usr/bin/tr '\0' '\n' < /proc/$$/environ > "$BH_TEST_OUT.environ"
E
chmod +x "$T/entry.sh"
# Every python the start script runs goes through this wrapper, which records its environment and argv.
cat > "$T/pywrap.sh" <<'E'
#!/bin/sh
{ /usr/bin/tr '\0' '\n' < /proc/$$/environ; /usr/bin/tr '\0' ' ' < /proc/$$/cmdline; echo; } >> "$BH_TEST_PYLOG"
exec "$BH_TEST_REALPY" "$@"
E
chmod +x "$T/pywrap.sh"
H="$T/h"; mkdir -p "$H"
E="$T/etc/bh-github"          # stands in for /etc/bh-github: root 0755, container filesystem
GC="$E/gitconfig"; mkdir -p "$T/etc"
D="$E/creds"                  # the credential directory
L="$H/.config/bh-github"      # the old location on the volume, swept on every start
ALL="$T/all-output"; : > "$ALL"
mkdir -p "$T/hh"; chown hermes:hermes "$T/hh"
HOLDERS_ALLOWED="$D/api-header $D/git-credentials"

# run <token-or-UNSET> <repo-or-UNSET>: start with those variables; output, the entry's env and every python's env
# and argv go to $ALL. Afterwards no file other than the two credential files may hold either token.
run() {
  rm -f "$T/out" "$T/out.environ" "$T/pylog"
  (
    umask 000   # hostile umask: the script must not depend on the caller's
    [ "$1" = UNSET ] || export BH_GITHUB_TOKEN="$1"
    [ "$2" = UNSET ] || export BH_GITHUB_REPO="$2"
    HERMES_HOME="$H" BH_ROOT="$T/bh" BH_PY="${RUN_PY:-$T/pywrap.sh}" BH_TEST_REALPY="$PY" BH_TEST_PYLOG="$T/pylog" \
      BH_ENTRYPOINT="$T/entry.sh" BH_TEST_OUT="$T/out" BH_GITCONFIG="$GC" PATH="${RUN_PATH_PREFIX-}$PATH" \
      /bin/sh "$SCRIPT" gateway run > "$T/stdout" 2> "$T/err"
  )
  rc=$?
  cat "$T/stdout" "$T/err" >> "$ALL"
  [ -f "$T/out.environ" ] && cat "$T/out.environ" >> "$ALL"
  if [ -f "$T/pylog" ]; then
    cat "$T/pylog" >> "$ALL"
    grep -q -F -e "$TOK1" -e "$TOK2" -e BH_GITHUB_TOKEN= "$T/pylog" && bad "run: token reached a python's env or argv"
  fi
  extra=$(grep -rlF -e "$TOK1" -e "$TOK2" "$H" "$(dirname "$GC")" "$T/bh" 2>/dev/null | sort | while read -r f; do
    case " $HOLDERS_ALLOWED " in (*" $f "*) ;; (*) echo "$f" ;; esac; done)
  [ -z "$extra" ] || bad "run: token found in an unexpected file: $extra"
  return $rc
}
booted() { [ "$(cat "$T/out" 2>/dev/null)" = "ENTRY gateway run" ]; }
nothing_written() { [ ! -e "$D/git-credentials" ] && [ ! -e "$D/api-header" ] && [ ! -e "$GC" ]; }
# as_hermes <cmd...>: run as hermes with the git config Hermes gets: no system config (the image has no include
# there) plus the environment include bh-start.sh exports.
as_hermes() {
  env -i PATH="$PATH" HOME="$T/hh" GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 \
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=include.path GIT_CONFIG_VALUE_0="$GC" \
    setpriv --reuid hermes --regid hermes --init-groups "$@"
}
# fill <url>: the password git would use for url.
fill() { printf 'url=%s\n\n' "$1" | as_hermes git credential fill 2>/dev/null | sed -n 's/^password=//p'; }

# 1. Neither variable: V1 behaviour, nothing created, gateway handed off.
run UNSET UNSET || bad "1 exit"
booted && ok "1 boots without credentials" || bad "1 boots without credentials"
[ ! -e "$H/.config" ] && [ ! -e "$GC" ] && [ ! -e "$D" ] && ok "1 nothing created" || bad "1 nothing created"

# 2. Valid token and repo: two files, owner-only, owned by hermes; git config root-owned and secret-free.
run "$TOK1" "$REPO" || bad "2 exit"
booted && ok "2 boots" || bad "2 boots"
[ "$(stat -c '%U:%G %a' "$D")" = "hermes:hermes 700" ] && ok "2 dir hermes 0700" || bad "2 dir mode/owner: $(stat -c '%U:%G %a' "$D")"
for f in git-credentials api-header; do
  [ -f "$D/$f" ] && [ ! -L "$D/$f" ] && [ "$(stat -c '%U:%G %a %h' "$D/$f")" = "hermes:hermes 600 1" ] \
    && ok "2 $f hermes 0600" || bad "2 $f mode/owner"
done
[ "$(cat "$D/git-credentials")" = "$(printf 'https://x-access-token:%s@github.com/%s.git\nhttps://x-access-token:%s@github.com/%s' "$TOK1" "$REPO" "$TOK1" "$REPO")" ] \
  && ok "2 credential store lines" || bad "2 credential store lines"
[ "$(cat "$D/api-header")" = "Authorization: Bearer $TOK1" ] && ok "2 api header" || bad "2 api header"
[ "$(stat -c '%U %a' "$GC")" = "root 644" ] && [ "$(stat -c '%U %a' "$E")" = "root 755" ] \
  && ok "2 git config root-owned 0644" || bad "2 git config owner/mode"
# Nothing on the volume: no .config created, the store path in the git config points at the container filesystem.
[ ! -e "$H/.config" ] && ok "2 nothing written under HERMES_HOME" || bad "2 wrote under HERMES_HOME"
grep -q -F -- "--file=$D/git-credentials get" "$GC" && ! grep -q -F "$H" "$GC" \
  && ok "2 git config points at the new store path" || bad "2 store path in git config"
# The gateway's uid cannot replace the credential directory: its parent is root's.
setpriv --reuid hermes --regid hermes --init-groups sh -c "mv '$D' '$E/x' || ln -s /tmp '$E/y'" 2>/dev/null \
  && bad "2 hermes can rename or add in the parent" || ok "2 hermes cannot touch the credential dir's parent"
grep -q "$TOK1" "$GC" && bad "2 git config holds the token" || ok "2 git config has no token"
setpriv --reuid hermes --regid hermes --init-groups sh -c "echo x >> '$GC'" 2>/dev/null \
  && bad "2 hermes can write git config" || ok "2 hermes cannot write git config"
ls -A "$D" | grep -v -x -e git-credentials -e api-header | grep -q . && bad "2 stray files in dir" || ok "2 no temp files left"
grep -q 'github credentials: configured' "$T/err" && ! grep -q -F -e "$REPO" -e octo-org "$T/err" \
  && ok "2 success message names no repository value" || bad "2 success message"

# 3. The token is gone from the environment Hermes is exec'd with.
grep -q '^BH_GITHUB_TOKEN=' "$T/out.environ" && bad "3 variable reached hermes" || ok "3 variable unset before exec"
grep -q "$TOK1" "$T/out.environ" && bad "3 value reached hermes env" || ok "3 value not in hermes env"
grep -q '^BH_GITHUB_REPO=' "$T/out.environ" && bad "3 repo variable reached hermes" || ok "3 repo variable unset too"
# Hermes (and through it every agent command) gets the git config as an environment include, read last.
grep -q '^GIT_CONFIG_COUNT=1$' "$T/out.environ" && grep -q '^GIT_CONFIG_KEY_0=include.path$' "$T/out.environ" && \
  grep -q "^GIT_CONFIG_VALUE_0=$GC\$" "$T/out.environ" && ok "3 environment include handed to hermes" || bad "3 environment include"
# The script's own PATH is fixed, and Hermes gets the caller's PATH back.
grep -q "^PATH=$PATH\$" "$T/out.environ" && ok "3 original PATH restored before exec" || bad "3 PATH not restored"

# 4. Scoping: only https://github.com/<repo>(.git) gets the credential.
[ "$(fill "$GH.git")" = "$TOK1" ] && ok "4 repo .git url gets credential" || bad "4 repo .git url"
[ "$(fill "$GH")" = "$TOK1" ] && ok "4 repo url gets credential" || bad "4 repo url"
for u in https://github.com/octo-org/other.git https://github.com/octo-org/demo.repo_12.git \
         https://github.com/other/demo.repo_1.git "$GH/sub.git" "$GH.gitx" http://github.com/$REPO.git \
         https://gitlab.com/$REPO.git https://github.com.evil.example/$REPO.git https://evil.example/github.com/$REPO.git \
         https://api.github.com/$REPO.git https://x@github.com/$REPO.git; do
  [ -z "$(fill "$u")" ] && ok "4 no credential for $u" || bad "4 credential leaked to $u"
done
# Each layer on its own: no helper is even selected outside the repo URL (useHttpPath is the second layer).
helper_for() { as_hermes git config --get-urlmatch credential.helper "$1" 2>/dev/null; }
[ -n "$(helper_for "$GH.git")" ] && [ -z "$(helper_for https://github.com/octo-org/other.git)" ] && \
  [ -z "$(helper_for https://github.com/)" ] && ok "4 helper selected only for the repo URL" || bad "4 helper selected outside repo"
# Defense in depth: even an unscoped helper the agent adds globally answers only for the repo path.
printf '[credential]\n\thelper = store --file=%s\n' "$D/git-credentials" > "$T/hh/.gitconfig"; chown hermes "$T/hh/.gitconfig"
[ -z "$(fill https://github.com/octo-org/other.git)" ] && ok "4 useHttpPath limits an unscoped helper" || bad "4 unscoped helper leaks"
rm -f "$T/hh/.gitconfig"
# The helper is read-only: git's store/erase (after a push or a 401) never rewrite or delete the file.
cp "$D/git-credentials" "$T/cred-before"
for action in reject approve; do
  printf 'url=%s.git\nusername=x-access-token\npassword=other\n\n' "$GH" | as_hermes git credential "$action" 2>/dev/null
done
cmp -s "$D/git-credentials" "$T/cred-before" && [ "$(stat -c %a "$D/git-credentials")" = 600 ] \
  && ok "4 helper never writes the store" || bad "4 helper wrote the store"
# 4b. A second helper the agent adds (the bundled GitHub skill says `git config --global credential.helper store`)
# never receives the token: after a successful push git sends "store" to every helper; ours resets the list first.
printf '[credential]\n\thelper = store\n' > "$T/hh/.gitconfig"; chown hermes "$T/hh/.gitconfig"
spill() { # spill yes|no: what git does after a successful push, with or without the environment include
  rm -f "$T/hh/.git-credentials"
  if [ "$1" = yes ]; then
    printf 'url=%s.git\n\n' "$GH" | as_hermes git credential fill 2>/dev/null | as_hermes git credential approve
  else
    printf 'url=%s.git\n\n' "$GH" | as_hermes env -u GIT_CONFIG_COUNT git credential fill 2>/dev/null | \
      as_hermes env -u GIT_CONFIG_COUNT git credential approve
  fi
}
spill no; [ ! -e "$T/hh/.git-credentials" ] && ok "4b without the environment include there is nothing to copy" || bad "4b token copied without the include"
[ -z "$(printf 'url=%s.git\n\n' "$GH" | as_hermes env -u GIT_CONFIG_COUNT git credential fill 2>/dev/null | sed -n 's/^password=//p')" ] \
  && ok "4b a child without the environment include gets no credential" || bad "4b credential offered without the include"
spill yes; [ ! -e "$T/hh/.git-credentials" ] && ok "4b user-level store helper gets no copy" || bad "4b token copied to ~/.git-credentials"
# ...and a user-level useHttpPath=false plus an unscoped helper on our store still answers only for the repo.
printf '[credential]\n\thelper = store --file=%s\n\tuseHttpPath = false\n' "$D/git-credentials" > "$T/hh/.gitconfig"
[ -z "$(fill https://github.com/octo-org/other.git)" ] && ok "4b user useHttpPath=false overridden" || bad "4b useHttpPath override"
rm -f "$T/hh/.gitconfig" "$T/hh/.git-credentials"

# 5. Rotation: a changed token replaces both files atomically.
run "$TOK2" "$REPO" || bad "5 exit"
grep -q "$TOK2" "$D/git-credentials" && grep -q "$TOK2" "$D/api-header" && ! grep -q "$TOK1" "$D/git-credentials" "$D/api-header" \
  && ok "5 token replaced" || bad "5 token replaced"
[ "$(fill "$GH.git")" = "$TOK2" ] && ok "5 git uses the new token" || bad "5 git uses the new token"

# 6. Revocation: removing the token variable deletes both files and the git config, and still boots.
run UNSET "$REPO" || bad "6 exit"
booted && nothing_written && [ ! -e "$D" ] && ok "6 token removed: files, dir and git config deleted" || bad "6 cleanup on removal"
grep -q 'removed' "$T/err" && ok "6 says it removed them" || bad "6 removal message"

# 7. Bad token shapes: write nothing, drop any previous credential, name the variable not the value, still boot.
nl='
'
for case in classic short long space newline prefix-only lowercase-prefix unicode empty-suffix; do
  case "$case" in
    classic) v="ghp_${A74}" ;;
    short) v="${P}_pat_ABC" ;;
    long) v="${P}_pat_$(printf 'A%.0s' $(seq 300))" ;;
    space) v="${P}_pat_11BHTEST ${A74}" ;;
    newline) v="${TOK1}${nl}" ;;
    prefix-only) v="${P}_pat_" ;;
    lowercase-prefix) v="GITHUB_PAT_11BHTEST${A74}" ;;
    unicode) v="${P}_pat_11BHTEST${A74}é" ;;
    empty-suffix) v="${TOK1}%" ;;
  esac
  run "$TOK1" "$REPO" >/dev/null || bad "7 $case setup"
  run "$v" "$REPO" || bad "7 $case exit"
  booted && nothing_written && ok "7 $case refused, nothing written, boots" || bad "7 $case"
  grep -q 'BH_GITHUB_TOKEN' "$T/err" && ! grep -q -F "$A74" "$T/err" && ok "7 $case message without value" || bad "7 $case message"
done

# 8. Bad or missing repo: refuse before writing anything; the value is never echoed.
for r in UNSET "" octo "octo/demo/x" "../demo" "octo/.." "octo/." "octo/demo.git" "$GH" "octo/demo;rm" "-octo/demo" \
         "octo/de mo" "octo/demo${nl}" "octo-/demo" "$TOK1/x" "octo/$TOK1" "x/ghp_${A74}" "x/GITHUB_PAT_${A74}" \
         "x/gho_abc" "x/ghs_abc" "x/ghu_abc" "x/ghr_abc" "octo/demo$TOK1"; do
  run "$TOK1" "$REPO" >/dev/null || bad "8 setup"
  run "$TOK1" "$r" || bad "8 '$r' exit"
  booted && nothing_written && grep -q 'BH_GITHUB_REPO' "$T/err" && ok "8 repo '$r' refused" || bad "8 repo '$r'"
  case "$r" in ""|UNSET|*"$nl"*) ;; *) grep -q -F -- "$r" "$T/err" && bad "8 repo value '$r' echoed" ;; esac
done

# 9. Symlinks and foreign ownership are refused without following them; git config is not left behind.
mkdir -p "$T/outside"; echo keep > "$T/outside/git-credentials"; chmod 644 "$T/outside/git-credentials"
outside_ok() { [ "$(cat "$T/outside/git-credentials")" = keep ] && [ "$(stat -c '%U %a' "$T/outside/git-credentials")" = "root 644" ] \
  && [ "$(ls -A "$T/outside")" = git-credentials ]; }
reset() { rm -rf "$H/.config" "$E"; mkdir -p "$E"; chmod 755 "$E"; }
# 9a dir is a symlink
reset; ln -s "$T/outside" "$D"
run "$TOK1" "$REPO" || bad "9a exit"
booted && outside_ok && [ ! -e "$GC" ] && grep -q 'refused' "$T/err" && ok "9a symlinked dir refused" || bad "9a symlinked dir"
# 9b old location: a symlinked .config is not followed by the sweep, and install at the new location still works
reset; rm -rf "$H/.config"; ln -s "$T/outside" "$H/.config"
run "$TOK1" "$REPO" || bad "9b exit"
booted && outside_ok && [ "$(fill "$GH.git")" = "$TOK1" ] && grep -q 'old location not checked' "$T/err" \
  && ok "9b symlinked .config not followed, new location unaffected" || bad "9b symlinked .config"
rm -f "$H/.config"
# 9c credential file is a symlink
reset; mkdir -m 700 "$D"; chown hermes:hermes "$D"; ln -s "$T/outside/git-credentials" "$D/git-credentials"
run "$TOK1" "$REPO" || bad "9c exit"
booted && outside_ok && [ ! -e "$GC" ] && ok "9c symlinked file refused" || bad "9c symlinked file"
# 9d header file is a hard link to a file elsewhere (owned by hermes, so only the link count can refuse it)
mkdir -p "$T/outside-h"; echo keep > "$T/outside-h/other"; chown hermes:hermes "$T/outside-h/other"
reset; mkdir -m 700 "$D"; chown hermes:hermes "$D"; ln "$T/outside-h/other" "$D/api-header"
run "$TOK1" "$REPO" || bad "9d exit"
# Install refuses it; the cleanup that follows unlinks only our name, so the other file keeps its content.
booted && [ "$(cat "$T/outside-h/other")" = keep ] && [ "$(stat -c %h "$T/outside-h/other")" = 1 ] && [ ! -e "$GC" ] && \
  [ ! -e "$D/git-credentials" ] && grep -q 'hard link' "$T/err" && ok "9d hard-linked file refused" || bad "9d hard-linked file"
# 9e dir owned by someone else
reset; mkdir -m 700 "$D"; chown nobody "$D"
run "$TOK1" "$REPO" || bad "9e exit"
booted && [ ! -e "$D/git-credentials" ] && [ ! -e "$GC" ] && ok "9e foreign-owned dir refused" || bad "9e foreign-owned dir"
# 9f the same refusals hold on removal: cleanup never follows a planted link
reset; ln -s "$T/outside" "$D"
run UNSET UNSET || bad "9f exit"
booted && outside_ok && [ ! -e "$D" ] && [ ! -L "$D" ] && ok "9f cleanup removes the link, never its target" || bad "9f cleanup follows symlink"
# 9g git config directory is a symlink
reset; rm -rf "$E"; mkdir -p "$T/outside-etc"; ln -s "$T/outside-etc" "$E"
run "$TOK1" "$REPO" || bad "9g exit"
booted && [ -z "$(ls -A "$T/outside-etc")" ] && ok "9g symlinked git config dir refused" || bad "9g"
rm -f "$E"
# 9h stale temp files from an interrupted run are removed on the next start
reset; run "$TOK1" "$REPO" >/dev/null; echo "$TOK1" > "$D/.git-credentials.tmp-dead"; chown hermes "$D/.git-credentials.tmp-dead"
run UNSET UNSET; [ ! -e "$D" ] && ok "9h stale temp files cleaned" || bad "9h stale temp files"
# 9j dir is a symlink to a hermes-owned dir elsewhere: refused on the link itself, not on ownership
mkdir -p "$T/outside-hd"; chown hermes:hermes "$T/outside-hd"; chmod 700 "$T/outside-hd"
reset; ln -s "$T/outside-hd" "$D"
run "$TOK1" "$REPO" || bad "9j exit"
booted && [ -z "$(ls -A "$T/outside-hd")" ] && [ ! -e "$GC" ] && ok "9j symlink to hermes-owned dir refused" || bad "9j"
# 9i a pre-existing, too-open directory and files are tightened, not trusted
reset; mkdir -m 755 "$D"; chmod 755 "$D"; printf 'old\n' > "$D/git-credentials"; chmod 644 "$D/git-credentials"
chown -R hermes:hermes "$D"
run "$TOK1" "$REPO" || bad "9i exit"
[ "$(stat -c %a "$D")" = 700 ] && [ "$(stat -c %a "$D/git-credentials")" = 600 ] && [ "$(stat -c %a "$D/api-header")" = 600 ] \
  && ok "9i existing modes tightened to 0700/0600" || bad "9i modes: $(stat -c %a "$D")"

# 11. Removal removes, whatever is at the names, and one odd entry does not block the rest.
reset; run "$TOK1" "$REPO" >/dev/null; ln "$D/git-credentials" "$T/hh/cred-copy"
HOLDERS_ALLOWED="$HOLDERS_ALLOWED $T/hh/cred-copy"
run UNSET UNSET || bad "11a exit"
[ ! -e "$D" ] && grep -q 'hard link' "$T/err" && ok "11a hard-linked store removed, extra link reported" || bad "11a"
rm -f "$T/hh/cred-copy"
reset; run "$TOK1" "$REPO" >/dev/null; ln "$D/api-header" "$T/hh/hdr-copy"
run UNSET UNSET || bad "11b exit"
[ ! -e "$D" ] && ok "11b hard-linked header removed, store too" || bad "11b"
rm -f "$T/hh/hdr-copy"
reset; run "$TOK1" "$REPO" >/dev/null; chown nobody "$D/git-credentials"
run UNSET UNSET; [ ! -e "$D/git-credentials" ] && [ ! -e "$D/api-header" ] && ok "11c foreign-owned file removed" || bad "11c"
reset; mkdir -m 700 "$D"; mkdir "$D/.bh-tmp-x"; chown -R hermes:hermes "$D"
run "$TOK1" "$REPO" || bad "11d exit"
[ "$(fill "$GH.git")" = "$TOK1" ] && ok "11d a stray temp directory does not block install" || bad "11d"
mkdir "$D/.bh-tmp-y"; chown hermes:hermes "$D/.bh-tmp-y"
run UNSET UNSET; [ ! -e "$D" ] && ok "11d an empty stray temp directory is tidied on removal" || bad "11d tidy"
reset; mkdir -m 700 "$D"; mkdir -p "$D/git-credentials/sub"; chown -R hermes:hermes "$D"
run UNSET UNSET; [ ! -e "$GC" ] && grep -q 'not removed' "$T/err" && ok "11e a directory at a credential name is reported" || bad "11e"
rm -rf "$D"

# 12. Early exits never skip the credential step: a stale credential cannot survive a start without the token.
reset; run "$TOK1" "$REPO" >/dev/null
ln -s "$T/outside" "$H/logs"
run UNSET UNSET && bad "12 exit expected" || true
grep -q 'logs must not be a symlink' "$T/err" && [ ! -e "$D" ] && [ ! -e "$GC" ] && ok "12 credential removed before the early exit" || bad "12"
rm -f "$H/logs"
# ...and before the first exit the script has (config.yaml), not only the last one.
reset; run "$TOK1" "$REPO" >/dev/null
mv "$H/config.yaml" "$T/config.keep"; ln -s "$T/outside" "$H/config.yaml"
run UNSET UNSET && bad "12b exit expected" || true
grep -q 'config.yaml must be a regular file' "$T/err" && [ ! -e "$D" ] && [ ! -e "$GC" ] && ok "12b credential removed before the first early exit" || bad "12b"
rm -f "$H/config.yaml"; mv "$T/config.keep" "$H/config.yaml"

# 13. The script's own commands come from a fixed system PATH; a binary planted on the caller's PATH never runs.
mkdir -p "$T/evilbin"
for c in mkdir rm cp id chown cat tr sed grep; do
  printf '#!/bin/sh\necho %s >> %s/evil-ran\nexec /usr/bin/%s "$@"\n' "$c" "$T" "$c" > "$T/evilbin/$c"; chmod 755 "$T/evilbin/$c"
done
rm -f "$T/evil-ran"
RUN_PATH_PREFIX="$T/evilbin:" run "$TOK1" "$REPO" || bad "13 exit"
booted && [ ! -e "$T/evil-ran" ] && ok "13 planted PATH binaries not run" || bad "13 planted binary ran: $(cat "$T/evil-ran" 2>/dev/null | tr '\n' ' ')"
grep -q "^PATH=$T/evilbin:" "$T/out.environ" && ok "13 caller PATH handed on unchanged" || bad "13 PATH handed on"

# 14. If the helper itself dies, git stops using the store and the message says the files were not checked.
cat > "$T/failpy.sh" <<'E'
#!/bin/sh
case "$2" in *bh-github-creds.py) exit 3 ;; esac
exec "$BH_TEST_REALPY" "$@"
E
chmod +x "$T/failpy.sh"
reset; run "$TOK1" "$REPO" >/dev/null
RUN_PY="$T/failpy.sh" run "$TOK1" "$REPO" || bad "14 exit"
booted && [ ! -e "$GC" ] && [ ! -e "$D" ] && grep -q 'NOT checked' "$T/err" \
  && ok "14 helper failure: git config and credential dir removed, old location reported unchecked" || bad "14"

# 15. A github.com credential copy elsewhere (an extra helper, an old skill step) is reported, never rewritten.
reset; mkdir -p "$H/home"; printf 'https://x-access-token:%s@github.com/%s.git\n' "$TOK1" "$REPO" > "$H/home/.git-credentials"
chown -R hermes:hermes "$H/home"; chmod 600 "$H/home/.git-credentials"; cp "$H/home/.git-credentials" "$T/copy-before"
HOLDERS_ALLOWED="$HOLDERS_ALLOWED $H/home/.git-credentials"
run UNSET UNSET || bad "15 exit"
grep -q "$H/home/.git-credentials" "$T/err" && grep -q -i 'revoke' "$T/err" && cmp -s "$T/copy-before" "$H/home/.git-credentials" \
  && [ "$(stat -c '%U %a' "$H/home/.git-credentials")" = "hermes 600" ] && ok "15 copy reported on removal, untouched" || bad "15 removal"
run "$TOK1" "$REPO" >/dev/null; grep -q "$H/home/.git-credentials" "$T/err" && ok "15 copy reported on install" || bad "15 install"
# A link at a copy path to a root-only file that does look like a credential: root must not read through it.
rm -rf "$H/home"; printf 'https://u:p@github.com/x/y.git\n' > "$T/root-only-cred"; chmod 600 "$T/root-only-cred"
ln -sf "$T/root-only-cred" "$H/.git-credentials"
run UNSET UNSET; ! grep -q "$H/.git-credentials" "$T/err" && [ -L "$H/.git-credentials" ] \
  && ok "15 a symlinked copy path is not followed" || bad "15 symlink followed"
rm -rf "$H/home" "$H/.git-credentials"; HOLDERS_ALLOWED="$D/api-header $D/git-credentials"

# 16. Upgrade: a credential left at the old volume location is swept on every start: with no token, with a
# token (install goes to the new location), and before an early exit. Odd entries there never block the sweep.
plant_legacy() {
  rm -rf "$L"; mkdir -p "$L"; printf 'https://x-access-token:%s@github.com/%s.git\n' "$TOK1" "$REPO" > "$L/git-credentials"
  printf 'Authorization: Bearer %s\n' "$TOK1" > "$L/api-header"; echo "$TOK1" > "$L/.bh-tmp-old"
  chown -R hermes:hermes "$H/.config"; chmod 700 "$L"; chmod 600 "$L/git-credentials" "$L/api-header"
}
reset; plant_legacy; run UNSET UNSET || bad "16a exit"
booted && [ ! -e "$L" ] && [ -d "$H/.config" ] && grep -q 'old location' "$T/err" && ok "16a old location swept without a token" || bad "16a"
reset; plant_legacy; run "$TOK1" "$REPO" || bad "16b exit"
[ ! -e "$L" ] && [ "$(fill "$GH.git")" = "$TOK1" ] && ok "16b old location swept on install" || bad "16b"
reset; plant_legacy; ln -s "$T/outside" "$H/logs"
run UNSET UNSET && bad "16c exit expected" || true
grep -q 'logs must not be a symlink' "$T/err" && [ ! -e "$L" ] && ok "16c old location swept before the early exit" || bad "16c"
rm -f "$H/logs"
reset; plant_legacy; mv "$H/config.yaml" "$T/config.keep"; ln -s "$T/outside" "$H/config.yaml"
run UNSET UNSET && bad "16c2 exit expected" || true
grep -q 'config.yaml must be a regular file' "$T/err" && [ ! -e "$L" ] && ok "16c2 old location swept before the first early exit" || bad "16c2"
rm -f "$H/config.yaml"; mv "$T/config.keep" "$H/config.yaml"
# The sweep removes only the names this script writes: a file of another name stays, in both places.
reset; plant_legacy; echo note > "$L/notes.txt"; run "$TOK1" "$REPO" >/dev/null; echo note > "$D/notes.txt"
run UNSET UNSET || bad "16f exit"
[ -f "$L/notes.txt" ] && [ -f "$D/notes.txt" ] && [ ! -e "$L/git-credentials" ] && [ ! -e "$D/git-credentials" ] \
  && ok "16f files of other names are kept" || bad "16f"
rm -rf "$L" "$D"
# Every start says what it did, also when there was nothing to remove; a token pasted into BH_GITHUB_REPO is named.
reset; run UNSET UNSET || bad "17 exit"
grep -q 'BH_GITHUB_TOKEN not set; no credential installed' "$T/err" && ok "17 a start without a token says so" || bad "17"
run UNSET "$TOK1" || bad "17b exit"
booted && grep -q 'refused: BH_GITHUB_REPO must be a plain owner/name' "$T/err" && nothing_written \
  && ok "17b a token in BH_GITHUB_REPO alone is refused by name" || bad "17b"
# An odd GIT_CONFIG_COUNT from the operator never stops the boot or breaks git.
for n in 08 00 12345678901234567890; do
  reset; ( export GIT_CONFIG_COUNT=$n; run "$TOK1" "$REPO" ) || bad "18 exit ($n)"
  grep -q '^GIT_CONFIG_COUNT=1$' "$T/out.environ" && ok "18 GIT_CONFIG_COUNT=$n counts as 0" || bad "18 ($n)"
done
reset; plant_legacy; ln "$L/git-credentials" "$T/hh/legacy-copy"; chown root "$L/api-header"; mkdir "$L/.bh-tmp-dir"
HOLDERS_ALLOWED="$HOLDERS_ALLOWED $T/hh/legacy-copy"
run UNSET UNSET || bad "16d exit"
[ ! -e "$L" ] && grep -q 'hard link' "$T/err" && ok "16d odd entries at the old location removed and reported" || bad "16d"
rm -f "$T/hh/legacy-copy"; HOLDERS_ALLOWED="$D/api-header $D/git-credentials"
reset; mkdir -p "$H/.config"; ln -s "$T/outside" "$L"
run UNSET UNSET; outside_ok && [ ! -L "$L" ] && ok "16e a symlink at the old location is unlinked, never followed" || bad "16e"
rm -rf "$H/.config"

# 10. No start output, and no environment handed to Hermes, ever carried a token or its base64.
for t in "$TOK1" "$TOK2"; do
  for needle in "$t" "$(printf '%s' "$t" | base64 -w0)" "$(printf 'x-access-token:%s' "$t" | base64 -w0)"; do
    grep -q -F -- "$needle" "$ALL" && bad "10 token form found in output" || ok "10 no token form in output"
  done
done
grep -q -F "$A74" "$ALL" && bad "10 token body found in output" || ok "10 no token body in output"
grep -q -F -e '_pat_' -e 11BHTEST -e AAAAAAAA -e BBBBBBBB "$ALL" && bad "10 token prefix or marker found in output" || ok "10 no token prefix or marker in output"
# The token goes only to the shell's builtin printf: every non-comment line that names it is one of these.
grep 'bh_github_token' "$SCRIPT" | grep -v -E '^[[:space:]]*#' | grep -v -x -F \
  -e 'bh_github_token="${BH_GITHUB_TOKEN-}"' \
  -e "  printf '%s\\000%s' \"\$bh_github_repo\" \"\$bh_github_token\" | \\" \
  -e 'bh_github_token= bh_github_repo=' | grep -q . && bad "10 token used outside the builtin printf" || ok "10 token only fed to builtin printf"
[ "$(grep -c 'bh_github_token' "$SCRIPT" | tr -d ' ')" -ge 3 ] || bad "10 static check matched nothing"
[ "$(sh -c 'command -V printf')" = "printf is a shell builtin" ] && ok "10 printf is a builtin in /bin/sh" || bad "10 printf not builtin"
grep -E -q '^[[:space:]]*set[[:space:]]+(-[a-wyz]*x|-o[[:space:]]+xtrace)|^#!.* -[a-z]*x' "$SCRIPT" && bad "10 xtrace in start script" || ok "10 no xtrace"

exit $fail
