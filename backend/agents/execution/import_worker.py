"""
Background worker for the embedding-generation half of a graph import.

A whole-graph import (``backend/service/graph_import.py`` +
``backend/service/import_service.py``) validates and REPLACEs the live graph
synchronously, then enqueues one ``ExecutionJob`` (``kind=IMPORT``) and returns
immediately — embedding generation is by far the slowest part of an import and,
per ``docs/DATA_MANAGEMENT.md``, embeddings are already a best-effort,
regeneratable index rather than part of the graph itself, so they must never
block the import's own success signal.

This is the first non-agent consumer of the ``ExecutionStore``'s live PENDING
queue (see ``docs/adr/0006-graph-import-replace-mode.md``). Every other current
producer (``AgentRunRecorder``) enqueues jobs already ``RUNNING``, purely as
history, and never calls ``claim_next`` — so an import job is the only kind of
job any caller of ``claim_next`` on this seam will ever see today.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .models import ExecutionKind
from .store import ExecutionStore

if TYPE_CHECKING:
    from backend.core import GraphStorage
    from .models import ExecutionJob

logger = logging.getLogger(__name__)

# Generous: embedding a large graph with the CPU-only sentence-transformers
# model can genuinely take minutes, and a lease that expires mid-encode would
# let a second worker reclaim and re-run work still legitimately in flight.
DEFAULT_LEASE_SECONDS = 300.0

_SUPERSEDED_MESSAGE = (
    "a later graph import replaced the graph before this job's embeddings "
    "could be committed; the graph's own content is unaffected, and the "
    "later import's own job (re)generates embeddings for the content that "
    "is actually live"
)


def _mark_superseded(store: ExecutionStore, job: "ExecutionJob") -> None:
    """Cancel a job whose embeddings a later import has made stale.

    Distinct from both ``succeeded`` (nothing was actually committed) and a
    genuine ``failed`` (nothing went wrong; the job is simply obsolete, so it
    must not be retried). ``cancel`` is the one terminal state that already
    means neither of those, and ``result`` carries why, the same way
    ``complete``'s does — see ``docs/DATA_MANAGEMENT.md``, "Importing a
    graph".
    """
    store.cancel(
        job.id,
        result={
            "embeddings_status": "superseded",
            "message": _SUPERSEDED_MESSAGE,
        },
    )


def _run_one_import_job(
    store: ExecutionStore, storage: "GraphStorage", job: "ExecutionJob"
) -> None:
    # The generation the graph was at right after THIS job's import replaced
    # it (see backend/service/import_service.py). A job enqueued before this
    # field existed carries None, which always commits — nothing to compare
    # against for a job from an older build. Checked here, BEFORE reading
    # `storage.nodes` at all: once the graph has already moved on, that
    # attribute is a DIFFERENT (newer) graph's nodes, not the ones this job
    # was ever about, and no work should be attempted against them under this
    # job's identity.
    job_generation = job.payload.get("graph_generation")
    if job_generation is not None and storage.generation != job_generation:
        _mark_superseded(store, job)
        return

    nodes = [
        node
        for node in storage.nodes.values()
        if not storage.vector_store.has_embedding(node.id)
    ]
    try:
        # Encode off to the side — `compute_node_embeddings` never touches the
        # live index — so a replace landing during this (potentially
        # minutes-long) step cannot yet be corrupted by it. The result is only
        # ever written back through `commit_generation_embeddings`, which
        # re-checks the generation and does the check-and-write as one atomic
        # step under GraphStorage's own lock, closing the window a bare
        # check-then-act here would leave open.
        vectors = storage.vector_store.compute_node_embeddings(nodes) if nodes else {}
        if not storage.commit_generation_embeddings(job_generation, vectors):
            _mark_superseded(store, job)
            return
        store.complete(
            job.id,
            result={
                "embeddings_status": "succeeded",
                "embedded_count": len(nodes),
            },
        )
    except ImportError as exc:
        # The optional ML extras (sentence-transformers) are not installed.
        # Retrying cannot fix this, and the graph replace already succeeded
        # and is fully valid without embeddings — semantic search degrades to
        # name-based matching, the same way an unreadable sidecar degrades
        # elsewhere in this codebase (see docs/DATA_MANAGEMENT.md). This is a
        # terminal SUCCEEDED with a degraded result, not a failure to retry.
        logger.warning(
            "Import job %s: embeddings unavailable (ML extras not installed): %s",
            job.id,
            exc,
        )
        store.complete(
            job.id,
            result={
                "embeddings_status": "unavailable",
                "message": (
                    "sentence-transformers is not installed; the graph was "
                    "imported successfully but has no search embeddings. "
                    "Install backend/requirements-ml.txt and run "
                    "scripts/generate_embeddings.py, or POST the import again "
                    "once the ML extras are installed."
                ),
            },
        )
    except Exception as exc:
        # A genuine failure (encoding error, sidecar write failure, ...).
        # store.fail() reschedules with backoff up to the store's retry
        # policy, then dead-letters — surfaced to the status endpoint as
        # "failed" so an operator knows to investigate or re-run
        # scripts/generate_embeddings.py by hand. The graph replace itself is
        # NOT rolled back: it already committed and is valid data.
        logger.warning("Import job %s: embedding generation failed: %s", job.id, exc)
        store.fail(job.id, error=str(exc))


def drain_import_jobs(
    store: ExecutionStore,
    storage: "GraphStorage",
    *,
    worker_id: str = "import-worker",
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> int:
    """
    Claim and process every currently-runnable IMPORT job, oldest first.

    Safe to call concurrently and repeatedly: ``claim_next`` is atomic, so two
    callers draining the same store split the work rather than double-process
    a job. Returns the number of jobs processed.
    """
    processed = 0
    while True:
        job = store.claim_next(worker_id, lease_seconds=lease_seconds)
        if job is None:
            return processed
        if job.kind != ExecutionKind.IMPORT:
            # Defensive only: nothing else in this codebase enqueues a
            # PENDING job today (see module docstring). Refuse to process
            # foreign work under an embedding worker's assumptions rather
            # than silently mis-handling it.
            logger.error(
                "Import worker claimed a non-import job %s (kind=%s); failing "
                "it back rather than processing it as an import",
                job.id,
                job.kind.value,
            )
            store.fail(job.id, error="claimed by the import embeddings worker")
            continue
        _run_one_import_job(store, storage, job)
        processed += 1


def recover_import_jobs(store: ExecutionStore, storage: "GraphStorage") -> int:
    """
    Startup recovery for import jobs left mid-flight by a crashed process.

    Call once at app startup, after ``storage`` has finished loading.
    ``recover_stale`` resets any job left RUNNING with an expired lease back
    to PENDING (a worker that died mid-encode), and the drain that follows
    resumes it — this is what makes an import job durable across a restart.
    """
    store.recover_stale()
    return drain_import_jobs(store, storage)
