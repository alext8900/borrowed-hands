"""Find the commit to build when the caller names none (friendly build requests).

Reads the branch list of the granted public repository over HTTPS, the same anonymous ref advertisement that
`git ls-remote` reads (`GET <repo>/info/refs?service=git-upload-pack`), and picks the tip of a `bh/*` branch.
The Mac still checks the commit against its own remote and `bh/*` ancestry; this only saves the user a SHA.

Egress policy for this one server-side request:
- The host must be a DNS name (no numeric or IP-literal hosts). A Unicode name is accepted only when IDNA 2003
  (stdlib `encodings.idna`) converts it to A-labels and back without change; mixed or invalid names refuse.
- The name is resolved once, and every A/AAAA answer must be a public unicast address (not loopback, private,
  link-local, CGNAT, multicast, reserved, unspecified, documentation, or a v4-mapped/6to4/NAT64 form of one).
- The connection goes to that checked address, with the original name for TLS SNI and certificate verification.
  No proxy, no credentials, no cookies, and no redirects (a redirect refuses).
- One deadline (TIMEOUT_S) covers DNS, connect, TLS, the request and the body; the caller returns at it and the
  socket is shut down.
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import bhproto

BRANCH_PATTERN = "^bh/[A-Za-z0-9._/-]+$"
_BRANCH = re.compile(r"bh/[A-Za-z0-9._/-]+")
_SHA = re.compile(rb"[0-9a-f]{40}")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
MAX_BRANCH = 200
TIMEOUT_S = 8.0
MAX_BYTES = 1 << 20
MAX_LISTED = 10
CONTENT_TYPE = "application/x-git-upload-pack-advertisement"
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class RefsError(Exception):
    """A user-readable reason the commit could not be resolved. Nothing was built."""

    def __init__(self, code: str, reason: str, message: str, **detail):
        super().__init__(message)
        self.code, self.reason, self.message, self.detail = code, reason, message, detail


def valid_branch(name) -> bool:
    """bh/<name> in the tool's pattern, plus git's ref-name rules for the characters that pattern allows."""
    if not isinstance(name, str) or len(name) > MAX_BRANCH or not _BRANCH.fullmatch(name):
        return False
    if ".." in name or name.endswith("."):
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock") for part in name.split("/"))


# ---------------------------------------------------------------- host and address policy
def _bad_host() -> RefsError:
    return RefsError("invalid_arguments", "bad_host",
                     "The repository host must be a public DNS name (not an IP address or a local name). "
                     "Nothing was built.")


def ascii_host(host: str) -> str:
    """The A-label form of `host`, or RefsError. Rejects numeric hosts, single labels and lossy IDNA."""
    if not isinstance(host, str) or not host or len(host) > 253:
        raise _bad_host()
    host = host.lower()
    if host.isascii():
        out = host
    else:
        if any(label.startswith("xn--") for label in host.split(".")):
            raise _bad_host()                                  # mixed A-labels and Unicode
        try:
            out = host.encode("idna").decode("ascii")
            if out.encode("ascii").decode("idna") != host:     # lossy mapping (e.g. U+200B, sharp s)
                raise _bad_host()
        except (UnicodeError, ValueError):
            raise _bad_host()
    labels = out.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels) or labels[-1].isdigit():
        raise _bad_host()
    if any(c.isdigit() for c in labels[-1]) and not labels[-1].startswith("xn--"):
        raise _bad_host()                                      # 0x7f000001-style or numeric TLDs
    for label in labels:
        if label.startswith("xn--"):
            try:
                label.encode("ascii").decode("idna")
            except (UnicodeError, ValueError):
                raise _bad_host()
    return out


def public_address(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    embedded = []
    if ip.version == 6:
        for v4 in (ip.ipv4_mapped, ip.sixtofour, *(ip.teredo or ())):
            if v4 is not None:
                embedded.append(v4)
        if ip in _NAT64:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    for a in (ip, *embedded):
        if (not a.is_global or a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved
                or a.is_unspecified or getattr(a, "is_site_local", False)):   # fec0::/10, which Python calls global
            return False
    return True


def _getaddrinfo(host: str) -> List[str]:
    """Every address the system resolver returns for host:443. Patched in tests."""
    return [ai[4][0] for ai in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


def _unreachable(why: str) -> RefsError:
    return RefsError("timeout", "refs_unreachable",
                     f"Could not read the repository's branch list within {TIMEOUT_S:g} s ({why}). Nothing was "
                     "built. Try again, or pass an exact commit.")

def _bounded(fn, deadline: float, name: str = "bh-refs", on_timeout=None, on_start_failure=None):
    """Run fn() on a daemon thread; return its result, re-raise its error, or at the deadline call on_timeout()
    (which must not block) and raise refs_unreachable. The caller never waits past the deadline."""
    box: dict = {}

    def work():
        try:
            box["ok"] = fn()
        except BaseException as exc:  # handed back to the caller
            box["exc"] = exc
    t = threading.Thread(target=work, name=name, daemon=True)
    try:
        t.start()
    except BaseException:
        if on_start_failure is not None:
            on_start_failure()
        raise
    t.join(max(0.0, deadline - time.monotonic()))
    if t.is_alive():
        if on_timeout is not None:
            on_timeout()
        raise _unreachable("timed out")
    if "exc" in box:
        raise box["exc"]
    return box["ok"]


# A lookup stuck in the OS resolver cannot be cancelled. The caller still returns at its deadline, but the lookup
# keeps its thread until the resolver returns, so at most this many may be outstanding; more are refused.
MAX_PENDING_LOOKUPS = 4
_LOOKUP_SLOTS = threading.BoundedSemaphore(MAX_PENDING_LOOKUPS)


def resolve_public(host: str, deadline: float) -> str:
    """One lookup; every answer must be public. Returns the address to connect to (IPv4 first)."""
    if not _LOOKUP_SLOTS.acquire(blocking=False):
        raise RefsError("timeout", "resolver_busy",
                        "Too many earlier repository lookups are still waiting on this server's DNS resolver. Try "
                        "again in a minute, or pass an exact commit. Nothing was built.")

    def lookup():
        try:
            return _getaddrinfo(host)
        finally:
            _LOOKUP_SLOTS.release()
    try:
        answers = _bounded(lookup, deadline, name="bh-refs-dns", on_start_failure=_LOOKUP_SLOTS.release)
    except RefsError:
        raise
    except Exception as exc:
        raise _unreachable(f"DNS lookup failed: {type(exc).__name__}")
    if not answers:
        raise _unreachable("DNS returned no address")
    if not all(public_address(a) for a in answers):
        raise RefsError("needs_resolution", "non_public_address",
                        "The repository host resolves to a non-public address, so its branch list was not read. "
                        "Nothing was built.")
    v4 = [a for a in answers if ":" not in a]
    return (v4 or answers)[0].split("%", 1)[0]


# ---------------------------------------------------------------- the pinned HTTPS exchange
class _Cancelled(Exception):
    pass


def _close(obj) -> None:
    try:
        obj.close()
    except Exception:
        pass


class _Exchange:
    """Owns every socket and response of one request, from before it can block.

    The worker thread does all I/O and closes everything it owns when it finishes. The waiting caller, at the
    deadline, only calls cancel(): it marks the exchange cancelled and shuts the owned transports down at the
    socket level, which never blocks (it takes no buffered-reader lock) and wakes any blocked connect, handshake
    or read. Anything the worker tries to own after that is closed at once, and every step re-checks the budget,
    so no request is sent after a cancel or the deadline."""

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.cancelled = False
        self._lock = threading.Lock()
        self._owned: list = []

    def remaining(self) -> float:
        if self.cancelled:
            raise _Cancelled()
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("deadline")
        return left

    def own(self, obj):
        with self._lock:
            if not self.cancelled:
                self._owned.append(obj)
                return obj
        _close(obj)
        raise _Cancelled()

    def budget_sockets(self) -> float:
        """Give every owned socket only what is left of the budget for its next blocking step."""
        left = self.remaining()
        with self._lock:
            socks = [o for o in self._owned if isinstance(o, socket.socket)]
        for s in socks:
            try:
                s.settimeout(left)
            except OSError:
                pass
        return left

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            socks = [o for o in self._owned if isinstance(o, socket.socket)]
        for s in socks:
            try:
                socket.socket.shutdown(s, socket.SHUT_RDWR)   # the fd itself, not SSLSocket's override
            except OSError:
                pass

    def close_all(self) -> None:
        with self._lock:
            owned, self._owned = self._owned, []
        for obj in reversed(owned):
            _close(obj)


def _tls_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _tcp_connect(ip: str, timeout: float, ex: _Exchange) -> socket.socket:
    """A TCP connection to the checked numeric address (no second name lookup), owned before it connects."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sock = ex.own(socket.socket(family, socket.SOCK_STREAM))
    sock.settimeout(timeout)
    sock.connect((ip, 443))
    return sock


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to a fixed, already-checked address; `host` is still used for SNI, certificate checks and Host."""

    def __init__(self, host: str, ip: str, ex: _Exchange):
        super().__init__(host, 443, timeout=ex.remaining(), context=_tls_context())
        self._ip, self._ex = ip, ex

    def connect(self):
        ex = self._ex
        raw = _tcp_connect(self._ip, ex.remaining(), ex)
        left = ex.remaining()
        tls = ex.own(self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False))
        tls.settimeout(left)
        tls.do_handshake()
        ex.remaining()                     # never send the request after a cancel or the deadline
        self.sock = tls


def _open(url: str, host: str, ip: str, timeout: float, ex: _Exchange):
    """GET `url` from `ip` as `host`, inside `ex`. Returns the response (status, headers, read1, close). The
    timeout argument is what is left of the budget (informational: `ex` enforces it). Patched in tests."""
    p = urlsplit(url)
    conn = _PinnedHTTPS(host, ip, ex)
    conn.request("GET", f"{p.path}?{p.query}", headers={"User-Agent": "git/2 (borrowed-hands)", "Accept": "*/*"})
    ex.budget_sockets()
    return conn.getresponse()


def _unreadable(why: str) -> RefsError:
    return RefsError("needs_resolution", "repo_unreadable",
                     f"Could not read the branch list of the repository ({why}). It must be a public https git "
                     "repository. Nothing was built.")


def _download(url: str, host: str, ip: str, ex: _Exchange) -> bytes:
    try:
        resp = ex.own(_open(url, host, ip, ex.remaining(), ex))
        status = getattr(resp, "status", 0)
        if 300 <= status < 400:
            raise _unreadable(f"the host answered with a redirect (HTTP {status}); redirects are not followed, "
                              "so use the repository's canonical https URL")
        if status in (401, 403, 404):
            raise _unreadable(f"HTTP {status}: not found or not public")
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if status != 200 or ctype != CONTENT_TYPE:
            raise _unreadable("the host did not answer as a git server")
        body = b""
        while len(body) <= MAX_BYTES:
            ex.budget_sockets()
            chunk = resp.read1(65536)
            if not chunk:
                return body
            body += chunk
        raise _unreadable("the branch list is larger than 1 MiB")
    finally:
        ex.close_all()                     # every terminal path, on the thread that did the I/O


def _http_get(url: str, host: str, deadline: float) -> bytes:
    ip = resolve_public(host, deadline)
    ex = _Exchange(deadline)
    try:
        return _bounded(lambda: _download(url, host, ip, ex), deadline, on_timeout=ex.cancel)
    except RefsError:
        raise
    except Exception as exc:  # connection refused/reset, TLS and certificate errors, read timeouts, cancel
        raise _unreachable(type(exc).__name__)


# ---------------------------------------------------------------- the advertisement
def parse_advertisement(body: bytes) -> Dict[str, str]:
    """{ref name: sha} from a smart-HTTP v0 ref advertisement. Malformed framing refuses; odd lines are skipped."""
    out: Dict[str, str] = {}
    pos, service_seen = 0, False
    while pos < len(body):
        head = body[pos:pos + 4]
        if len(head) < 4 or not all(c in b"0123456789abcdef" for c in head):
            raise _unreadable("malformed reply")
        n = int(head, 16)
        if n == 0:
            pos += 4
            continue
        if n < 4 or pos + n > len(body):
            raise _unreadable("malformed reply")
        line, pos = body[pos + 4:pos + n].rstrip(b"\n"), pos + n
        if line == b"# service=git-upload-pack":
            service_seen = True
            continue
        if line.startswith(b"ERR "):
            raise _unreadable("the host refused")
        parts = line.split(b"\0", 1)[0].split(b" ")
        if len(parts) != 2 or not _SHA.fullmatch(parts[0]) or parts[1].endswith(b"^{}"):
            continue
        try:
            out.setdefault(parts[1].decode("ascii"), parts[0].decode("ascii"))
        except UnicodeDecodeError:
            continue
    if not service_seen:
        raise _unreadable("the host did not answer as a git server")
    return out


def fetch_refs(repo_url: str) -> Dict[str, str]:
    try:
        bhproto.normalize_repo_url(repo_url)
        p = urlsplit(repo_url)
    except Exception:
        raise RefsError("invalid_arguments", "bad_repo_url", "The repository URL must be a plain public https URL.")
    deadline = time.monotonic() + TIMEOUT_S
    host = ascii_host(p.hostname or "")
    url = f"https://{host}{p.path.rstrip('/')}/info/refs?service=git-upload-pack"
    return parse_advertisement(_http_get(url, host, deadline))


def bh_branches(refs: Dict[str, str]) -> Dict[str, str]:
    prefix = "refs/heads/"
    return {name[len(prefix):]: sha for name, sha in refs.items()
            if name.startswith(prefix) and valid_branch(name[len(prefix):])}


def _listing(names: List[str]) -> str:
    shown = ", ".join(names[:MAX_LISTED])
    return shown + (f", and {len(names) - MAX_LISTED} more" if len(names) > MAX_LISTED else "")


def resolve(repo_url: str, branch: Optional[str]) -> Tuple[str, str, str]:
    """(sha, branch, how) for the tip of `branch`, or of the only bh/* branch. Raises RefsError."""
    branches = bh_branches(fetch_refs(repo_url))
    names = sorted(branches)
    if branch is not None:
        if branch not in branches:
            raise RefsError("needs_resolution", "branch_not_found",
                            f"There is no branch {branch} on the repository. "
                            + (f"Its bh/* branches: {_listing(names)}." if names else
                               "It has no bh/* branch; push the code to bh/<name> first.")
                            + " Nothing was built.", options=names[:MAX_LISTED], total=len(names))
        return branches[branch], branch, f"latest on {branch}"
    if not names:
        raise RefsError("needs_resolution", "no_bh_branch",
                        "The repository has no bh/* branch, and the Mac only builds commits on one. Push the code "
                        "to a branch named bh/<name> (for example bh/main) and ask again. Nothing was built.")
    if len(names) > 1:
        raise RefsError("invalid_arguments", "needs_input",
                        f"The repository has {len(names)} bh/* branches: {_listing(names)}. Ask the user which "
                        "one to build, then call again with branch set to it.", field="branch",
                        options=names[:MAX_LISTED], total=len(names))
    return branches[names[0]], names[0], f"latest on {names[0]}"
