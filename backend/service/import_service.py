"""
Whole-graph import orchestration: validate -> backup -> replace -> enqueue
embeddings.

This module is the sequencing layer described in
docs/adr/0006-graph-import-replace-mode.md. It owns none of the mechanics
itself: validation lives in ``graph_import.py``, the atomic swap lives on
``GraphStorage.replace_all_nodes_and_edges``, and background embedding
generation lives in ``backend.agents.execution.import_worker``. This module
just calls them in the right order and reports what happened at each stage.

Sequencing and failure handling (see the PR / ADR for the full guarantee
list):

  1. Authorize the caller for a graph mutation, and refuse outright if the
     caller's access is narrowed to a subset of the graph — a REPLACE import
     acts on the WHOLE graph, and a caller who can only see part of it must
     never be able to make the part they cannot see disappear.
  2. Validate the whole document. Any problem: return every problem found,
     touch nothing.
  3. Write a pre-import backup of the CURRENT graph. If this fails, refuse to
     proceed — an import that could not be protected by a backup must not run
     one.
  4. Replace the live graph atomically. If the commit fails partway, the
     storage layer restores the in-memory graph to what it was, so the caller
     is guaranteed the live graph is either the fully-imported graph or the
     original graph, never a mix.
  5. Enqueue a durable background job to (re)generate embeddings for the new
     graph, and kick a worker thread to start draining it immediately. Return
     success without waiting for embeddings — they are best-effort and
     regeneratable, never a precondition for the graph itself being valid.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from backend.runtime.authorization import GRAPH_ACTION_MUTATE

from . import access
from .graph_import import validate_import_document
from .serializers import serialize_edges, serialize_nodes

if TYPE_CHECKING:
    from backend.agents.execution import ExecutionJob, ExecutionStore
    from backend.core import GraphStorage
    from backend.runtime.authorization import GraphAuthorizationHook

logger = logging.getLogger(__name__)

BACKUP_SUBDIR = "import-backups"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat().replace("+00:00", "Z") if dt is not None else None


def _write_backup(storage: "GraphStorage") -> Optional[str]:
    """
    Snapshot the CURRENT graph to a timestamped file before it is replaced.

    Written in the same nodes/edges/metadata shape ``GET /export`` returns
    (docs/DATA_MANAGEMENT.md "Graph JSON Format"), so it can be re-imported
    as-is to undo an import after the fact. Returns the path, or None if the
    backup could not be written (the caller must then refuse to import rather
    than proceed unprotected).
    """
    try:
        backup_dir = storage.json_path.parent / BACKUP_SUBDIR
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"import-backup-{stamp}-{uuid.uuid4().hex[:8]}.json"
        content = {
            "version": "1.0",
            "exportDate": datetime.now(timezone.utc).isoformat(),
            "nodes": serialize_nodes(storage.get_all_nodes()),
            "edges": serialize_edges(storage.get_all_edges()),
        }
        tmp_path = backup_path.with_suffix(backup_path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(content, fh)
        os.replace(tmp_path, backup_path)
        return str(backup_path)
    except OSError as exc:
        logger.error("Import: could not write pre-import backup: %s", exc)
        return None


def _enqueue_embedding_job(
    execution_store: "ExecutionStore",
    *,
    node_count: int,
    backup_path: Optional[str],
    correlation_id: Optional[str],
    session_id: Optional[str],
    origin: Optional[str],
) -> "ExecutionJob":
    from backend.agents.execution import ExecutionJob, ExecutionKind

    job = ExecutionJob(
        agent_id="system:graph-import",
        kind=ExecutionKind.IMPORT,
        idempotency_key=f"import:{uuid.uuid4()}",
        payload={
            "phase": "embeddings",
            "node_count": node_count,
            "backup_path": backup_path,
        },
        correlation_id=correlation_id,
        session_id=session_id,
        origin=origin,
    )
    return execution_store.enqueue(job)


def _start_embedding_drain(
    execution_store: "ExecutionStore", storage: "GraphStorage"
) -> None:
    from backend.agents.execution.import_worker import drain_import_jobs

    def _run() -> None:
        try:
            drain_import_jobs(execution_store, storage)
        except (
            Exception
        ):  # pragma: no cover - defensive: never crash the thread silently
            logger.exception("Import embeddings worker thread failed")

    threading.Thread(target=_run, name="import-embeddings-worker", daemon=True).start()


def import_graph(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    execution_store: "ExecutionStore",
    document: Any,
    *,
    event_origin: Optional[str] = None,
    event_session_id: Optional[str] = None,
    event_correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_MUTATE, target="import_graph"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_MUTATE, target="import_graph", decision=decision
        )
    if decision.graph_access.enabled:
        return {
            "success": False,
            "error_code": "import_requires_full_graph_access",
            "message": (
                "Whole-graph import replaces the entire active graph and is "
                "refused while the caller's access is narrowed to a subset of "
                "it — a full replace could remove content the caller cannot "
                "even see."
            ),
            "graph_replaced": False,
        }

    validation = validate_import_document(document)
    if not validation.valid:
        return {
            "success": False,
            "error_code": "validation_failed",
            "message": (
                f"{len(validation.errors)} problem(s) found in the import "
                f"document; the graph was not changed."
            ),
            "errors": [issue.to_dict() for issue in validation.errors],
            "graph_replaced": False,
        }

    backup_path = _write_backup(storage)
    if backup_path is None:
        return {
            "success": False,
            "error_code": "backup_failed",
            "message": (
                "Could not write a pre-import backup of the current graph; "
                "the import was refused and the graph was not changed."
            ),
            "graph_replaced": False,
        }

    try:
        storage.replace_all_nodes_and_edges(validation.nodes, validation.edges)
    except Exception as exc:
        return {
            "success": False,
            "error_code": "replace_failed",
            "message": (
                "Import failed while committing the new graph; the previous "
                f"graph is unchanged: {exc}"
            ),
            "graph_replaced": False,
            "backup_path": backup_path,
        }

    job = _enqueue_embedding_job(
        execution_store,
        node_count=len(validation.nodes),
        backup_path=backup_path,
        correlation_id=event_correlation_id,
        session_id=event_session_id,
        origin=event_origin,
    )
    _start_embedding_drain(execution_store, storage)

    return {
        "success": True,
        "graph_replaced": True,
        "node_count": len(validation.nodes),
        "edge_count": len(validation.edges),
        "job_id": job.id,
        "embeddings_status": "queued",
        "backup_path": backup_path,
    }


_STATUS_LABELS = None  # populated lazily to avoid importing execution eagerly


def _status_labels() -> Dict[Any, str]:
    global _STATUS_LABELS
    if _STATUS_LABELS is None:
        from backend.agents.execution import ExecutionState

        _STATUS_LABELS = {
            ExecutionState.PENDING: "queued",
            ExecutionState.RUNNING: "running",
            ExecutionState.SUCCEEDED: "succeeded",
            ExecutionState.DEAD_LETTER: "failed",
            ExecutionState.CANCELLED: "cancelled",
        }
    return _STATUS_LABELS


def import_job_to_dict(job: "ExecutionJob") -> Dict[str, Any]:
    """Map a stored import ``ExecutionJob`` to the API's status view."""
    payload = job.payload or {}
    result = job.result or {}
    return {
        "id": job.id,
        "kind": job.kind.value,
        "status": _status_labels().get(job.state, job.state.value),
        "embeddings_status": result.get("embeddings_status"),
        "embeddings_message": result.get("message"),
        "embedded_count": result.get("embedded_count"),
        "node_count": payload.get("node_count"),
        "backup_path": payload.get("backup_path"),
        "attempts": job.attempts,
        "correlation_id": job.correlation_id,
        "error": job.last_error or job.dead_letter_reason,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
    }


def get_import_job(
    execution_store: "ExecutionStore", job_id: str
) -> Optional[Dict[str, Any]]:
    from backend.agents.execution import ExecutionKind

    job = execution_store.get(job_id)
    if job is None or job.kind != ExecutionKind.IMPORT:
        return None
    return import_job_to_dict(job)


def list_import_jobs(
    execution_store: "ExecutionStore", *, limit: int = 100
) -> List[Dict[str, Any]]:
    from backend.agents.execution import ExecutionKind

    jobs = execution_store.list_jobs(kind=ExecutionKind.IMPORT, limit=limit)
    return [import_job_to_dict(job) for job in jobs]
