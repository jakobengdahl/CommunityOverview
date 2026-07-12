"""Startup diagnostics builders for the api_host application.

Pure functions that assemble the structured startup/readiness diagnostics
exposed at ``/ready`` and ``/diagnostics/startup``. They read from the loaded
configuration and the initialized runtime components; they never mutate state.
"""

import json
import logging
from typing import Any, Dict

from backend.core import GraphStorage
from backend.llm.llm_providers import get_llm_availability
from backend.agents import AgentRegistry
from backend.federation import FederationManager
from backend import config_loader

from .config import AppConfig

logger = logging.getLogger(__name__)


PUBLIC_STARTUP_DIAGNOSTICS_PATH = "/diagnostics/startup"
PUBLIC_READINESS_PATH = "/ready"


def count_enabled_capabilities(capabilities: Dict[str, Any]) -> Dict[str, int]:
    manifest = capabilities.get("capabilities", [])
    return {
        "configured": len(manifest),
        "enabled": sum(1 for capability in manifest if capability.get("enabled", True)),
        "disabled": sum(
            1 for capability in manifest if not capability.get("enabled", True)
        ),
    }


def collect_graph_integrity_diagnostics(graph_storage: GraphStorage) -> Dict[str, Any]:
    dangling_edges = 0
    self_referencing_edges = 0

    for edge in graph_storage.edges.values():
        source = getattr(edge, "source", None)
        target = getattr(edge, "target", None)

        if source == target:
            self_referencing_edges += 1

        if source not in graph_storage.nodes or target not in graph_storage.nodes:
            dangling_edges += 1

    return {
        "status": "ok" if dangling_edges == 0 else "degraded",
        "node_count": len(graph_storage.nodes),
        "edge_count": len(graph_storage.edges),
        "dangling_edge_count": dangling_edges,
        "self_referencing_edge_count": self_referencing_edges,
    }


def build_public_tenant_context(tenant_context: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize tenant configuration without exposing tenant identifiers."""
    return {
        "environment": tenant_context["environment"],
        "tenant_id_configured": bool(tenant_context["tenant_id"]),
        "tenant_name_configured": bool(tenant_context["tenant_name"]),
        "tenant_context_configured": bool(
            tenant_context["tenant_id"] or tenant_context["tenant_name"]
        ),
    }


def build_public_config_context(config_context: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize config resolution context without exposing tenant identifiers."""
    return {
        "environment": config_context["environment"],
        "tenant_context_configured": bool(
            config_context["tenant_id"] or config_context["tenant_name"]
        ),
        "tenant_config_dir_configured": config_context["tenant_config_dir_configured"],
        "schema_config_source": config_context["schema_config_source"],
        "federation_config_source": config_context["federation_config_source"],
    }


def build_startup_diagnostics(
    *,
    config: AppConfig,
    graph_storage: GraphStorage,
    federation_summary: Dict[str, Any],
    federation_manager: FederationManager,
    agent_registry: AgentRegistry,
) -> Dict[str, Any]:
    runtime_info = config_loader.get_runtime_info()
    config_context = config_loader.get_config_context()
    tenant_context = config_loader.get_tenant_context()
    request_actor = config_loader.get_request_actor_info()
    request_scope = config_loader.get_request_scope_info()
    request_selection = config_loader.get_request_graph_selection_info()
    capabilities = config_loader.get_capabilities()
    capability_summary = count_enabled_capabilities(capabilities)
    graph_integrity = collect_graph_integrity_diagnostics(graph_storage)
    federation_runtime = federation_manager.get_status()
    agent_status = agent_registry.get_all_status()
    public_tenant_context = build_public_tenant_context(tenant_context)
    public_config_context = build_public_config_context(config_context)

    llm_availability = get_llm_availability()

    checks = {
        "config": {
            "status": "ok",
            "schema_config_source": config_context["schema_config_source"],
            "federation_config_source": config_context["federation_config_source"],
            "tenant_config_dir_configured": config_context[
                "tenant_config_dir_configured"
            ],
        },
        "graph_storage": {
            "status": graph_integrity["status"],
            "graph_nodes": len(graph_storage.nodes),
            "graph_edges": len(graph_storage.edges),
            "integrity": graph_integrity,
        },
        "event_delivery": {
            "status": "ok"
            if getattr(graph_storage, "_events_enabled", False)
            else "disabled",
        },
        "llm": {
            "status": "ok" if llm_availability["available"] else "no_key",
            "available": llm_availability["available"],
            "provider": llm_availability["provider"],
        },
        "agents": {
            "status": "ok" if agent_status["enabled"] else "disabled",
            "enabled": agent_status["enabled"],
            "worker_count": agent_status["worker_count"],
            "subscription_count": agent_status["subscription_count"],
            "configured_integrations": len(agent_status.get("mcp_integrations", [])),
        },
        "federation": {
            "status": federation_runtime.get(
                "status", "disabled" if not federation_summary.get("enabled") else "ok"
            ),
            "enabled": federation_summary.get("enabled", False),
            "configured_graphs": federation_summary.get("configured_graphs", 0),
            "active_graphs": federation_summary.get("active_graphs", 0),
        },
    }

    blocking_failures = [
        name
        for name, check in checks.items()
        if name in {"config", "graph_storage"}
        and check.get("status") not in {"ok", "disabled"}
    ]
    readiness_status = "ready" if not blocking_failures else "not_ready"

    diagnostics = {
        "status": readiness_status,
        "config_profile": config.config_profile,
        "runtime": runtime_info,
        "tenant_context": public_tenant_context,
        "config_context": public_config_context,
        "request_context_defaults": {
            "actor": request_actor,
            "scope": request_scope,
            "selection": request_selection,
        },
        "capabilities": capability_summary,
        "checks": checks,
        "warnings": [],
    }

    if graph_integrity["status"] != "ok":
        diagnostics["warnings"].append("graph_integrity_degraded")
    federation_check_status = checks["federation"]["status"]
    if federation_check_status not in {"ok", "disabled", "healthy"}:
        diagnostics["warnings"].append("federation_runtime_degraded")

    return diagnostics


def emit_startup_diagnostics_log(diagnostics: Dict[str, Any]) -> None:
    logger.info(
        "startup_diagnostics %s",
        json.dumps(diagnostics, sort_keys=True),
    )
