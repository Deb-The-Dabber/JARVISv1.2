"""SQLite persistence behind a small interface (Contract preamble: single
process + SQLite + transactions + revisions is the intended implementation).

Every canonical mutation happens inside a single BEGIN IMMEDIATE transaction
(Law 2). Reads happen outside write transactions. Crash injection is a
test-only hook: `store.arm_crash(point)` makes the next `crash_point(point)`
raise SimulatedCrash; when raised inside a write transaction the whole
transaction rolls back — exactly what a process kill would leave behind.

This module is deliberately dumb: no business logic, no lifecycle rules.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    completion_policy TEXT NOT NULL,
    created_at TEXT NOT NULL,
    session_origin TEXT,
    integrity TEXT NOT NULL DEFAULT 'OK'
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    goal_id TEXT NOT NULL,
    status TEXT NOT NULL,
    active_plan_id TEXT,
    completion_policy TEXT NOT NULL,
    retry_budget TEXT NOT NULL,
    integrity TEXT NOT NULL DEFAULT 'OK'
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    superseded_by TEXT,
    dependency_graph TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS steps (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    plan_id TEXT NOT NULL,
    status TEXT NOT NULL,
    required INTEGER NOT NULL,
    depends_on TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    step_id TEXT NOT NULL,
    status TEXT NOT NULL,
    capability TEXT NOT NULL,
    arguments TEXT NOT NULL DEFAULT '{}',
    idempotency_class TEXT NOT NULL,
    confirmation_id TEXT,
    retry_of TEXT,
    integrity TEXT NOT NULL DEFAULT 'OK'
);
CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    raw_result TEXT NOT NULL,
    execution_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    acquisition_method TEXT NOT NULL,
    source TEXT NOT NULL,
    relevance_to TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    content TEXT NOT NULL,
    origin_observation_id TEXT
);
CREATE TABLE IF NOT EXISTS claims (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    asserts TEXT NOT NULL,
    based_on TEXT NOT NULL DEFAULT '[]',
    confidence TEXT NOT NULL,
    made_by TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY,
    verifies TEXT NOT NULL,
    method TEXT NOT NULL,
    independence_level TEXT NOT NULL,
    result TEXT NOT NULL,
    timestamp TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS obligations (
    id TEXT PRIMARY KEY,
    origin_action_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    disposition TEXT NOT NULL,
    resolution_budget INTEGER NOT NULL,
    unknown_reason TEXT NOT NULL,
    possible_external_effect TEXT NOT NULL,
    safe_retry_conditions TEXT,
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS obligation_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    obligation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_owner TEXT NOT NULL,
    to_owner TEXT,
    authorized_by TEXT,
    reason TEXT NOT NULL,
    timestamp TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS required_verifications (
    object_id TEXT NOT NULL,
    verification_id TEXT NOT NULL,
    PRIMARY KEY (object_id, verification_id)
);
CREATE TABLE IF NOT EXISTS confirmations (
    id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    action_revision INTEGER NOT NULL,
    capability TEXT NOT NULL,
    arguments TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    authorized_by TEXT,
    reason TEXT NOT NULL,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obligations_owner ON obligations(owner);
CREATE INDEX IF NOT EXISTS idx_obligations_disposition ON obligations(disposition);
CREATE INDEX IF NOT EXISTS idx_actions_step ON actions(step_id);
CREATE INDEX IF NOT EXISTS idx_steps_plan ON steps(plan_id);
CREATE INDEX IF NOT EXISTS idx_plans_task ON plans(task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_goal ON tasks(goal_id);
CREATE INDEX IF NOT EXISTS idx_obs_action ON observations(action_id);
"""


class SimulatedCrash(Exception):
    """Deterministic crash injection. Raised at a labeled boundary when armed.
    Process state is expected to be discarded after catching it (the caller
    closes the Store and reopens the database from disk)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def jdump(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def jload(s, default=None):
    if s is None:
        return default
    return json.loads(s)


class Store:
    """SQLite-backed canonical state store. One connection shared across
    threads (check_same_thread=False); SQLite serializes writers via
    BEGIN IMMEDIATE + busy_timeout, and every mutation re-validates inside
    its own transaction, so concurrent racers get exactly-one-winner."""

    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10.0,
                                    isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(_SCHEMA)  # executescript manages its own commits
        self._armed: dict[str, bool] = {}
        self._write_lock = threading.Lock()
        self._apply_migrations()

    # ── schema migrations (Contract preamble-consistent) ─────────────────────
    # SQLite-native versioning via PRAGMA user_version. Additive upgrades only
    # (ALTER TABLE ... ADD COLUMN). Never destructive. Nothing here invents a
    # framework; each step is idempotent and transaction-bounded.
    SCHEMA_VERSION = 2

    def _apply_migrations(self):
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current >= self.SCHEMA_VERSION:
            return
        with self.write() as conn:
            # v2: Evidence gains Law-23 provenance to its origin Observation.
            if current < 2:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(evidence)").fetchall()]
                if "origin_observation_id" not in cols:
                    conn.execute("ALTER TABLE evidence ADD COLUMN origin_observation_id TEXT")
            conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    # ── transactions ────────────────────────────────────────────────────────
    @contextmanager
    def write(self):
        """Atomic mutation boundary (Law 2). BEGIN IMMEDIATE takes the write
        lock up front; anything raised inside rolls the whole transaction
        back, leaving the pre-transaction state on disk."""
        self._write_lock.acquire()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except SimulatedCrash:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        finally:
            self._write_lock.release()

    def read(self):
        return self._conn

    # ── crash injection (test-only) ────────────────────────────────────────
    def arm_crash(self, point: str):
        """Arm a one-shot crash at the named boundary point."""
        self._armed[point] = True

    def crash_point(self, point: str):
        """Raise SimulatedCrash if `point` is armed (fires once). Safe to call
        both inside and outside transactions."""
        if self._armed.get(point):
            self._armed.pop(point, None)
            raise SimulatedCrash(point)

    # ── audit ──────────────────────────────────────────────────────────────
    def audit(self, conn, object_id: str, kind: str, from_state=None,
              to_state=None, authorized_by=None, reason: str = ""):
        """Append-only audit record. Rejections are auditable (Law 4);
        repairs are permanently audited (Law 30)."""
        conn.execute(
            "INSERT INTO audit_log (object_id, kind, from_state, to_state, authorized_by, reason, timestamp) "
            "VALUES (?,?,?,?,?,?,?)",
            (object_id, kind,
             None if from_state is None else str(from_state),
             None if to_state is None else str(to_state),
             authorized_by, reason, iso(utcnow())),
        )

    def audit_rows(self, object_id: str | None = None, kind: str | None = None,
                   limit: int = 100) -> list[sqlite3.Row]:
        q = "SELECT * FROM audit_log"
        conds, params = [], []
        if object_id:
            conds.append("object_id = ?")
            params.append(object_id)
        if kind:
            conds.append("kind = ?")
            params.append(kind)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        return self._conn.execute(q, params).fetchall()
