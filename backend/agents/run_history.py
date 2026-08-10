"""
Durable AgentRun history over the execution-store seam.

An *AgentRun* is one durable record of an agent processing one trigger
(scheduled or event): when it started, whether it succeeded or failed, its
correlation/provenance, and a small terminal result. History is persisted
behind the ``ExecutionStore`` seam (`backend/agents/execution/`) so it survives
a restart and can be swapped for a hosted store without touching the API/UI.

This is a **history sink**, not the delivery mechanism: the worker still
delivers events through its in-memory queue. For each run the recorder writes a
job to the store at start and finalizes it at completion. Because delivery does
not go through the store's ``claim_next`` queue, the recorder never claims: it
enqueues the run already in ``RUNNING`` state (so attribution stays with that
specific job id, even when several workers record concurrently) and finalizes
it by id. A run whose process dies mid-flight is left ``RUNNING`` — a truthful
record of what was in flight at the crash.

The store is created with ``RetryPolicy(max_attempts=1)`` so a failed run moves
straight to a terminal state (surfaced as ``failed``) rather than being
rescheduled — history records outcomes, it does not re-run work.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from .execution import (
    ExecutionJob,
    ExecutionKind,
    ExecutionState,
    ExecutionStore,
)

logger = logging.getLogger(__name__)

# ExecutionState -> the status vocabulary the API/UI speak.
_STATUS = {
    ExecutionState.PENDING: "queued",
    ExecutionState.RUNNING: "running",
    ExecutionState.SUCCEEDED: "succeeded",
    ExecutionState.DEAD_LETTER: "failed",
    ExecutionState.CANCELLED: "cancelled",
}

# Reverse map for filtering the read API by AgentRun status.
STATUS_TO_STATE = {status: state for state, status in _STATUS.items()}


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat().replace("+00:00", "Z") if dt is not None else None


def agent_run_to_dict(job: ExecutionJob) -> Dict[str, Any]:
    """Map a stored ``ExecutionJob`` to the AgentRun view the API returns."""
    payload = job.payload or {}
    event = payload.get("event") or {}
    return {
        "id": job.id,
        "agent_id": job.agent_id,
        "agent_name": payload.get("agent_name"),
        "trigger": job.kind.value,  # "scheduled" | "event"
        "event_type": event.get("event_type"),
        "status": _STATUS.get(job.state, job.state.value),
        "attempts": job.attempts,
        "correlation_id": job.correlation_id,
        "session_id": job.session_id,
        "origin": job.origin,
        "error": job.last_error or job.dead_letter_reason,
        "result": job.result,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
    }


class AgentRunRecorder:
    """
    Records agent runs to an ``ExecutionStore`` as durable history.

    Every method is best-effort: a store error is logged and swallowed so that
    history recording can never break live agent processing. When constructed
    without a store (``None``), all methods are no-ops.
    """

    def __init__(self, store: Optional[ExecutionStore]) -> None:
        self._store = store

    @property
    def store(self) -> Optional[ExecutionStore]:
        return self._store

    def record_start(
        self,
        agent_id: str,
        agent_name: str,
        event_payload: Dict[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        """Record a run entering RUNNING. Returns the run id, or None."""
        if self._store is None:
            return None
        try:
            event_id = event_payload.get("event_id") or f"evt-{uuid.uuid4()}"
            event_type = event_payload.get("event_type", "")
            kind = (
                ExecutionKind.SCHEDULED
                if event_type == "scheduled_trigger"
                else ExecutionKind.EVENT
            )
            origin = event_payload.get("origin") or {}
            job = ExecutionJob(
                agent_id=agent_id,
                kind=kind,
                idempotency_key=f"run:{agent_id}:{event_id}",
                payload={"agent_name": agent_name, "event": event_payload},
                state=ExecutionState.RUNNING,
                attempts=1,
                lease_owner=agent_id,
                correlation_id=origin.get("event_correlation_id"),
                session_id=origin.get("event_session_id"),
                origin=origin.get("event_origin"),
            )
            if now is not None:
                job.run_at = now
                job.created_at = now
                job.updated_at = now
            job.started_at = job.run_at
            stored = self._store.enqueue(job)
            return stored.id
        except Exception as exc:  # never let history break processing
            logger.warning("AgentRun history: failed to record start: %s", exc)
            return None

    def record_success(
        self,
        run_id: Optional[str],
        result: Optional[Dict[str, Any]] = None,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        if self._store is None or run_id is None:
            return
        try:
            self._store.complete(run_id, result=result, now=now)
        except Exception as exc:
            logger.warning("AgentRun history: failed to record success: %s", exc)

    def record_failure(
        self,
        run_id: Optional[str],
        error: str,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        if self._store is None or run_id is None:
            return
        try:
            self._store.fail(run_id, error=error, now=now)
        except Exception as exc:
            logger.warning("AgentRun history: failed to record failure: %s", exc)

    # -- inspection (basis for the read API) --------------------------------

    def list_runs(
        self,
        *,
        agent_id: Optional[str] = None,
        kind: Optional[ExecutionKind] = None,
        states: Optional[Sequence[ExecutionState]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if self._store is None:
            return []
        jobs = self._store.list_jobs(
            states=states, agent_id=agent_id, kind=kind, limit=limit
        )
        return [agent_run_to_dict(job) for job in jobs]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        if self._store is None:
            return None
        job = self._store.get(run_id)
        return agent_run_to_dict(job) if job is not None else None
