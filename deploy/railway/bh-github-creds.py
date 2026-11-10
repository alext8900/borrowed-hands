"""Install, replace or remove the optional GitHub push credential at container start (called by bh-start.sh).

stdin: "<BH_GITHUB_REPO>\\0<BH_GITHUB_TOKEN>" (either may be empty). argv: HERMES_HOME, git config path.
The token arrives on stdin only, so it is never in argv or in this process's environment.

Valid token and repo (G = the git config's directory, /etc/bh-github in the image: root 0755, container
filesystem, so new in every container, never on the volume, never in `hermes backup`, under MEDIA's /etc denylist):
  G/gitconfig        0644 root   URL-scoped, read-only helper over the store; holds no secret
  G/creds/           0700, owned by the user Hermes runs as (the parent is root's, so that user cannot replace it)
    git-credentials  0600  credential-store lines for https://github.com/<owner>/<repo>(.git) only
    api-header       0600  "Authorization: Bearer <token>" for `curl -H @file`
Every start also sweeps the old location, $HERMES_HOME/.config/bh-github on the volume, so an upgrade leaves no
credential behind there.
Anything else (absent, malformed, symlinked, foreign-owned): the git config and both files are removed, nothing
is written, and the gateway starts without GitHub credentials. Messages never contain either value, and name
no repository either (a mis-pasted token can sit in the repo variable).
Install is strict (refuses links, foreign owners, odd entries). Removal is lenient: it unlinks whatever is at our
names (unlink never follows a link) and reports what was odd, so one odd entry never keeps an old token alive.
"""
import os
import pwd
import re
import secrets
import stat
import sys

TOKEN_RE = re.compile(r"github_pat_[A-Za-z0-9_]{30,244}")  # fine-grained PAT; 41-255 chars in all
# GitHub owner (1-39, alphanumeric or single hyphens, no edge hyphen) / repository name (1-100 of [A-Za-z0-9._-]).
REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}/[A-Za-z0-9._-]{1,100}")
# Prefixes of every GitHub token kind. A repo value holding one is a mis-paste, never a repository name we accept.
CREDENTIAL_PREFIXES = ("github_pat_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_")
PATH_RE = re.compile(r"/[A-Za-z0-9/._-]*")  # the store path is embedded in a git config value
CRED_DIR = "creds"  # under the git config's directory
LEGACY = (".config", "bh-github")  # the volume location used before; swept on every start
FILES = ("git-credentials", "api-header")
TMP_PREFIX = ".bh-tmp-"
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# Where git's own `store` helper would put a copy (HOME is $HERMES_HOME for the gateway, $HERMES_HOME/home for
# agent commands; plus the XDG location). Reported, never rewritten as root.
COPY_PATHS = ((".git-credentials",), ("home", ".git-credentials"), (".config", "git", "credentials"),
              ("home", ".config", "git", "credentials"))
REPO_REFUSAL = "BH_GITHUB_REPO must be a plain owner/name (no URL, no .git, no extra path, no token)"


class Refuse(Exception):
    pass


def say(message):
    print(f"[bh-start] github credentials: {message}", file=sys.stderr)


def owner():
    if os.geteuid() == 0:
        p = pwd.getpwnam("hermes")
        return p.pw_uid, p.pw_gid
    return os.geteuid(), os.getegid()


def open_dir(parent_fd, name, label, uids, create=False, mode=0o700, chown_to=None):
    """Open parent_fd/name as a directory without following links; None if absent and not created."""
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            return None
        os.mkdir(name, mode, dir_fd=parent_fd)
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if chown_to:
            os.chown(name, *chown_to, dir_fd=parent_fd, follow_symlinks=False)
            st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(st.st_mode):
        raise Refuse(f"{label} is a symlink or not a directory")
    fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino) or opened.st_uid not in uids:
        os.close(fd)
        raise Refuse(f"{label} is not owned as expected")
    return fd


def check_file(dfd, name, uid):
    """An existing entry we would replace must be our own plain file, not a link of either kind (install only)."""
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != uid:
        raise Refuse(f"{name} is a symlink, a hard link, not a regular file, or not owned as expected")
    return True


def publish(dfd, name, data, uid, gid, mode):
    """Write a sibling temp file and rename it over name (rename never follows a link at the destination)."""
    tmp = TMP_PREFIX + secrets.token_hex(8)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchown(f.fileno(), uid, gid)
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except FileNotFoundError:
            pass
        raise
    os.fsync(dfd)


def ours(entry):
    """Names this script creates in the credential directory: the two files and its (or older) temp names."""
    return entry in FILES or entry.startswith(TMP_PREFIX) or any(entry.startswith(f".{n}.") for n in FILES)


