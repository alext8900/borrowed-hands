"""The helper's durable job table: `jobs.sqlite3` beside config.toml (PROTOCOL 4.5).

WAL, synchronous=FULL; every transition commits before the frame announcing it is sent. The replay check
(PROTOCOL 4.2 step 6) is one transaction in `claim`. Terminal rows keep their result 24 h for re-delivery,
and the row (job_id + nonce) 7 days for replay checks. `bh cancel` sets `cancel_requested`, which the
daemon polls: the CLI never talks to the daemon any other way.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Union

STATES = ("received", "queued", "materializing", "building", "succeeded", "failed", "cancelled", "expired")
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "expired"})
RESULT_KEEP_MS = 24 * 3600_000
ROW_KEEP_MS = 7 * 86400_000

_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  nonce TEXT NOT NULL UNIQUE,
  contract_hash TEXT NOT NULL,
  contract BLOB NOT NULL,
  action TEXT NOT NULL,
  workspace_id TEXT,
  state TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  result_json TEXT,
  error_json TEXT,
  delivered INTEGER NOT NULL DEFAULT 0,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  grant_json TEXT,
  rejected INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
"""


@dataclass(frozen=True)
class JobRow:
    job_id: str
    nonce: str
    contract_hash: str
    contract: bytes
    action: str
    workspace_id: Optional[str]
    state: str
    expires_at: int
    result: Optional[dict]
    error: Optional[dict]
    delivered: bool
    cancel_requested: bool
    created_at: int
    updated_at: int

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


@dataclass(frozen=True)
class Inserted:
    pass


@dataclass(frozen=True)
class Duplicate:
    row: JobRow


@dataclass(frozen=True)
class Replay:
    pass


@dataclass(frozen=True)
class Busy:
    """Too many refused requests in the replay window: refuse new ones without storing them."""


Claim = Union[Inserted, Duplicate, Replay, Busy]
REPLAY_WINDOW_MS = 600_000 + 60_000     # a contract lives <= 10 min, plus the clock skew allowance
MAX_RECENT_REJECTED = 2000
# Refused requests stay as compact tombstones (id, nonce, hash, expiry; no contract bytes) for the full row
# retention, so an id or nonce can never be reused within it. Their number is bounded by refusing new requests
# once the cap is reached, never by forgetting old ones early.
MAX_TOMBSTONES = 50_000


def _row(r) -> JobRow:
    return JobRow(job_id=r[0], nonce=r[1], contract_hash=r[2], contract=bytes(r[3]), action=r[4],
                  workspace_id=r[5], state=r[6], expires_at=r[7],
                  result=json.loads(r[8]) if r[8] else None, error=json.loads(r[9]) if r[9] else None,
                  delivered=bool(r[10]), cancel_requested=bool(r[11]), created_at=r[12], updated_at=r[13])


_COLS = ("job_id, nonce, contract_hash, contract, action, workspace_id, state, expires_at, result_json, "
         "error_json, delivered, cancel_requested, created_at, updated_at")


def restarted_error() -> dict:
    return {"code": "internal_error", "message": "the Mac helper restarted while this job was running",
            "detail": {"reason": "helper_restarted"}}


