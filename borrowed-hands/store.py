"""SQLite storage for the plugin (SPEC 7). One connection per thread, WAL, every write committed immediately.

Server-side job state machine (PROTOCOL 4.3 seen from the server):

    pending_send --sent--> submitted --helper job.state--> received|queued|materializing|building
         |                    |                                   |
         +--------------------+-----------------------------------+--> succeeded | failed | cancelled | expired

Terminal states absorb: the first terminal result wins, later ones are ignored (PROTOCOL 4.5). `stale` and
`detached_since` are overlays, not states.
"""
from __future__ import annotations

import enum
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from . import bhproto, refs

SCHEMA_VERSION = 1


class JobState(str, enum.Enum):
    PENDING_SEND = "pending_send"
    SUBMITTED = "submitted"
    RECEIVED = "received"
    QUEUED = "queued"
    MATERIALIZING = "materializing"
    BUILDING = "building"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL


TERMINAL = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.EXPIRED})
NONTERMINAL_SQL = "('pending_send','submitted','received','queued','materializing','building')"

_DDL = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS helpers(
  helper_id TEXT PRIMARY KEY, device_pubkey BLOB NOT NULL, name TEXT NOT NULL, paired_at INTEGER NOT NULL,
  paired_by TEXT NOT NULL, revoked_at INTEGER, last_seen INTEGER, caps_json TEXT);
CREATE TABLE IF NOT EXISTS pair_codes(
  code_hash TEXT PRIMARY KEY, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, used_at TEXT,
  issued_to TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
  job_id TEXT PRIMARY KEY, helper_id TEXT NOT NULL, action TEXT NOT NULL, contract BLOB NOT NULL,
  sig TEXT NOT NULL, contract_hash TEXT NOT NULL, nonce TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
  stale INTEGER NOT NULL DEFAULT 0, detached_since INTEGER, pending_send INTEGER NOT NULL DEFAULT 1,
  session_id TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  result_json TEXT, error_json TEXT);
CREATE INDEX IF NOT EXISTS jobs_helper ON jobs(helper_id, state);
CREATE TABLE IF NOT EXISTS notices(id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, text TEXT NOT NULL,
  delivered_at INTEGER);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at INTEGER NOT NULL, event TEXT NOT NULL,
  helper_id TEXT, job_id TEXT, detail_json TEXT);
