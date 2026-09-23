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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from backend.runtime.authorization import GRAPH_ACTION_MUTATE

from . import access, graph_archive
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
    graph_generation: int,
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
            # The graph's generation right after THIS import's replace
            # landed. The worker re-checks this against the live graph
            # before writing any embedding back, so a job left running past a
            # later import (a slow encode, or a crash-recovered job) never
            # overwrites that later import's content — see
            # GraphStorage.commit_generation_embeddings.
            "graph_generation": graph_generation,
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


@dataclass(frozen=True)
class _ReplaceOutcome:
    """What survived a successful validate -> backup -> replace, carried
    forward to whichever embeddings path the caller takes next."""

    node_count: int
    edge_count: int
    backup_path: str
    graph_generation: int


def _validate_backup_replace(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    document: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[_ReplaceOutcome]]:
    """The validate -> backup -> replace half of a whole-graph import.

    Shared by ``import_graph`` (plain ``graph.json``) and
    ``import_graph_archive`` (the archive's ``graph.json`` member) — see
    docs/adr/0007-vector-aware-export-archive.md, "Reusing the PR #664
    pipeline". Only what happens to embeddings afterward differs between the
    two callers, which is why this stops right after the replace and leaves
    that decision to them.

    Returns ``(error_result, None)`` on any failure (nothing was written to
    the live graph in that case, exactly as ``import_graph`` has always
    guaranteed), or ``(None, outcome)`` once the graph has been durably
    replaced.
    """
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_MUTATE, target="import_graph"
    )
    if not decision.allowed:
        return (
            access.build_access_denied_result(
                action=GRAPH_ACTION_MUTATE, target="import_graph", decision=decision
            ),
            None,
        )
    if decision.graph_access.enabled:
        return (
            {
                "success": False,
                "error_code": "import_requires_full_graph_access",
                "message": (
                    "Whole-graph import replaces the entire active graph and is "
                    "refused while the caller's access is narrowed to a subset of "
                    "it — a full replace could remove content the caller cannot "
                    "even see."
                ),
                "graph_replaced": False,
            },
            None,
        )

    validation = validate_import_document(document)
    if not validation.valid:
        return (
            {
                "success": False,
                "error_code": "validation_failed",
                "message": (
                    f"{len(validation.errors)} problem(s) found in the import "
                    f"document; the graph was not changed."
                ),
                "errors": [issue.to_dict() for issue in validation.errors],
                "graph_replaced": False,
            },
            None,
        )

    backup_path = _write_backup(storage)
    if backup_path is None:
        return (
            {
                "success": False,
                "error_code": "backup_failed",
                "message": (
                    "Could not write a pre-import backup of the current graph; "
                    "the import was refused and the graph was not changed."
                ),
                "graph_replaced": False,
            },
            None,
        )

    try:
        storage.replace_all_nodes_and_edges(validation.nodes, validation.edges)
    except Exception as exc:
        return (
            {
                "success": False,
                "error_code": "replace_failed",
                "message": (
                    "Import failed while committing the new graph; the previous "
                    f"graph is unchanged: {exc}"
                ),
                "graph_replaced": False,
                "backup_path": backup_path,
            },
            None,
        )

    # Captured right after the replace: this is the generation whatever
    # happens to embeddings next must still see live when it goes to commit.
    graph_generation = storage.generation

    return None, _ReplaceOutcome(
        node_count=len(validation.nodes),
        edge_count=len(validation.edges),
        backup_path=backup_path,
        graph_generation=graph_generation,
    )


