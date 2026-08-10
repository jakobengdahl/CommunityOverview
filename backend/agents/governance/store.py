"""
Durable persistence for agent proposals.

``ProposalStore`` is a small replaceable seam, mirroring the execution-store
style. ``InMemoryProposalStore`` is the volatile reference used in tests and
standalone runs; ``SqliteProposalStore`` persists proposals to a SQLite file so
approve/reject decisions survive a restart. Hosted layers can bind their own
adapter behind the same interface.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Protocol, Sequence, Union, runtime_checkable

from .models import Proposal, ProposalStatus

_SCHEMA_VERSION = 1

_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    agent_name      TEXT,
    tool            TEXT NOT NULL,
    input_args      TEXT NOT NULL DEFAULT '{}',
    autonomy_level  TEXT NOT NULL,
    status          TEXT NOT NULL,
    run_id          TEXT,
    correlation_id  TEXT,
    decided_by      TEXT,
    apply_result    TEXT,
    apply_error     TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    decided_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_proposals_created ON proposals (created_at);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals (status);
"""


@runtime_checkable
class ProposalStore(Protocol):
    def create(self, proposal: Proposal) -> Proposal: ...

    def get(self, proposal_id: str) -> Optional[Proposal]: ...

    def save(self, proposal: Proposal) -> Proposal: ...

    def list_proposals(
        self,
        *,
        agent_id: Optional[str] = None,
        statuses: Optional[Sequence[ProposalStatus]] = None,
        limit: Optional[int] = None,
    ) -> List[Proposal]: ...


class InMemoryProposalStore:
    """Volatile, thread-safe reference proposal store."""

    def __init__(self) -> None:
        self._items: dict[str, Proposal] = {}
        self._lock = threading.Lock()

    def create(self, proposal: Proposal) -> Proposal:
        with self._lock:
            stored = proposal.copy()
            self._items[stored.id] = stored
            return stored.copy()

    def get(self, proposal_id: str) -> Optional[Proposal]:
        with self._lock:
            item = self._items.get(proposal_id)
            return item.copy() if item else None

    def save(self, proposal: Proposal) -> Proposal:
        with self._lock:
            stored = proposal.copy()
            self._items[stored.id] = stored
            return stored.copy()

    def list_proposals(
        self,
        *,
        agent_id: Optional[str] = None,
        statuses: Optional[Sequence[ProposalStatus]] = None,
        limit: Optional[int] = None,
    ) -> List[Proposal]:
        status_set = set(statuses) if statuses else None
        with self._lock:
            items = [
                p.copy()
                for p in self._items.values()
                if (agent_id is None or p.agent_id == agent_id)
                and (status_set is None or p.status in status_set)
            ]
        items.sort(key=lambda p: p.created_at, reverse=True)
        if limit is not None:
            items = items[:limit]
        return items


def _to_epoch(dt: Optional[datetime]) -> Optional[float]:
    return dt.astimezone(timezone.utc).timestamp() if dt is not None else None


def _from_epoch(value: Optional[float]) -> Optional[datetime]:
    return datetime.fromtimestamp(value, tz=timezone.utc) if value is not None else None


class SqliteProposalStore:
    """Durable, file-backed proposal store (SQLite)."""

    def __init__(self, db_path: Union[str, Path]) -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version < _SCHEMA_VERSION:
                self._conn.executescript(_CREATE_SCHEMA)
                self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create(self, proposal: Proposal) -> Proposal:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO proposals (
                    id, agent_id, agent_name, tool, input_args, autonomy_level,
                    status, run_id, correlation_id, decided_by, apply_result,
                    apply_error, created_at, updated_at, decided_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                self._to_row(proposal),
            )
            self._conn.commit()
            return self._get_locked(proposal.id)

    def save(self, proposal: Proposal) -> Proposal:
        with self._lock:
            self._conn.execute(
                """
                UPDATE proposals SET
                    agent_name = ?, tool = ?, input_args = ?, autonomy_level = ?,
                    status = ?, run_id = ?, correlation_id = ?, decided_by = ?,
                    apply_result = ?, apply_error = ?, updated_at = ?, decided_at = ?
                WHERE id = ?
                """,
                (
                    proposal.agent_name,
                    proposal.tool,
                    json.dumps(proposal.input_args or {}),
                    proposal.autonomy_level.value,
                    proposal.status.value,
                    proposal.run_id,
                    proposal.correlation_id,
                    proposal.decided_by,
                    json.dumps(proposal.apply_result)
                    if proposal.apply_result is not None
                    else None,
                    proposal.apply_error,
                    _to_epoch(proposal.updated_at),
                    _to_epoch(proposal.decided_at),
                    proposal.id,
                ),
            )
            self._conn.commit()
            return self._get_locked(proposal.id)

    def get(self, proposal_id: str) -> Optional[Proposal]:
        with self._lock:
            return self._get_locked(proposal_id)

    def list_proposals(
        self,
        *,
        agent_id: Optional[str] = None,
        statuses: Optional[Sequence[ProposalStatus]] = None,
        limit: Optional[int] = None,
    ) -> List[Proposal]:
        clauses = []
        params: list = []
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(s.value for s in statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM proposals {where} ORDER BY created_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [self._row_to_proposal(r) for r in rows]

    # -- internal (call under self._lock) -----------------------------------

    def _get_locked(self, proposal_id: str) -> Optional[Proposal]:
        row = self._conn.execute(
            "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        return self._row_to_proposal(row) if row is not None else None

    @staticmethod
    def _to_row(p: Proposal) -> tuple:
        return (
            p.id,
            p.agent_id,
            p.agent_name,
            p.tool,
            json.dumps(p.input_args or {}),
            p.autonomy_level.value,
            p.status.value,
            p.run_id,
            p.correlation_id,
            p.decided_by,
            json.dumps(p.apply_result) if p.apply_result is not None else None,
            p.apply_error,
            _to_epoch(p.created_at),
            _to_epoch(p.updated_at),
            _to_epoch(p.decided_at),
        )

    @staticmethod
    def _row_to_proposal(row: sqlite3.Row) -> Proposal:
        p = Proposal(
            agent_id=row["agent_id"],
            tool=row["tool"],
            input_args=json.loads(row["input_args"]) if row["input_args"] else {},
            autonomy_level=row["autonomy_level"],
            agent_name=row["agent_name"],
        )
        p.id = row["id"]
        p.status = ProposalStatus(row["status"])
        p.run_id = row["run_id"]
        p.correlation_id = row["correlation_id"]
        p.decided_by = row["decided_by"]
        p.apply_result = (
            json.loads(row["apply_result"]) if row["apply_result"] else None
        )
        p.apply_error = row["apply_error"]
        p.created_at = _from_epoch(row["created_at"])
        p.updated_at = _from_epoch(row["updated_at"])
        p.decided_at = _from_epoch(row["decided_at"])
        return p