"""


@dataclass(frozen=True)
class Helper:
    helper_id: str
    device_pubkey: bytes
    name: str
    paired_at: int
    paired_by: str
    revoked_at: Optional[int]
    last_seen: Optional[int]
    caps: Optional[dict]


@dataclass(frozen=True)
class Job:
    job_id: str
    helper_id: str
    action: str
    contract: bytes
    sig: str
    contract_hash: str
    nonce: str
    state: JobState
    stale: bool
    detached_since: Optional[int]
    pending_send: bool
    session_id: Optional[str]
    created_at: int
    updated_at: int
    expires_at: int
    result: Optional[dict]
    error: Optional[dict]


def _helper(row) -> Optional[Helper]:
    if row is None:
        return None
    return Helper(row["helper_id"], bytes(row["device_pubkey"]), row["name"], row["paired_at"], row["paired_by"],
                  row["revoked_at"], row["last_seen"], json.loads(row["caps_json"]) if row["caps_json"] else None)


def _job(row) -> Optional[Job]:
    if row is None:
        return None
    return Job(row["job_id"], row["helper_id"], row["action"], bytes(row["contract"]), row["sig"],
               row["contract_hash"], row["nonce"], JobState(row["state"]), bool(row["stale"]),
               row["detached_since"], bool(row["pending_send"]), row["session_id"], row["created_at"],
               row["updated_at"], row["expires_at"],
               json.loads(row["result_json"]) if row["result_json"] else None,
               json.loads(row["error_json"]) if row["error_json"] else None)


COMMIT_EXPLICIT = "explicit commit"
COMMIT_EXPLICIT_BRANCH = "explicit commit; branch not checked"


def source_fields(branch: Any, commit_source: Any) -> Optional[dict]:
    """The stored provenance fields if well formed, else None: a bh/* branch name, and a commit source that is
    one of the fixed explicit-commit strings or "latest on <bh/* branch>"."""
    if branch is not None and not refs.valid_branch(branch):          # bh/<name>, 200 characters, git ref rules
        return None
    if not isinstance(commit_source, str):
        return None
    if commit_source not in (COMMIT_EXPLICIT, COMMIT_EXPLICIT_BRANCH) and not (
            commit_source.startswith("latest on ") and refs.valid_branch(commit_source[len("latest on "):])):
        return None
    out = {"commit_source": commit_source}
    if branch is not None:
        out["branch"] = branch
    return out


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.exists():
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        os.chmod(self.path, 0o600)
        self._local = threading.local()
        c = self._conn()
        c.executescript(_DDL)
        c.execute("INSERT OR IGNORE INTO meta(k, v) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        for suffix in ("-wal", "-shm"):
            side = Path(str(self.path) + suffix)
            if side.exists():
                os.chmod(side, 0o600)

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            old = os.umask(0o077)
            try:
                c = sqlite3.connect(str(self.path), timeout=5, isolation_level=None, check_same_thread=True)
            finally:
                os.umask(old)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            c.execute("PRAGMA busy_timeout=5000")
            self._local.c = c
        return c

    def schema_version(self) -> int:
        return int(self._conn().execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()[0])

    # ------------------------------------------------------------ helpers
    def add_helper(self, helper_id: str, device_pubkey: bytes, name: str, *, now: int, paired_by: str) -> None:
        self._conn().execute(
            "INSERT INTO helpers(helper_id, device_pubkey, name, paired_at, paired_by) VALUES(?,?,?,?,?)",
            (helper_id, bytes(device_pubkey), name, now, paired_by))

    def get_helper(self, helper_id: str) -> Optional[Helper]:
        return _helper(self._conn().execute("SELECT * FROM helpers WHERE helper_id=?", (helper_id,)).fetchone())

    def find_helper(self, id_or_name: str) -> Optional[Helper]:
        """Exact helper_id, else exact name (newest pairing first). Never a prefix match."""
        h = self.get_helper(id_or_name)
        if h is not None:
            return h
        return _helper(self._conn().execute(
            "SELECT * FROM helpers WHERE name=? ORDER BY revoked_at IS NOT NULL, paired_at DESC LIMIT 1",
            (id_or_name,)).fetchone())

    def list_helpers(self) -> List[Helper]:
        return [_helper(r) for r in self._conn().execute("SELECT * FROM helpers ORDER BY paired_at")]

    def revoke_helper(self, helper_id: str, *, now: int) -> bool:
        cur = self._conn().execute("UPDATE helpers SET revoked_at=? WHERE helper_id=? AND revoked_at IS NULL",
                                   (now, helper_id))
        return cur.rowcount == 1

    def set_caps(self, helper_id: str, caps: dict, *, now: int) -> bool:
        """False if the helper is unknown or revoked: revoked helpers never get caps stored."""
        cur = self._conn().execute(
            "UPDATE helpers SET caps_json=?, last_seen=? WHERE helper_id=? AND revoked_at IS NULL",
            (json.dumps(caps, sort_keys=True), now, helper_id))
        return cur.rowcount == 1

    def touch(self, helper_id: str, *, now: int) -> None:
        self._conn().execute("UPDATE helpers SET last_seen=? WHERE helper_id=?", (now, helper_id))

    def set_device_pubkey(self, helper_id: str, pub: bytes) -> None:
        self._conn().execute("UPDATE helpers SET device_pubkey=? WHERE helper_id=? AND revoked_at IS NULL",
                             (bytes(pub), helper_id))

    # ------------------------------------------------------------ pairing codes (PROTOCOL 3.2)
    def create_code(self, code_hash: str, *, now: int, ttl_ms: int, issued_to: str) -> None:
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("UPDATE pair_codes SET used_at='superseded' WHERE used_at IS NULL")
            c.execute("INSERT INTO pair_codes(code_hash, created_at, expires_at, issued_to) VALUES(?,?,?,?)",
                      (code_hash, now, now + ttl_ms, issued_to))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def consume_code(self, code_hash: str, *, now: int) -> bool:
        cur = self._conn().execute(
            "UPDATE pair_codes SET used_at=? WHERE code_hash=? AND used_at IS NULL AND expires_at>?",
            (str(now), code_hash, now))
        return cur.rowcount == 1

    def code_issued_to(self, code_hash: str) -> Optional[str]:
        row = self._conn().execute("SELECT issued_to FROM pair_codes WHERE code_hash=?", (code_hash,)).fetchone()
        return row[0] if row else None

    def invalidate_codes(self, *, now: int) -> None:
        self._conn().execute("UPDATE pair_codes SET used_at='invalidated' WHERE used_at IS NULL")

    # ------------------------------------------------------------ jobs
    def insert_job(self, *, job_id: str, helper_id: str, action: str, contract: bytes, sig: str,
                   contract_hash: str, nonce: str, session_id: str, now: int, expires_at: int,
                   source: Any = None) -> None:
        """Insert a job. `source` ({"branch", "commit_source"} for a build) is stored in the same transaction as
        the contract, so it is durable before anything is dispatched or awaited; an invalid one is not stored."""
        fields = source_fields(source.get("branch"), source.get("commit_source")) \
            if isinstance(source, dict) else None
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute(
                "INSERT INTO jobs(job_id, helper_id, action, contract, sig, contract_hash, nonce, state, session_id,"
                " created_at, updated_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, helper_id, action, bytes(contract), sig, contract_hash, nonce, JobState.PENDING_SEND.value,
                 session_id, now, now, expires_at))
            if fields is not None:
                c.execute("INSERT OR IGNORE INTO meta(k, v) VALUES(?, ?)",
                          (f"job_source:{job_id}", json.dumps(fields, sort_keys=True)))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def get_job(self, job_id: str) -> Optional[Job]:
        return _job(self._conn().execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def jobs_nonterminal(self, helper_id: Optional[str] = None) -> List[Job]:
        q = f"SELECT * FROM jobs WHERE state IN {NONTERMINAL_SQL}"
        args: tuple = ()
        if helper_id is not None:
            q += " AND helper_id=?"
            args = (helper_id,)
        return [_job(r) for r in self._conn().execute(q + " ORDER BY created_at", args)]

    def newer_terminal_builds(self, job_id: str) -> List[Job]:
        """Every finished xcode.build job stored after `job_id` (insertion order, which is creation order: jobs are
        only ever inserted, never deleted or re-inserted), any Mac. Empty if `job_id` is unknown. A stored state
        this code does not know is selected too, so it raises here instead of being skipped."""
        return [_job(r) for r in self._conn().execute(
            f"SELECT * FROM jobs WHERE action='xcode.build' AND state NOT IN {NONTERMINAL_SQL}"
            " AND rowid > (SELECT rowid FROM jobs WHERE job_id=?) ORDER BY rowid", (job_id,))]

    def mark_sent(self, job_id: str, *, now: int) -> bool:
        cur = self._conn().execute(
            "UPDATE jobs SET pending_send=0, updated_at=?,"
            " state=CASE WHEN state='pending_send' THEN 'submitted' ELSE state END WHERE job_id=?",
            (now, job_id))
        return cur.rowcount == 1

    def mark_dispatching(self, job_id: str, *, now: int) -> bool:
        """A job.submit write is about to start: from here delivery is uncertain, never "unsent". The job stays
        pending_send=1 (resent with identical bytes on the next authentication) until mark_sent or a report."""
        cur = self._conn().execute(
            "UPDATE jobs SET state='submitted', updated_at=? WHERE job_id=? AND state IN ('pending_send','submitted')",
            (now, job_id))
        return cur.rowcount == 1

    def update_state(self, job_id: str, state: JobState, *, now: int) -> bool:
        """A non-terminal transition reported by the helper (so it has the job: pending_send is cleared).
        Ignored once the job is terminal."""
        if state.terminal:
            raise ValueError("use finish() for terminal states")
        cur = self._conn().execute(
            f"UPDATE jobs SET state=?, stale=0, pending_send=0, updated_at=? WHERE job_id=? AND state IN {NONTERMINAL_SQL}",
            (state.value, now, job_id))
        return cur.rowcount == 1

    def finish(self, job_id: str, state: JobState, *, result: Optional[dict], error: Optional[dict],
               now: int) -> bool:
        """Terminal transition; the first one wins (returns False if the job was already terminal)."""
        if not state.terminal:
            raise ValueError("finish() needs a terminal state")
        cur = self._conn().execute(
            "UPDATE jobs SET state=?, stale=0, pending_send=0, updated_at=?, result_json=?, error_json=?"
            f" WHERE job_id=? AND state IN {NONTERMINAL_SQL}",
            (state.value, now, json.dumps(result) if result is not None else None,
             json.dumps(error) if error is not None else None, job_id))
        return cur.rowcount == 1

    def mark_nonterminal_stale(self) -> int:
        return self._conn().execute(f"UPDATE jobs SET stale=1 WHERE state IN {NONTERMINAL_SQL}").rowcount

    def set_detached(self, helper_id: str, since: Optional[int]) -> None:
        if since is None:
            self._conn().execute("UPDATE jobs SET detached_since=NULL WHERE helper_id=?", (helper_id,))
        else:
            self._conn().execute(
                f"UPDATE jobs SET detached_since=? WHERE helper_id=? AND state IN {NONTERMINAL_SQL}",
                (since, helper_id))

    # ------------------------------------------------------------ repository URLs named for granted workspaces
    # The Mac advertises only a repo fingerprint, never its URL (PROTOCOL 6.1). The first build that names a URL
    # matching a grant's fingerprint is remembered here, so later requests can omit it. Stored in meta (no DDL).
    def remember_repo_url(self, fingerprint: str, url: str) -> bool:
        try:
            if bhproto.repo_fingerprint(url) != fingerprint:
                return False
        except Exception:
            return False
        self._conn().execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (f"repo_url:{fingerprint}", url))
        return True

    def repo_url_for(self, fingerprint: str) -> Optional[str]:
        row = self._conn().execute("SELECT v FROM meta WHERE k=?", (f"repo_url:{fingerprint}",)).fetchone()
        try:
            return row[0] if row is not None and bhproto.repo_fingerprint(row[0]) == fingerprint else None
        except Exception:
            return None

    # ------------------------------------------------------------ how a build's commit was chosen
    # Plugin-side provenance for a build job: the bh/* branch the call requested (if any) and how the commit was
    # chosen. The signed contract carries only the commit (it has no branch field), so this is kept beside it, in
    # meta (no DDL). Written once (INSERT OR IGNORE), normally by insert_job in the contract's own transaction;
    # never rewritten, and never inferred later from a ref read.
    def remember_job_source(self, job_id: str, *, branch: Optional[str], commit_source: Optional[str]) -> bool:
        """True if stored; False if invalid or a source is already recorded for this job (it is kept)."""
        source = source_fields(branch, commit_source)
        if source is None:
            return False
        cur = self._conn().execute("INSERT OR IGNORE INTO meta(k, v) VALUES(?, ?)",
                                   (f"job_source:{job_id}", json.dumps(source, sort_keys=True)))
        return cur.rowcount == 1

    def job_source(self, job_id: str) -> Optional[dict]:
        row = self._conn().execute("SELECT v FROM meta WHERE k=?", (f"job_source:{job_id}",)).fetchone()
        try:
            d = json.loads(row[0]) if row is not None else None
            return source_fields(d.get("branch"), d.get("commit_source")) if isinstance(d, dict) else None
        except ValueError:
            return None

    # ------------------------------------------------------------ notices and audit
    def add_notice(self, text: str, *, now: int) -> None:
        self._conn().execute("INSERT INTO notices(created_at, text) VALUES(?,?)", (now, text))

    def take_notices(self, *, now: int) -> List[str]:
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            rows = c.execute("SELECT id, text FROM notices WHERE delivered_at IS NULL ORDER BY id").fetchall()
            c.execute("UPDATE notices SET delivered_at=? WHERE delivered_at IS NULL", (now,))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        return [r["text"] for r in rows]

    def pending_notices(self) -> List[str]:
        return [r[0] for r in self._conn().execute(
            "SELECT text FROM notices WHERE delivered_at IS NULL ORDER BY id")]

    def audit(self, event: str, *, now: int, helper_id: Optional[str] = None, job_id: Optional[str] = None,
              detail: Optional[dict] = None) -> None:
        c = self._conn()
        c.execute("INSERT INTO audit(at, event, helper_id, job_id, detail_json) VALUES(?,?,?,?,?)",
                  (now, event, helper_id, job_id, json.dumps(detail) if detail else None))
        c.execute("DELETE FROM audit WHERE at < ?", (now - 30 * 86400 * 1000,))

    def audit_events(self) -> List[str]:
        return [r[0] for r in self._conn().execute("SELECT event FROM audit ORDER BY id")]

    def close(self) -> None:
        c = getattr(self._local, "c", None)
        if c is not None:
            c.close()
            self._local.c = None
