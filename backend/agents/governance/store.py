"""
Durable persistence for agent proposals.

``ProposalStore`` is a small replaceable seam, mirroring the execution-store
style. ``InMemoryProposalStore`` is the volatile reference used in tests and
standalone runs; ``SqliteProposalStore`` persists proposals to a SQLite file so
approve/reject decisions survive a restart. Hosted layers can bind their own
adapter behind the same interface.

Both stores also carry the retention operations the run-history sweep applies
to proposals (``list_decided_older_than``, ``redact``, ``purge``). They act on
decided proposals only; a pending proposal is never redacted or purged.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Protocol, Sequence, Union, runtime_checkable

from backend.core.actor_attribution import validate_erasure

from .models import Proposal, ProposalStatus, utcnow

_SCHEMA_VERSION = 3

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
    decided_at      REAL,
    session_id      TEXT,
    causation_depth INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_proposals_created ON proposals (created_at);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals (status);
"""


@runtime_checkable
class ProposalStore(Protocol):
    def create(self, proposal: Proposal) -> Proposal: ...

    def get(self, proposal_id: str) -> Optional[Proposal]: ...

    def save(self, proposal: Proposal) -> Proposal: ...

    def decide(
        self,
        proposal_id: str,
        to_status: ProposalStatus,
        *,
        decided_by: Optional[str] = None,
        decided_at: Optional[datetime] = None,
    ) -> Optional[Proposal]: ...

    def list_proposals(
        self,
        *,
        agent_id: Optional[str] = None,
        statuses: Optional[Sequence[ProposalStatus]] = None,
        limit: Optional[int] = None,
    ) -> List[Proposal]: ...

    def list_decided_older_than(
        self, cutoff: datetime, *, redacted: Optional[bool] = None
    ) -> List[Proposal]: ...

    def redact(self, proposal_ids: Sequence[str]) -> int: ...

    def erase_actor(self, actor_id: str, replacement_token: str) -> int: ...

    def purge(self, proposal_ids: Sequence[str]) -> int: ...


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

    def decide(
        self,
        proposal_id: str,
        to_status: ProposalStatus,
        *,
        decided_by: Optional[str] = None,
        decided_at: Optional[datetime] = None,
    ) -> Optional[Proposal]:
        when = decided_at or utcnow()
        with self._lock:
            item = self._items.get(proposal_id)
            if item is None or item.status is not ProposalStatus.PENDING:
                return None
            item.status = to_status
            item.decided_by = decided_by
            item.decided_at = when
            item.updated_at = when
            return item.copy()

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

    def list_decided_older_than(
        self, cutoff: datetime, *, redacted: Optional[bool] = None
    ) -> List[Proposal]:
        with self._lock:
            items = [
                p.copy()
                for p in self._items.values()
                if p.status.is_decided
                and _decided_or_updated(p) < cutoff
                and (redacted is None or is_redacted(p) == redacted)
            ]
        items.sort(key=_decided_or_updated)
        return items

    def redact(self, proposal_ids: Sequence[str]) -> int:
        count = 0
        with self._lock:
            for proposal_id in dict.fromkeys(proposal_ids):
                item = self._items.get(proposal_id)
                if item is None or not item.status.is_decided or is_redacted(item):
                    continue
                item.input_args = {}
                item.apply_result = None
                item.apply_error = None
                count += 1
        return count

    def erase_actor(self, actor_id: str, replacement_token: str) -> int:
        validate_erasure(actor_id, replacement_token)
        count = 0
        with self._lock:
            for item in self._items.values():
                if item.decided_by == actor_id:
                    item.decided_by = replacement_token
                    count += 1
        return count

    def purge(self, proposal_ids: Sequence[str]) -> int:
        count = 0
        with self._lock:
            for proposal_id in dict.fromkeys(proposal_ids):
                item = self._items.get(proposal_id)
                if item is not None and item.status.is_decided:
                    del self._items[proposal_id]
                    count += 1
        return count


def is_redacted(p: Proposal) -> bool:
    """Whether ``p`` carries none of the content retention strips."""
    return not p.input_args and p.apply_result is None and p.apply_error is None


def _decided_or_updated(p: Proposal) -> datetime:
    return p.decided_at or p.updated_at


def _to_epoch(dt: Optional[datetime]) -> Optional[float]:
    return dt.astimezone(timezone.utc).timestamp() if dt is not None else None


def _from_epoch(value: Optional[float]) -> Optional[datetime]:
    return datetime.fromtimestamp(value, tz=timezone.utc) if value is not None else None