def unlink_entry(dfd, name, notes):
    """Remove one entry whatever it is; never follows it, never recurses. True if a name was removed."""
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    try:
        if stat.S_ISDIR(st.st_mode):
            os.rmdir(name, dir_fd=dfd)
        else:
            os.unlink(name, dir_fd=dfd)
    except OSError as exc:
        notes.append(f"{name} is a non-empty directory or could not be removed ({type(exc).__name__}); not removed")
        return False
    if stat.S_ISREG(st.st_mode) and st.st_nlink > 1 and name in FILES:
        notes.append(f"{name} had another hard link; a copy of the old token may still exist elsewhere on the same filesystem")
    elif stat.S_ISLNK(st.st_mode):
        notes.append(f"{name} was a symlink; removed the link only")
    return True


def remove_stale_tmp(dfd):
    for entry in os.listdir(dfd):
        if ours(entry) and entry not in FILES:
            unlink_entry(dfd, entry, [])


def remove_gitconfig(path):
    parent, name = os.path.split(path)
    try:
        pfd = os.open(parent, DIR_FLAGS)
    except FileNotFoundError:
        return False
    except OSError:
        # A symlinked parent (ELOOP/ENOTDIR) holds nothing git reads through our include; leave it alone.
        return False
    try:
        return unlink_entry(pfd, name, [])
    finally:
        os.close(pfd)


def sweep(parent_fd, name, notes):
    """Lenient removal of parent_fd/name: our entries inside it, then the directory if empty. A link or file at
    name is unlinked, never followed. True if a credential file was removed."""
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        unlink_entry(parent_fd, name, notes)
        return False
    removed = False
    dfd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    try:
        for entry in os.listdir(dfd):
            if ours(entry):
                try:
                    if unlink_entry(dfd, entry, notes) and entry in FILES:
                        removed = True
                except OSError as exc:
                    notes.append(f"{entry} could not be checked ({type(exc).__name__}); not removed")
        left = os.listdir(dfd)
    finally:
        os.close(dfd)
    if not left:
        os.rmdir(name, dir_fd=parent_fd)
    return removed


def remove_all(gitconfig, notes=None):
    """Remove the git config first (git stops using the store), then our files and the empty directory."""
    notes = [] if notes is None else notes
    removed = remove_gitconfig(gitconfig)
    try:
        gfd = os.open(os.path.dirname(gitconfig), DIR_FLAGS)
    except FileNotFoundError:
        return removed
    except OSError:
        notes.append("the git config directory is a symlink or not a directory; not followed")
        return removed
    try:
        return sweep(gfd, CRED_DIR, notes) or removed
    finally:
        os.close(gfd)


def sweep_legacy(home, notes):
    """Remove a credential left at $HERMES_HOME/.config/bh-github by an earlier image. Never follows links."""
    try:
        hfd = os.open(home, DIR_FLAGS)
    except OSError as exc:
        notes.append(f"HERMES_HOME could not be opened without following links ({type(exc).__name__})")
        return False
    try:
        try:
            cfd = os.open(LEGACY[0], DIR_FLAGS, dir_fd=hfd)
        except FileNotFoundError:
            return False
        except OSError:
            notes.append("$HERMES_HOME/.config is a symlink or not a directory; old location not checked")
            return False
        try:
            return sweep(cfd, LEGACY[1], notes)
        finally:
            os.close(cfd)
    finally:
        os.close(hfd)


def find_copies(home):
    """Paths under home where git's own store helper keeps a github.com credential. Read-only, no link following."""
    found = []
    for parts in COPY_PATHS:
        fds = []
        try:
            fds.append(os.open(home, DIR_FLAGS))
            for part in parts[:-1]:
                fds.append(os.open(part, DIR_FLAGS, dir_fd=fds[-1]))
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fds[-1])
            try:
                if stat.S_ISREG(os.fstat(fd).st_mode) and b"@github.com" in os.read(fd, 1 << 20):
                    found.append(os.path.join(home, *parts))
            finally:
                os.close(fd)
        except OSError:
            pass
        finally:
            for d in fds:
                os.close(d)
    return found


def gitconfig_text(store, repo):
    # Read-only helper: answers "get" from the store and ignores store/erase, so git never rewrites the file
    # (credential-store would recreate it under the caller's umask) or deletes it after a 401.
    helper = f"!f() {{ test $1 = get || exit 0; exec git credential-store --file={store} get; }}; f"
    lines = ["# Written by bh-start.sh at container start. No secret here; the token is in the store file.",
             "# Included only through GIT_CONFIG_* (read last), so the empty helper below clears any helper a",
             "# user-level config adds for this URL: git then never sends the token to a second helper's store.",
             '[credential "https://github.com"]', "\tuseHttpPath = true"]
    for url in (f"https://github.com/{repo}.git", f"https://github.com/{repo}"):
        lines += [f'[credential "{url}"]', "\thelper =", f'\thelper = "{helper}"']
    return ("\n".join(lines) + "\n").encode()


