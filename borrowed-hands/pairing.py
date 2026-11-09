"""Pairing codes, rate limits and the owner notice (PROTOCOL 3.2, 3.3).

The plaintext code exists only in the pairing string posted to the owner's DM; the store keeps sha256(code).
"""
from __future__ import annotations

import copy
import logging
import os
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional, Tuple

from . import bhproto, readiness
from .config import Settings
from .store import Store

log = logging.getLogger("borrowed_hands")

CODE_TTL_MS = 600_000
PER_IP_LIMIT, PER_IP_WINDOW_MS = 10, 60_000
GLOBAL_FAIL_LIMIT, GLOBAL_WINDOW_MS = 30, 600_000


class PairingUnavailable(Exception):
    pass


def new_code(store: Store, settings: Settings, server_pub: bytes, issued_to: str, now: int) -> Tuple[str, str]:
    """Create a single-use code (supersedes earlier ones). Returns (pairing string, code hash)."""
    if not settings.public_url:
        raise PairingUnavailable("public_url is not set")
    code = os.urandom(16)
    h = bhproto.code_hash(code)
    store.create_code(h, now=now, ttl_ms=CODE_TTL_MS, issued_to=issued_to)
    store.audit("pair_code", now=now, detail={"issued_to": issued_to})
    return bhproto.pairing_string(settings.public_url, code, server_pub), h