class SqliteProposalStore:
    """Durable, file-backed proposal store (SQLite)."""

    def __init__(self, db_path: Union[str, Path], *, read_only: bool = False) -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        if read_only:
            # For a dry-run report: no WAL switch, no schema creation or
            # migration, and any write statement is refused by SQLite.
            self._conn = sqlite3.connect(
                f"{Path(self.db_path).resolve().as_uri()}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=5000")
            return
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        # Overwrite freed pages so redacted or purged content does not linger.
        self._conn.execute("PRAGMA secure_delete=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version < _SCHEMA_VERSION:
                self._conn.executescript(_CREATE_SCHEMA)
                columns = {
                    row["name"]
                    for row in self._conn.execute("PRAGMA table_info(proposals)")
                }
                if "session_id" not in columns:
                    # v2: the session a proposal was made in, so approving it
                    # stages there (docs/SESSION_OVERLAY_CONTRACT.md §9.1).
                    self._conn.execute(
                        "ALTER TABLE proposals ADD COLUMN session_id TEXT"
                    )
                if "causation_depth" not in columns:
                    self._conn.execute(
                        "ALTER TABLE proposals ADD COLUMN causation_depth INTEGER NOT NULL DEFAULT 1"
                    )
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
                    apply_error, created_at, updated_at, decided_at, session_id, causation_depth
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    (
                        json.dumps(proposal.apply_result)
                        if proposal.apply_result is not None
                        else None
                    ),
                    proposal.apply_error,
                    _to_epoch(proposal.updated_at),
                    _to_epoch(proposal.decided_at),
                    proposal.id,
                ),
            )
            self._conn.commit()
            return self._get_locked(proposal.id)

    def decide(
        self,
        proposal_id: str,
        to_status: ProposalStatus,
        *,
        decided_by: Optional[str] = None,
        decided_at: Optional[datetime] = None,
    ) -> Optional[Proposal]:
        # Atomic compare-and-set: only the caller that flips PENDING wins, so a
        # decision (and any apply that follows) happens exactly once even across
        # processes sharing the file.
        when = _to_epoch(decided_at or utcnow())
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE proposals SET
                    status = ?, decided_by = ?, decided_at = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    to_status.value,
                    decided_by,
                    when,
                    when,
                    proposal_id,
                    ProposalStatus.PENDING.value,
                ),
            )
            self._conn.commit()
            if cur.rowcount != 1:
                return None
            return self._get_locked(proposal_id)

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
        # WHERE holds only fixed column clauses and "?" placeholders; values are bound.
        sql = f"SELECT * FROM proposals {where} ORDER BY created_at DESC"  # nosec B608
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [self._row_to_proposal(r) for r in rows]

    def list_decided_older_than(
        self, cutoff: datetime, *, redacted: Optional[bool] = None
    ) -> List[Proposal]:
        want = None if redacted is None else int(redacted)
        with self._lock:
            # The parenthesised predicate is the SQL twin of ``is_redacted``.
            rows = self._conn.execute(
                """
                SELECT * FROM proposals
                WHERE status != ? AND COALESCE(decided_at, updated_at) < ?
                  AND (? IS NULL OR (
                    input_args = '{}' AND apply_result IS NULL
                    AND apply_error IS NULL
                  ) = ?)
                ORDER BY COALESCE(decided_at, updated_at) ASC
                """,
                (ProposalStatus.PENDING.value, _to_epoch(cutoff), want, want),
            ).fetchall()
            return [self._row_to_proposal(r) for r in rows]

    def redact(self, proposal_ids: Sequence[str]) -> int:
        # The decided/unredacted guard lives in the SQL, so a proposal still
        # pending (or already redacted) is left alone whoever listed it.
        params = [
            (proposal_id, ProposalStatus.PENDING.value)
            for proposal_id in dict.fromkeys(proposal_ids)
        ]
        return self._write_many(
            """
            UPDATE proposals SET
                input_args = '{}', apply_result = NULL, apply_error = NULL
            WHERE id = ? AND status != ? AND NOT (
                input_args = '{}' AND apply_result IS NULL AND apply_error IS NULL
            )
            """,
            params,
        )

    def erase_actor(self, actor_id: str, replacement_token: str) -> int:
        validate_erasure(actor_id, replacement_token)
        return self._write_many(
            "UPDATE proposals SET decided_by = ? WHERE decided_by = ?",
            [(replacement_token, actor_id)],
        )

    def purge(self, proposal_ids: Sequence[str]) -> int:
        params = [
            (proposal_id, ProposalStatus.PENDING.value)
            for proposal_id in dict.fromkeys(proposal_ids)
        ]
        return self._write_many(
            "DELETE FROM proposals WHERE id = ? AND status != ?", params
        )

    # -- internal (call under self._lock) -----------------------------------

    def _write_many(self, sql: str, params: list) -> int:
        if not params:
            return 0
        with self._lock:
            try:
                count = self._conn.executemany(sql, params).rowcount
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            if count:
                # Fold the WAL back so pre-redaction page images do not linger.
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return count

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
            p.session_id,
            p.causation_depth,
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
        p.session_id = row["session_id"]
        p.causation_depth = (
            row["causation_depth"] if "causation_depth" in row.keys() else 1
        )
        return p