def _enqueue_and_start_regeneration(
    storage: "GraphStorage",
    execution_store: "ExecutionStore",
    outcome: _ReplaceOutcome,
    *,
    event_origin: Optional[str],
    event_session_id: Optional[str],
    event_correlation_id: Optional[str],
    regeneration_reason: Optional[str] = None,
    archive_compatible: Optional[bool] = None,
    embeddings_status: str = "queued",
    extra_result_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Enqueue and start the async embedding-regeneration job, and build the
    success response for it. Shared by ``import_graph`` (which always takes
    this path), ``import_graph_archive``'s fully-incompatible fallback, and
    ``import_graph_archive``'s *partially*-compatible case (see
    docs/adr/0007-vector-aware-export-archive.md, "A compatible archive that
    does not cover every node").

    ``regeneration_reason``, when given, explains WHY embeddings are being
    regenerated rather than restored (an archive-only concept; plain
    ``import_graph`` never sets it). ``archive_compatible`` is included in the
    result only when the caller is the archive path, so a plain
    ``POST /import`` response keeps its exact pre-existing shape.

    ``embeddings_status`` overrides the default ``"queued"`` label — the
    partially-compatible archive case uses ``"restored_partial"`` so the
    caller can tell it apart from a fully-incompatible archive's plain
    ``"queued"``, since here SOME vectors were already restored directly.
    ``extra_result_fields`` (e.g. ``embedded_count``, ``pending_node_ids``)
    is merged into whichever result dict below actually gets returned,
    including the "job could not even be started" fallback — those vectors
    were already committed to the live graph regardless of whether the
    follow-up regeneration job could be started, and the caller should not
    lose sight of that.

    The regeneration job is deliberately NOT given an explicit list of node
    ids to scope itself to. ``import_worker._run_one_import_job`` already
    filters candidate nodes with ``vector_store.has_embedding(node.id)``, and
    by the time this is called for the partial-restore case, the covered
    node ids already have their vectors committed
    (``commit_generation_embeddings`` above) — so that filter alone lands the
    job on exactly the still-missing node ids, with no worker-side change
    needed.
    """
    try:
        job = _enqueue_embedding_job(
            execution_store,
            node_count=outcome.node_count,
            backup_path=outcome.backup_path,
            graph_generation=outcome.graph_generation,
            correlation_id=event_correlation_id,
            session_id=event_session_id,
            origin=event_origin,
        )
        _start_embedding_drain(execution_store, storage)
    except Exception as exc:
        # The graph itself already replaced durably and successfully — only
        # starting the embedding side of the import failed. Reporting this as
        # a failure would invite a retry, and a retry means ANOTHER full
        # replace, which is unnecessary (the graph is already correct) and
        # briefly disruptive. Report success instead, with a status distinct
        # from every other embeddings_status so the caller can tell embedding
        # generation never even started and act on it (re-run
        # scripts/generate_embeddings.py, or import again later) without
        # re-replacing the graph for no reason.
        logger.error(
            "Import: graph replaced successfully but background embedding "
            "generation could not be started: %s",
            exc,
        )
        result = {
            "success": True,
            "graph_replaced": True,
            "node_count": outcome.node_count,
            "edge_count": outcome.edge_count,
            "job_id": None,
            "embeddings_status": "not_started",
            "embeddings_message": (
                "The graph was replaced successfully, but background "
                f"embedding generation could not be started: {exc}. Run "
                "scripts/generate_embeddings.py, or POST the import again "
                "later, to generate embeddings for it."
            ),
            "backup_path": outcome.backup_path,
        }
        if archive_compatible is not None:
            result["archive_compatible"] = archive_compatible
        if extra_result_fields:
            result.update(extra_result_fields)
        return result

    result = {
        "success": True,
        "graph_replaced": True,
        "node_count": outcome.node_count,
        "edge_count": outcome.edge_count,
        "job_id": job.id,
        "embeddings_status": embeddings_status,
        "backup_path": outcome.backup_path,
    }
    if regeneration_reason is not None:
        result["embeddings_message"] = f"Regenerating embeddings: {regeneration_reason}"
    if archive_compatible is not None:
        result["archive_compatible"] = archive_compatible
    if extra_result_fields:
        result.update(extra_result_fields)
    return result


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
    error, outcome = _validate_backup_replace(storage, hook, document)
    if error is not None:
        return error
    assert outcome is not None  # for type checkers: exactly one of the pair is set

    return _enqueue_and_start_regeneration(
        storage,
        execution_store,
        outcome,
        event_origin=event_origin,
        event_session_id=event_session_id,
        event_correlation_id=event_correlation_id,
    )


def import_graph_archive(
    storage: "GraphStorage",
    hook: "GraphAuthorizationHook",
    execution_store: "ExecutionStore",
    archive_bytes: bytes,
    *,
    event_origin: Optional[str] = None,
    event_session_id: Optional[str] = None,
    event_correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Whole-graph REPLACE import from a vector-aware export archive.

    Runs the exact same ``_validate_backup_replace`` pipeline as
    ``import_graph`` for the archive's ``graph.json`` member. What differs is
    what happens to embeddings afterward:

      * a compatible archive (checksums verified, ``embedding_model`` matches
        this instance's live model, ``embeddings.bin`` present and non-empty)
        has its vectors restored directly and synchronously — no async job —
        PROVIDED ``embeddings.bin`` covers every node id in the imported
        graph. When it covers only some of them (the exporting instance had
        its own never-embedded nodes), the covered subset is still restored
        synchronously, but a scoped async job is also enqueued for the rest:
        ``embeddings_status: "restored_partial"``, not the same success as a
        full restore;
      * anything else (mismatch, missing/unreadable manifest, no
        ``embeddings.bin`` at all) still imports the graph, and falls back to
        the same async regeneration job ``import_graph`` always uses, with a
        message explaining why.

    A corrupt ZIP or a checksum failure on any member the manifest names is
    caught by ``graph_archive.extract_archive`` and returned as a failure
    BEFORE ``_validate_backup_replace`` — and therefore before any write to
    the live graph — ever runs. See docs/adr/0007-vector-aware-export-archive.md.
    """
    try:
        extracted = graph_archive.extract_archive(
            archive_bytes, live_model_name=storage.vector_store.model_name
        )
    except graph_archive.ArchiveIntegrityError as exc:
        return {
            "success": False,
            "error_code": "archive_integrity_failed",
            "message": str(exc),
            "graph_replaced": False,
        }

    error, outcome = _validate_backup_replace(storage, hook, extracted.graph_document)
    if error is not None:
        return error
    assert outcome is not None

    if extracted.compatible and extracted.embedding_vectors:
        live_node_ids = {node.id for node in storage.get_all_nodes()}
        vectors = {
            node_id: vector
            for node_id, vector in extracted.embedding_vectors.items()
            if node_id in live_node_ids
        }
        if vectors and storage.commit_generation_embeddings(
            outcome.graph_generation, vectors
        ):
            missing_node_ids = sorted(live_node_ids - vectors.keys())
            if not missing_node_ids:
                return {
                    "success": True,
                    "graph_replaced": True,
                    "node_count": outcome.node_count,
                    "edge_count": outcome.edge_count,
                    "job_id": None,
                    "embeddings_status": "restored",
                    "embedded_count": len(vectors),
                    "backup_path": outcome.backup_path,
                    "archive_compatible": True,
                }
            # The archive is model-compatible and its embeddings.bin was
            # restored for every node id it actually covered, but that is
            # not every node in the imported graph — e.g. the exporting
            # instance itself had some nodes that were never embedded
            # (add_nodes's embedding step warns-and-skips on failure rather
            # than retrying; see backend/agents/execution/import_worker.py's
            # own has_embedding filter, which already anticipates a graph
            # with a partially-embedded node set as a normal steady state).
            # Reporting this as plain "restored" success, with no job ever
            # queued to fill the gap, would be the worst-served of the three
            # outcomes: it looks identical to full success while leaving
            # some nodes permanently unembedded until an operator happens to
            # notice. Report a status that says so, and enqueue a
            # regeneration job for the remainder — see
            # _enqueue_and_start_regeneration's docstring for why that job
            # needs no explicit node-id scoping: the worker's
            # has_embedding filter already skips the node ids this call
            # just committed, so it only ever processes the rest.
            return _enqueue_and_start_regeneration(
                storage,
                execution_store,
                outcome,
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
                regeneration_reason=(
                    f"the archive's {graph_archive.EMBEDDINGS_MEMBER} covered "
                    f"{len(vectors)} of {outcome.node_count} node(s) in the "
                    f"imported graph; regenerating the remaining "
                    f"{len(missing_node_ids)}"
                ),
                archive_compatible=True,
                embeddings_status="restored_partial",
                extra_result_fields={
                    "embedded_count": len(vectors),
                    "pending_node_ids": missing_node_ids,
                    "pending_count": len(missing_node_ids),
                },
            )
        # Either nothing in the archive's vectors actually matched a node id
        # this import just committed, or (far more rarely — the two calls
        # below are not atomic with each other) a concurrent import replaced
        # the graph again in the narrow window between the replace above and
        # this commit, so `commit_generation_embeddings` refused it as stale.
        # Either way, fall back the same way an incompatible archive would,
        # against the graph as it now actually stands.
        reason = (
            "the archive's vectors did not match any node in the imported graph"
            if not vectors
            else (
                "a concurrent import replaced the graph again before the "
                "archive's vectors could be committed"
            )
        )
        return _enqueue_and_start_regeneration(
            storage,
            execution_store,
            outcome,
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
            regeneration_reason=reason,
            archive_compatible=False,
        )

    return _enqueue_and_start_regeneration(
        storage,
        execution_store,
        outcome,
        event_origin=event_origin,
        event_session_id=event_session_id,
        event_correlation_id=event_correlation_id,
        regeneration_reason=extracted.regeneration_reason
        or "the archive's embeddings could not be used",
        archive_compatible=False,
    )


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