def install(gitconfig, repo, token):
    uid, gid = owner()
    gparent, gname = os.path.split(gitconfig)
    store = os.path.join(gparent, CRED_DIR, FILES[0])
    if not PATH_RE.fullmatch(store) or not PATH_RE.fullmatch(gitconfig):
        raise Refuse("the git config path has characters git config cannot carry safely")
    # The git config and the credential directory's parent are root's, off the volume: the gateway's uid can read
    # the credential files (it must, to push) but cannot edit the git config or replace the directory.
    top = os.open(os.path.dirname(gparent), DIR_FLAGS)
    try:
        gpfd = open_dir(top, os.path.basename(gparent), "git config directory", {os.geteuid()}, create=True, mode=0o755)
    finally:
        os.close(top)
    try:
        os.fchmod(gpfd, 0o755)
        dfd = open_dir(gpfd, CRED_DIR, "credential directory", {uid}, True, 0o700, (uid, gid))
        try:
            os.fchmod(dfd, 0o700)
            for name in FILES:
                check_file(dfd, name, uid)
            check_file(gpfd, gname, os.geteuid())
            remove_stale_tmp(dfd)
            t = token.encode()
            lines = b"".join(b"https://x-access-token:%s@github.com/%s%s\n" % (t, repo.encode(), suffix)
                             for suffix in (b".git", b""))
            publish(dfd, FILES[0], lines, uid, gid, 0o600)
            publish(dfd, FILES[1], b"Authorization: Bearer %s\n" % t, uid, gid, 0o600)
        finally:
            os.close(dfd)
        publish(gpfd, gname, gitconfig_text(store, repo), os.geteuid(), os.getegid(), 0o644)
    finally:
        os.close(gpfd)


def token_like(raw_repo):
    lowered = raw_repo.lower()
    return any(p.encode() in lowered for p in CREDENTIAL_PREFIXES)


def check_repo(raw_repo, raw_token):
    if not raw_repo:
        raise Refuse("BH_GITHUB_TOKEN is set but BH_GITHUB_REPO is missing")
    if raw_token in raw_repo or token_like(raw_repo):
        raise Refuse(REPO_REFUSAL)
    try:
        repo = raw_repo.decode("ascii")
    except UnicodeDecodeError:
        raise Refuse(REPO_REFUSAL) from None
    name = repo.partition("/")[2]
    if not REPO_RE.fullmatch(repo) or name in (".", "..") or name.lower().endswith(".git"):
        raise Refuse(REPO_REFUSAL)
    return repo


def main():
    os.umask(0o077)
    home, gitconfig = sys.argv[1], sys.argv[2]
    raw_repo, _, raw_token = sys.stdin.buffer.read().partition(b"\0")
    notes = []
    try:
        if sweep_legacy(home, notes):
            say("removed a credential from the old location $HERMES_HOME/.config/bh-github")
    except Exception as exc:
        notes.append(f"old location $HERMES_HOME/.config/bh-github not swept ({type(exc).__name__})")
    try:
        if not raw_token:
            removed = remove_all(gitconfig, notes)
            if token_like(raw_repo):  # a token pasted into the variable that is not sealed
                raise Refuse(REPO_REFUSAL)
            say("BH_GITHUB_TOKEN not set; " + ("removed the stored credential" if removed else "no credential installed"))
            return
        repo = check_repo(raw_repo, raw_token)
        try:
            token = raw_token.decode("ascii")
        except UnicodeDecodeError:
            raise Refuse("BH_GITHUB_TOKEN has non-ASCII characters") from None
        if not TOKEN_RE.fullmatch(token):
            raise Refuse("BH_GITHUB_TOKEN is not a fine-grained personal access token (GitHub's fine-grained prefix, "
                         "41-255 characters of A-Z a-z 0-9 _, no spaces or newlines)")
        install(gitconfig, repo, token)
        say("configured for the repository named in BH_GITHUB_REPO")
    except Exception as exc:
        reason = str(exc) if isinstance(exc, Refuse) else type(exc).__name__
        try:
            remove_all(gitconfig, notes)
        except Exception as cleanup_exc:
            reason += f"; cleanup also refused ({cleanup_exc if isinstance(cleanup_exc, Refuse) else type(cleanup_exc).__name__})"
        say(f"refused: {reason}; starting without GitHub credentials")
    finally:
        for note in notes:
            say(f"warning: {note}")
        for path in find_copies(home):
            say(f"warning: {path} holds a github.com credential this script did not write and does not remove; "
                "delete it and revoke the token at GitHub")


if __name__ == "__main__":
    main()
