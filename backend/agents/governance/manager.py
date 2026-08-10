"""
Governance manager: the read/decision surface over the proposal store.

Owns listing, fetching, and the approve/reject decisions. Approving an
``act_after_approval`` proposal applies the captured action using a raw tool
executor (built by the registry, which holds the MCP loader and graph service);
``propose`` proposals are approve-to-acknowledge and are not applied.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

from .models import Proposal, ProposalStatus, utcnow
from .store import ProposalStore

logger = logging.getLogger(__name__)

# executor_factory(agent_id) -> callable(namespaced_tool, input_args) -> result
ExecutorFactory = Callable[[str], Callable[[str, Dict[str, Any]], Any]]


def _small_result(result: Any) -> Optional[Dict[str, Any]]:
    if result is None:
        return None
    if isinstance(result, dict):
        return result
    return {"result": str(result)[:500]}


class GovernanceManager:
    """Approve/reject decisions and application over the proposal store."""

    def __init__(
        self,
        store: ProposalStore,
        executor_factory: Optional[ExecutorFactory] = None,
    ) -> None:
        self._store = store
        self._executor_factory = executor_factory

    @property
    def store(self) -> ProposalStore:
        return self._store

    def list_proposals(
        self,
        *,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        statuses: Optional[Sequence[ProposalStatus]] = None
        if status is not None:
            try:
                statuses = [ProposalStatus(status)]
            except ValueError:
                return []
        return [
            p.to_dict()
            for p in self._store.list_proposals(
                agent_id=agent_id, statuses=statuses, limit=limit
            )
        ]

    def get_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        p = self._store.get(proposal_id)
        return p.to_dict() if p is not None else None

    def reject(
        self, proposal_id: str, *, decided_by: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        existing = self._store.get(proposal_id)
        if existing is None:
            return None
        # Atomically claim the pending -> rejected transition. A None result
        # means it was already decided (sticky) — return the current state.
        claimed = self._store.decide(
            proposal_id, ProposalStatus.REJECTED, decided_by=decided_by
        )
        if claimed is None:
            current = self._store.get(proposal_id)
            return current.to_dict() if current is not None else None
        return claimed.to_dict()

    def approve(
        self, proposal_id: str, *, decided_by: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        existing = self._store.get(proposal_id)
        if existing is None:
            return None
        # Atomically claim pending -> approved so at most one caller applies the
        # action (exactly-once, even across processes sharing the store).
        claimed = self._store.decide(
            proposal_id, ProposalStatus.APPROVED, decided_by=decided_by
        )
        if claimed is None:
            current = self._store.get(proposal_id)
            return current.to_dict() if current is not None else None
        if claimed.autonomy_level.applies_on_approval:
            self._apply(claimed)
            claimed.updated_at = utcnow()
            claimed = self._store.save(claimed)
        return claimed.to_dict()

    # -- internal -----------------------------------------------------------

    def _apply(self, p: Proposal) -> None:
        if self._executor_factory is None:
            p.status = ProposalStatus.APPLY_FAILED
            p.apply_error = "No executor configured to apply the approved action."
            return
        try:
            executor = self._executor_factory(p.agent_id)
            result = executor(p.tool, dict(p.input_args))
            if isinstance(result, dict) and result.get("error"):
                p.status = ProposalStatus.APPLY_FAILED
                p.apply_error = str(result["error"])
            else:
                p.status = ProposalStatus.APPLIED
                p.apply_result = _small_result(result)
        except Exception as exc:
            logger.error("Governance: applying proposal %s failed: %s", p.id, exc)
            p.status = ProposalStatus.APPLY_FAILED
            p.apply_error = str(exc)