class JobStore:
    def __init__(self, path: Path):
        path = Path(path)
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_DDL)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(jobs)")}
        for col, ddl in (("grant_json", "TEXT"), ("rejected", "INTEGER NOT NULL DEFAULT 0")):
            if col not in cols:
                self._db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
        self.max_recent_rejected = MAX_RECENT_REJECTED
        self.max_tombstones = MAX_TOMBSTONES
        for side in ("-wal", "-shm"):
            p = Path(str(path) + side)
            if p.exists():
                os.chmod(p, 0o600)

    def close(self) -> None:
        self._db.close()

    def pragma(self, name: str):
        return self._db.execute(f"PRAGMA {name}").fetchone()[0]

    # ------------------------------------------------------------ step 6
    def claim(self, *, job_id: str, nonce: str, contract_hash: str, contract: bytes, action: str,
              workspace_id: Optional[str], expires_at: int, now: int) -> Claim:
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            r = db.execute(f"SELECT {_COLS} FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if r is not None:
                db.execute("COMMIT")
                row = _row(r)
                return Duplicate(row) if row.contract_hash == contract_hash else Replay()
            if db.execute("SELECT 1 FROM jobs WHERE nonce=?", (nonce,)).fetchone():
                db.execute("COMMIT")
                return Replay()
            n = db.execute("SELECT COUNT(*) FROM jobs WHERE rejected=1 AND created_at > ?",
                           (now - REPLAY_WINDOW_MS,)).fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM jobs WHERE rejected=1").fetchone()[0]
            if n >= self.max_recent_rejected or total >= self.max_tombstones:
                db.execute("COMMIT")
                return Busy()
            db.execute(f"INSERT INTO jobs ({_COLS}) VALUES (?,?,?,?,?,?,?,?,NULL,NULL,0,0,?,?)",
                       (job_id, nonce, contract_hash, contract, action, workspace_id, "received", expires_at,
                        now, now))
            db.execute("COMMIT")
            return Inserted()
        except BaseException:
            db.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------ transitions
    def set_state(self, job_id: str, state: str, now: int) -> None:
        if state not in STATES or state in TERMINAL:
            raise ValueError(f"set_state cannot move to {state}; use finish()")
        cur = self._db.execute("UPDATE jobs SET state=?, updated_at=? WHERE job_id=? AND state NOT IN "
                               "('succeeded','failed','cancelled','expired')", (state, now, job_id))
        if cur.rowcount != 1:
            raise ValueError(f"job {job_id[:8]} is unknown or already finished")

    def finish(self, job_id: str, state: str, result: Optional[dict], error: Optional[dict], now: int,
               delivered: bool = False) -> bool:
        """Terminal transition. Returns False if the job was already terminal (first terminal state wins)."""
        if state not in TERMINAL:
            raise ValueError(f"{state} is not terminal")
        cur = self._db.execute(
            "UPDATE jobs SET state=?, result_json=?, error_json=?, delivered=?, updated_at=? WHERE job_id=? "
            "AND state NOT IN ('succeeded','failed','cancelled','expired')",
            (state, json.dumps(result) if result is not None else None,
             json.dumps(error) if error is not None else None, int(delivered), now, job_id))
        return cur.rowcount == 1

    def finish_rejected(self, job_id: str, error: dict, now: int) -> None:
        """A request refused after the replay claim: failed with the error, contract bytes dropped. The row
        stays as a replay tombstone (id, nonce, hash, expiry) for the full retention (ROW_KEEP_MS)."""
        self.finish(job_id, "failed", None, error, now)
        self._db.execute("UPDATE jobs SET contract=x'', rejected=1 WHERE job_id=?", (job_id,))

    def set_grant(self, job_id: str, grant: dict) -> None:
        """The workspace grant a job was accepted under (for the dequeue comparison after a restart)."""
        self._db.execute("UPDATE jobs SET grant_json=? WHERE job_id=?", (json.dumps(grant), job_id))

    def grant_of(self, job_id: str) -> Optional[dict]:
        r = self._db.execute("SELECT grant_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return json.loads(r[0]) if r and r[0] else None

    # ------------------------------------------------------------ reads
    def get(self, job_id: str) -> Optional[JobRow]:
        r = self._db.execute(f"SELECT {_COLS} FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return _row(r) if r else None

    def by_state(self, *states: str) -> list[JobRow]:
        q = ",".join("?" * len(states))
        return [_row(r) for r in self._db.execute(
            f"SELECT {_COLS} FROM jobs WHERE state IN ({q}) ORDER BY created_at, rowid", states)]

    def active(self) -> list[JobRow]:
        return self.by_state("received", "queued", "materializing", "building")

    def recent(self, limit: int = 20, include_terminal: bool = True) -> list[JobRow]:
        where = "" if include_terminal else "WHERE state NOT IN ('succeeded','failed','cancelled','expired')"
        return [_row(r) for r in self._db.execute(
            f"SELECT {_COLS} FROM jobs {where} ORDER BY updated_at DESC, rowid DESC LIMIT ?", (limit,))]

    def undelivered(self, limit: int = 100) -> list[JobRow]:
        return [_row(r) for r in self._db.execute(
            f"SELECT {_COLS} FROM jobs WHERE delivered=0 AND state IN ('succeeded','failed','cancelled','expired')"
            f" ORDER BY updated_at, rowid LIMIT ?", (limit,))]

    def mark_delivered(self, job_ids: Iterable[str]) -> None:
        ids = list(job_ids)
        if ids:
            q = ",".join("?" * len(ids))
            self._db.execute(f"UPDATE jobs SET delivered=1 WHERE job_id IN ({q}) AND state IN "
                             f"('succeeded','failed','cancelled','expired')", ids)

    @staticmethod
    def result_body(row: JobRow) -> dict:
        return {"job_id": row.job_id, "state": row.state, "result": row.result, "error": row.error}

    # ------------------------------------------------------------ cancel requests from `bh cancel`
    def request_cancel(self, job_id: str) -> bool:
        cur = self._db.execute("UPDATE jobs SET cancel_requested=1 WHERE job_id=? AND state NOT IN "
                               "('succeeded','failed','cancelled','expired')", (job_id,))
        return cur.rowcount == 1

    def cancel_requests(self) -> list[str]:
        return [r[0] for r in self._db.execute(
            "SELECT job_id FROM jobs WHERE cancel_requested=1 AND state NOT IN "
            "('succeeded','failed','cancelled','expired')")]

    # ------------------------------------------------------------ start-up and housekeeping
    def recover_on_start(self, now: int, keep_running: bool = False) -> list[JobRow]:
        """PROTOCOL 4.5: running (or mid-check) rows fail with helper_restarted; queued rows past expiry
        expire; the remaining queued rows are returned, oldest first, to be re-queued. With `keep_running`
        (the start-up cleanup failed, so a build may still be alive) running rows stay unresolved."""
        for r in self.by_state("received", *(() if keep_running else ("materializing", "building"))):
            self.finish(r.job_id, "failed", None, restarted_error(), now)
        still = []
        for r in self.by_state("queued"):
            if now > r.expires_at:
                self.finish(r.job_id, "expired", None, {"code": "expired", "message":
                            "the job expired before it started", "detail": {"reason": "expired_in_queue"}}, now)
            else:
                still.append(r)
        return still

    def prune(self, now: int) -> None:
        self._db.execute("UPDATE jobs SET result_json=NULL WHERE state IN ('succeeded','failed','cancelled',"
                         "'expired') AND updated_at < ?", (now - RESULT_KEEP_MS,))
        self._db.execute("DELETE FROM jobs WHERE state IN ('succeeded','failed','cancelled','expired') AND "
                         "updated_at < ?", (now - ROW_KEEP_MS,))
