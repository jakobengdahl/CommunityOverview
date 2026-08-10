"""Federation and agent-system status endpoints for the api_host application."""

import logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query

logger = logging.getLogger(__name__)


def register_agent_routes(
    app: FastAPI,
    agent_registry,
    graph_storage,
    federation_manager,
) -> None:
    """Register /federation/* and /agents/* status and trigger endpoints."""

    @app.get("/federation/status")
    async def federation_status() -> Dict[str, Any]:
        """Get federation cache and connectivity status."""
        return federation_manager.get_status()

    @app.post("/federation/sync")
    async def federation_sync() -> Dict[str, Any]:
        """Trigger best-effort sync for all enabled federated graph sources."""
        return await federation_manager.sync_all()

    @app.get("/agents/status")
    async def agents_status() -> Dict[str, Any]:
        """Get agent system status and all worker statuses."""
        return agent_registry.get_all_status()

    @app.get("/agents/integrations")
    async def agents_integrations():
        """Get available MCP integrations for agent configuration."""
        return agent_registry.get_available_mcp_integrations()

    @app.get("/agents/skills")
    async def agents_skills():
        """List Skill nodes from the graph for agent configuration."""
        nodes = graph_storage.search_nodes("", node_types=["Skill"], limit=200)
        return [
            {"id": n.id, "name": n.name, "description": n.description or ""}
            for n in nodes
        ]

    @app.get("/agents/schedules")
    async def agents_schedules():
        """
        List all active agents that have a schedule configured.

        Returns cron expressions and timezone for each schedule so that an
        external scheduler (e.g. GCP Cloud Scheduler, a SaaS plugin) can
        reconcile its jobs against the current agent configuration without
        needing to parse agent node metadata directly.
        """
        return agent_registry.get_schedules()

    @app.get("/agents/runs")
    async def agents_runs(
        agent_id: Optional[str] = Query(None),
        kind: Optional[str] = Query(None, description="Trigger: scheduled | event"),
        status: Optional[str] = Query(
            None,
            description="queued | running | succeeded | failed | cancelled",
        ),
        limit: int = Query(100, ge=1, le=500),
    ) -> List[Dict[str, Any]]:
        """
        List durable AgentRun history, newest-first.

        Each run records the trigger, agent, status, timestamps, correlation,
        attempts and terminal outcome, persisted behind the execution-store
        seam so it survives a restart.
        """
        return agent_registry.list_runs(
            agent_id=agent_id, kind=kind, status=status, limit=limit
        )

    @app.get("/agents/runs/{run_id}")
    async def agents_run_detail(run_id: str) -> Dict[str, Any]:
        """Get a single AgentRun by id."""
        run = agent_registry.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        return run

    @app.post("/agents/{agent_id}/trigger")
    async def agent_trigger(agent_id: str):
        """
        Fire a scheduled_trigger event for the named agent.

        Intended for external schedulers (e.g. GCP Cloud Scheduler) so that
        deployments configured for scale-to-zero do not need AGENTS_SCHEDULER_ENABLED.
        The caller should authenticate this endpoint using OIDC (Cloud Run
        service accounts) or an equivalent mechanism at the infrastructure level.
        """
        success = agent_registry.trigger_agent(agent_id)
        if not success:
            raise HTTPException(
                status_code=404,
                detail=(f"Agent '{agent_id}' not found or has no schedule configured"),
            )
        return {"status": "triggered", "agent_id": agent_id}