class RateLimiter:
    """At most 10 pair attempts per minute per source IP and 30 failures per 10 minutes overall."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._per_ip: Dict[str, Deque[int]] = {}
        self._fails: Deque[int] = deque()

    @staticmethod
    def _trim(q: Deque[int], now: int, window: int) -> None:
        while q and q[0] <= now - window:
            q.popleft()

    def global_tripped(self, now: int) -> bool:
        with self._lock:
            self._trim(self._fails, now, GLOBAL_WINDOW_MS)
            return len(self._fails) >= GLOBAL_FAIL_LIMIT

    def allow(self, ip: str, now: int) -> Optional[int]:
        """Record an attempt. None if allowed, else seconds to wait."""
        with self._lock:
            self._trim(self._fails, now, GLOBAL_WINDOW_MS)
            if len(self._fails) >= GLOBAL_FAIL_LIMIT:
                return max(1, (self._fails[0] + GLOBAL_WINDOW_MS - now) // 1000)
            q = self._per_ip.setdefault(ip, deque())
            self._trim(q, now, PER_IP_WINDOW_MS)
            if len(q) >= PER_IP_LIMIT:
                return max(1, (q[0] + PER_IP_WINDOW_MS - now) // 1000)
            q.append(now)
            if len(self._per_ip) > 10_000:  # bound memory under a spray of source addresses
                for k in [k for k, v in self._per_ip.items() if not v][:5_000]:
                    del self._per_ip[k]
            return None

    def failed(self, now: int) -> None:
        with self._lock:
            self._fails.append(now)


def now_ms():
    return int(time.time() * 1000)


@dataclass
class _Route:
    handle: Any
    post: Any
    pair_text: str
    expires: int
    code_hash: str = ""
    posting: bool = False
    sid: Optional[str] = None
    caps: dict = field(default_factory=dict)
    seen: set = field(default_factory=set)
    dirty: bool = True


class Notifier:
    """One bounded worker serializes secret removal and NEW readiness replies to the original owner DM.

    Routes are memory-only. They expire after 24 hours; restart requires a fresh /hands pair for push delivery.
    Worker sends are best effort snapshots at dispatch. A public API send already in flight cannot be recalled.
    `allowed` is the capabilities a reply may call usable (register passes tools.USABLE_CAPABILITIES); the
    default announces nothing.
    """
    MAX_ROUTES = 64
    MAX_PENDING = 8
    MAX_ANNOUNCEMENTS = 4
    ROUTE_TTL_MS = 24 * 60 * 60 * 1000
    MAX_TEXT = readiness.MAX_TEXT

    def __init__(self, store: Store, allowed=()) -> None:
        self.store = store
        self.allowed = allowed
        self._handles = {}
        self._routes = OrderedDict()
        self._cv = threading.Condition()
        self._closed = False
        self._busy = False
        self._worker = None

    def _prune(self):
        now = now_ms()
        self._handles = {k: v for k, v in self._handles.items() if v[2] > now}
        for key, row in list(self._routes.items()):
            if row.expires <= now:
                del self._routes[key]

    def remember(self, code_hash: str, handle: Any, post=None, posting=False) -> None:
        with self._cv:
            if not self._closed:
                # Keep earlier records: a code consumed just before this one was issued still owns its route.
                # The store supersedes unconsumed codes, so a superseded record is never associated.
                self._handles[code_hash] = (handle, post, now_ms() + CODE_TTL_MS, posting)
                while len(self._handles) > self.MAX_PENDING:
                    del self._handles[next(iter(self._handles))]

    def attach_handle(self, code_hash, handle):
        """The initial public post may still be returning when its code is used. Finish that same record."""
        with self._cv:
            self._prune()
            pending = self._handles.get(code_hash)
            if pending:
                self._handles[code_hash] = (handle, pending[1], pending[2], False)
            for row in self._routes.values():
                if row.code_hash == code_hash:
                    row.handle, row.posting = handle, False
                    row.dirty = True
                    self._wake()

    def associate(self, code_hash, helper_id, text, now):
        """Called after consume_code and the pair_ok send succeed, before caps can be read. No message I/O here."""
        with self._cv:
            self._prune()
            if self._closed:
                return
            handle, post, _, posting = self._handles.pop(code_hash, (None, None, 0, False))
            # Missing route (CLI, restart, failed initial post) still needs the original queued pair notice.
            self._routes[helper_id] = _Route(handle, post, text, now + self.ROUTE_TTL_MS, code_hash, posting)
            while len(self._routes) > self.MAX_ROUTES:
                self._routes.popitem(last=False)
            self._wake()

    def connected(self, helper_id, sid):
        with self._cv:
            self._prune()
            row = self._routes.get(helper_id)
            if row:
                row.sid, row.caps = sid, {}  # cached caps from a previous connection are not fresh evidence

    def disconnected(self, helper_id, sid):
        with self._cv:
            row = self._routes.get(helper_id)
            if row and row.sid == sid:
                row.sid, row.caps = None, {}

    def forget(self, helper_id):
        with self._cv:
            self._routes.pop(helper_id, None)

    def caps(self, helper_id, sid, body):
        with self._cv:
            self._prune()
            row = self._routes.get(helper_id)
            if row and row.sid == sid and not self._closed:
                row.caps = copy.deepcopy({k: body[k] for k in ("name", "capabilities") if k in body})
                row.dirty = True
                self._wake()

    def _wake(self):
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="bh-owner-notices", daemon=True)
            self._worker.start()
        self._cv.notify_all()

    def close(self):
        # Never wait on a worker waiting for the gateway loop from the unload hook on that same loop.
        with self._cv:
            self._closed = True
            self._handles.clear()
            self._routes.clear()
            self._cv.notify_all()

    def wait_idle(self, timeout):
        """Local verification barrier; never called from the listener or gateway loop."""
        with self._cv:
            return self._cv.wait_for(lambda: not self._busy and not any(r.dirty for r in self._routes.values()), timeout)

    def _valid(self, helper_id, row):
        if self._closed or self._routes.get(helper_id) is not row or row.expires <= now_ms():
            return False
        helper = self.store.get_helper(helper_id)
        return helper is not None and helper.revoked_at is None

    def _run(self):
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._closed or any(r.dirty and not r.posting for r in self._routes.values()), 60)
                self._prune()
                if self._closed:
                    return
                item = next(((k, r) for k, r in self._routes.items() if r.dirty and not r.posting), None)
                if item is None:
                    continue
                helper_id, row = item
                row.dirty, self._busy = False, True
            try:
                self._deliver(helper_id, row)
            except Exception as exc:
                log.warning("borrowed_hands: owner notice failed: %s", type(exc).__name__)
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def _deliver(self, helper_id, row):
        with self._cv:
            if not self._valid(helper_id, row):
                return
            text, handle = row.pair_text, row.handle
            row.pair_text, row.handle = "", None
        if text:
            self._edit_or_notice(handle, text, now_ms(), helper_id, row)
        # The edit may block. Re-read everything afterward, including revocation, route lifetime and expiry.
        with self._cv:
            if not self._valid(helper_id, row) or not row.sid:
                return
            helper = self.store.get_helper(helper_id)
            text = readiness.summary(row.caps.get("name", helper.name), row.caps, now_ms(), self.allowed)
            if not text or text in row.seen or len(row.seen) >= self.MAX_ANNOUNCEMENTS:
                return
            row.seen.add(text)  # claim BEFORE blocking post; failures must not turn pings into retries
            post = row.post
        try:
            sent = post(text) if post else None
            ok = sent is not None and bool(getattr(sent, "ok", False))
        except Exception:
            ok = False
        if not ok:
            with self._cv:
                if self._valid(helper_id, row):
                    # Do not persist an "is ready" claim that could be stale when eventually read.
                    self.store.add_notice("Mac capabilities changed; run /hands status for current availability.",
                                          now=now_ms())

    def _edit_or_notice(self, handle, text, now, helper_id, row):
        ok = False
        if handle is not None:
            try:
                res = handle.edit(text)
                ok = bool(res.get("ok")) if isinstance(res, dict) else False
            except Exception as exc:
                log.warning("borrowed_hands: pair notice edit failed: %s", type(exc).__name__)
        if not ok:
            with self._cv:
                if self._valid(helper_id, row):
                    self.store.add_notice(text, now=now)
        return ok
