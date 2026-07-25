"""
Unit tests for app_host using FastAPI TestClient.

Tests that the FastAPI application is properly configured and
all REST API endpoints function correctly.
"""

import os
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from backend.api_host import create_app
from backend.core import GraphStorage


class TestHealthAndRoot:
    """Tests for health check and root endpoints."""

    def test_health_check(self, test_app: TestClient):
        """Health endpoint returns liveness status."""
        response = test_app.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["kind"] == "liveness"
        assert data["readiness_endpoint"] == "/ready"
        assert "graph_nodes" in data
        assert "graph_edges" in data

    def test_root_endpoint_redirects(self, test_app: TestClient):
        """Root endpoint redirects to /web/."""
        response = test_app.get("/", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/web/"

    def test_info_endpoint(self, test_app: TestClient):
        """Info endpoint returns API information."""
        response = test_app.get("/info")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Community Knowledge Graph"
        assert "endpoints" in data
        assert "graph_stats" in data
        assert data["endpoints"]["api"] == "/api"
        assert data["endpoints"]["mcp"] == "/mcp"
        assert data["endpoints"]["ready"] == "/ready"
        assert data["endpoints"]["startup_diagnostics"] == "/diagnostics/startup"
        assert data["operability"]["startup_status"] == "ready"
        assert data["operability"]["capabilities"] == {
            "configured": 0,
            "enabled": 0,
            "disabled": 0,
        }
        # llm_available is always present
        assert "llm_available" in data
        assert isinstance(data["llm_available"], bool)

    def test_info_endpoint_llm_available_with_key(self, app_config):
        """Info endpoint reports llm_available=True when API key is set."""
        with patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", "LLM_PROVIDER": "claude"}
        ):
            from backend.api_host import create_app

            with patch("backend.ui.chat_logic.create_provider"):
                app = create_app(app_config)
            client = TestClient(app)
            response = client.get("/info")
            assert response.status_code == 200
            assert response.json()["llm_available"] is True

    def test_info_endpoint_llm_not_available_without_key(self, app_config):
        """Info endpoint reports llm_available=False when no API key is configured."""
        env_patch = {"LLM_PROVIDER": "claude"}
        with patch.dict(os.environ, env_patch):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            from backend.api_host import create_app

            app = create_app(app_config)
            client = TestClient(app)
            response = client.get("/info")
            assert response.status_code == 200
            assert response.json()["llm_available"] is False

    def test_health_shows_graph_stats(self, test_app: TestClient):
        """Health endpoint shows correct graph statistics."""
        response = test_app.get("/health")
        data = response.json()
        assert data["graph_nodes"] == 3
        assert data["graph_edges"] == 2

    def test_readiness_endpoint_exposes_structured_checks(self, test_app: TestClient):
        """Readiness endpoint separates startup checks from liveness."""
        response = test_app.get("/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["kind"] == "readiness"
        assert data["startup_diagnostics_endpoint"] == "/diagnostics/startup"
        assert data["checks"]["config"]["status"] == "ok"
        assert data["checks"]["graph_storage"]["status"] == "ok"
        assert data["checks"]["graph_storage"]["graph_nodes"] == 3
        assert data["checks"]["graph_storage"]["integrity"] == {
            "status": "ok",
            "node_count": 3,
            "edge_count": 2,
            "dangling_edge_count": 0,
            "self_referencing_edge_count": 0,
        }
        assert data["checks"]["event_delivery"]["status"] == "ok"
        assert data["warnings"] == []

    def test_startup_diagnostics_endpoint_is_safe_and_structured(
        self, test_app: TestClient
    ):
        """Startup diagnostics expose safe summaries without filesystem path leakage."""
        response = test_app.get("/diagnostics/startup")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["runtime"] == {
            "runtime_mode": "standalone",
            "enabled_extensions": [],
        }
        assert data["tenant_context"] == {
            "environment": "local",
            "tenant_id_configured": False,
            "tenant_name_configured": False,
            "tenant_context_configured": False,
        }
        assert data["config_context"] == {
            "environment": "local",
            "tenant_context_configured": False,
            "tenant_config_dir_configured": False,
            "schema_config_source": "default",
            "federation_config_source": "default",
        }
        assert data["request_context_defaults"] == {
            "actor": {
                "actor_type": "",
                "is_authenticated": False,
                "auth_source": "anonymous",
                "has_actor": False,
                "source": "default",
            },
            "scope": {
                "workspace_kind": "",
                "has_workspace": False,
                "has_graph": False,
                "has_selection": False,
                "selection_mode": "default",
                "selection_source": "default",
                "source": "default",
            },
            "selection": {
                "workspace_kind": "",
                "has_workspace": False,
                "has_graph": False,
                "has_selection": False,
                "selection_mode": "default",
                "selection_source": "default",
                "source": "default",
            },
        }
        assert data["capabilities"] == {
            "configured": 0,
            "enabled": 0,
            "disabled": 0,
        }
        assert "/root/" not in response.text

    def test_create_app_logs_structured_startup_diagnostics(
        self,
        app_config,
        mock_llm_provider,
        caplog,
    ):
        """App startup emits structured diagnostics for operability tooling."""
        with patch(
            "backend.ui.chat_logic.create_provider", return_value=mock_llm_provider
        ):
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
                with caplog.at_level("INFO"):
                    app = create_app(app_config)

        startup_logs = [
            record.message
            for record in caplog.records
            if record.message.startswith("startup_diagnostics ")
        ]
        assert len(startup_logs) == 1
        assert '"status": "ready"' in startup_logs[0]
        assert '"dangling_edge_count": 0' in startup_logs[0]
        assert (
            app.state.startup_diagnostics["checks"]["graph_storage"]["integrity"][
                "status"
            ]
            == "ok"
        )

    def test_public_operability_endpoints_do_not_expose_tenant_identifiers(
        self,
        app_config,
        mock_llm_provider,
        monkeypatch,
    ):
        """Public diagnostics redact raw tenant identifiers into safe booleans."""
        monkeypatch.setenv("COMMUNITYOVERVIEW_TENANT_ID", "tenant-secret-123")
        monkeypatch.setenv("COMMUNITYOVERVIEW_TENANT_NAME", "Highly Sensitive Tenant")
        monkeypatch.setenv("COMMUNITYOVERVIEW_ENVIRONMENT", "staging")

        with patch(
            "backend.ui.chat_logic.create_provider", return_value=mock_llm_provider
        ):
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
                client = TestClient(create_app(app_config))

        startup_response = client.get("/diagnostics/startup")
        assert startup_response.status_code == 200
        startup_data = startup_response.json()
        assert startup_data["tenant_context"] == {
            "environment": "staging",
            "tenant_id_configured": True,
            "tenant_name_configured": True,
            "tenant_context_configured": True,
        }
        assert startup_data["config_context"] == {
            "environment": "staging",
            "tenant_context_configured": True,
            "tenant_config_dir_configured": False,
            "schema_config_source": "default",
            "federation_config_source": "default",
        }
        assert "tenant_id" not in startup_data["tenant_context"]
        assert "tenant_name" not in startup_data["tenant_context"]
        assert "tenant_id" not in startup_data["config_context"]
        assert "tenant_name" not in startup_data["config_context"]
        assert "tenant-secret-123" not in startup_response.text
        assert "Highly Sensitive Tenant" not in startup_response.text

        info_response = client.get("/info")
        assert info_response.status_code == 200
        info_data = info_response.json()
        assert (
            info_data["operability"]["config_context"] == startup_data["config_context"]
        )
        assert "tenant_id" not in info_data["operability"]["config_context"]
        assert "tenant_name" not in info_data["operability"]["config_context"]
        assert "tenant-secret-123" not in info_response.text
        assert "Highly Sensitive Tenant" not in info_response.text

    def test_readiness_reports_not_ready_when_graph_integrity_is_degraded(
        self,
        app_config,
        mock_llm_provider,
        tmp_path,
    ):
        """Readiness must fail when graph integrity is degraded."""
        graph_path = tmp_path / "degraded-graph.json"
        graph_path.write_text(
            '{"nodes": [{"id": "node-1", "type": "Actor", "name": "Node 1", "communities": []}], '
            '"edges": [{"id": "edge-1", "source": "node-1", "target": "missing-node", "type": "RELATES_TO"}]}'
        )
        degraded_graph_storage = GraphStorage(str(graph_path))

        with patch(
            "backend.ui.chat_logic.create_provider", return_value=mock_llm_provider
        ):
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
                client = TestClient(
                    create_app(app_config, graph_storage=degraded_graph_storage)
                )

        response = client.get("/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "not_ready"
        assert data["checks"]["graph_storage"]["status"] == "degraded"
        assert data["checks"]["graph_storage"]["integrity"]["status"] == "degraded"
        assert data["checks"]["graph_storage"]["integrity"]["dangling_edge_count"] == 1
        assert "graph_integrity_degraded" in data["warnings"]


class TestSearchEndpoints:
    """Tests for search-related endpoints."""

    def test_search_graph_basic(self, test_app: TestClient):
        """Basic search returns results."""
        response = test_app.post("/api/search", json={"query": "test"})
        assert response.status_code == 200
        data = response.json()
        assert "nodes" in data
        assert len(data["nodes"]) > 0

    def test_search_graph_with_type_filter(self, test_app: TestClient):
        """Search with node type filter."""
        response = test_app.post(
            "/api/search", json={"query": "test", "node_types": ["Actor"]}
        )
        assert response.status_code == 200
        data = response.json()
        for node in data["nodes"]:
            assert node["type"] == "Actor"

    def test_search_graph_with_type_and_limit(self, test_app: TestClient):
        """Search with type filter and limit."""
        response = test_app.post(
            "/api/search", json={"query": "test", "node_types": ["Actor"], "limit": 10}
        )
        assert response.status_code == 200
        data = response.json()
        for node in data["nodes"]:
            assert node["type"] == "Actor"

    def test_search_graph_with_limit(self, test_app: TestClient):
        """Search respects limit parameter."""
        response = test_app.post("/api/search", json={"query": "test", "limit": 1})
        assert response.status_code == 200
        data = response.json()
        assert len(data["nodes"]) <= 1

    def test_search_returns_edges(self, test_app: TestClient):
        """Search returns related edges."""
        response = test_app.post("/api/search", json={"query": "test"})
        data = response.json()
        assert "edges" in data


class TestNodeEndpoints:
    """Tests for node CRUD endpoints."""

    def test_get_node_details_success(self, test_app: TestClient):
        """Get node details for existing node."""
        response = test_app.get("/api/nodes/node-1")
        assert response.status_code == 200
        data = response.json()
        assert data["node"]["id"] == "node-1"
        assert data["node"]["name"] == "Test Organization"

    def test_get_node_details_not_found(self, test_app: TestClient):
        """Get node details returns 404 for missing node."""
        response = test_app.get("/api/nodes/nonexistent-node")
        assert response.status_code == 404

    def test_get_related_nodes(self, test_app: TestClient):
        """Get related nodes for a node."""
        response = test_app.post("/api/nodes/node-1/related", json={"depth": 1})
        assert response.status_code == 200
        data = response.json()
        assert "nodes" in data
        assert "edges" in data

    def test_get_related_nodes_with_depth(self, test_app: TestClient):
        """Get related nodes with increased depth."""
        response = test_app.post("/api/nodes/node-1/related", json={"depth": 2})
        assert response.status_code == 200
        data = response.json()
        # Should find more nodes with depth 2
        assert "nodes" in data

    def test_add_nodes(self, test_app_empty_graph: TestClient):
        """Add new nodes to the graph."""
        new_node = {
            "type": "Actor",
            "name": "New Organization",
            "description": "A newly added organization",
            "communities": ["NewCommunity"],
        }
        response = test_app_empty_graph.post("/api/nodes", json={"nodes": [new_node]})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["added_node_ids"]) == 1

    def test_add_nodes_with_edges(self, test_app_empty_graph: TestClient):
        """Add nodes with edges."""
        nodes = [
            {"type": "Actor", "name": "Org A", "communities": []},
            {"type": "Initiative", "name": "Project A", "communities": []},
        ]
        # Note: edges will use generated IDs, so we test without them first
        response = test_app_empty_graph.post("/api/nodes", json={"nodes": nodes})
        assert response.status_code == 200
        data = response.json()
        assert len(data["added_node_ids"]) == 2

    def test_update_node(self, test_app: TestClient):
        """Update an existing node."""
        response = test_app.patch(
            "/api/nodes/node-1",
            json={"updates": {"description": "Updated description"}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

        # Verify update
        get_response = test_app.get("/api/nodes/node-1")
        node_data = get_response.json()
        assert node_data["node"]["description"] == "Updated description"

    def test_update_node_not_found(self, test_app: TestClient):
        """Update non-existent node returns 404."""
        response = test_app.patch(
            "/api/nodes/nonexistent", json={"updates": {"description": "test"}}
        )
        assert response.status_code == 404

    def test_delete_nodes_requires_confirmation(self, test_app: TestClient):
        """Delete without confirmation fails."""
        response = test_app.request(
            "DELETE", "/api/nodes", json={"node_ids": ["node-1"], "confirmed": False}
        )
        assert response.status_code == 400

    def test_delete_nodes_with_confirmation(self, test_app: TestClient):
        """Delete with confirmation succeeds."""
        response = test_app.request(
            "DELETE", "/api/nodes", json={"node_ids": ["node-3"], "confirmed": True}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_delete_edge(self, test_app: TestClient):
        """Delete edge endpoint works."""
        response = test_app.delete("/api/edges/edge-1")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["deleted_edge_id"] == "edge-1"

    def test_delete_edge_propagates_event_metadata(self, test_app: TestClient):
        """Delete edge endpoint propagates request event metadata into emitted events."""
        captured_events = []
        test_app.app.state.graph_storage.add_system_listener(captured_events.append)

        response = test_app.request(
            "DELETE",
            "/api/edges/edge-1",
            json={
                "event_origin": "mcp",
                "event_session_id": "session-123",
                "event_correlation_id": "corr-456",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["deleted_edge_id"] == "edge-1"

        edge_delete_events = [
            event for event in captured_events if event.entity.id == "edge-1"
        ]
        assert len(edge_delete_events) == 1
        event = edge_delete_events[0]
        assert event.origin.event_origin == "mcp"
        assert event.origin.event_session_id == "session-123"
        assert event.origin.event_correlation_id == "corr-456"


class TestSimilarityEndpoints:
    """Tests for similarity search endpoints."""

    def test_find_similar_nodes(self, test_app: TestClient):
        """Find similar nodes by name."""
        response = test_app.post(
            "/api/similar", json={"name": "Test Org", "threshold": 0.5}
        )
        assert response.status_code == 200
        data = response.json()
        assert "similar_nodes" in data

    def test_find_similar_nodes_with_type_filter(self, test_app: TestClient):
        """Find similar nodes filtered by type."""
        response = test_app.post(
            "/api/similar",
            json={"name": "Test", "node_type": "Actor", "threshold": 0.3},
        )
        assert response.status_code == 200
        data = response.json()
        for node in data.get("similar_nodes", []):
            assert node["type"] == "Actor"

    def test_find_similar_nodes_batch(self, test_app: TestClient):
        """Batch similarity search."""
        response = test_app.post(
            "/api/similar/batch",
            json={"names": ["Test Org", "Test Proj"], "threshold": 0.3},
        )
        assert response.status_code == 200
        data = response.json()
        assert "results" in data


class TestStatisticsEndpoints:
    """Tests for statistics and metadata endpoints."""

    def test_get_graph_stats(self, test_app: TestClient):
        """Get graph statistics."""
        response = test_app.get("/api/stats")
        assert response.status_code == 200
        data = response.json()
        assert "total_nodes" in data
        assert "total_edges" in data
        assert data["total_nodes"] == 3
        assert data["total_edges"] == 2

    def test_get_graph_stats_has_type_counts(self, test_app: TestClient):
        """Stats include node counts by type."""
        response = test_app.get("/api/stats")
        assert response.status_code == 200
        data = response.json()
        assert "nodes_by_type" in data
        assert data["nodes_by_type"]["Actor"] == 1

    def test_list_node_types(self, test_app: TestClient):
        """List available node types."""
        response = test_app.get("/api/meta/node-types")
        assert response.status_code == 200
        data = response.json()
        assert "node_types" in data
        type_values = [t["type"] for t in data["node_types"]]
        assert "Actor" in type_values
        assert "Initiative" in type_values

    def test_list_relationship_types(self, test_app: TestClient):
        """List available relationship types."""
        response = test_app.get("/api/meta/relationship-types")
        assert response.status_code == 200
        data = response.json()
        assert "relationship_types" in data
        type_values = [t["type"] for t in data["relationship_types"]]
        assert "IMPLEMENTS" in type_values

    def test_get_capabilities(self, test_app: TestClient):
        """Get capability manifest via REST."""
        test_config_path = str(
            Path(__file__).resolve().parents[3]
            / "config"
            / "test"
            / "schema_config.json"
        )
        os.environ["SCHEMA_FILE"] = test_config_path
        from backend.config import config_loader

        config_loader.reset_loader()

        response = test_app.get("/api/capabilities")
        assert response.status_code == 200
        data = response.json()
        assert data == {
            "capabilities": [
                {
                    "id": "graph_export",
                    "name": "Graph export",
                    "description": "Allows clients to export graph data for offline analysis.",
                    "enabled": True,
                },
                {
                    "id": "assistant_guidance",
                    "name": "Assistant guidance",
                    "description": "Provides configuration for guided assistant interactions.",
                    "enabled": False,
                },
            ]
        }

    def test_get_runtime_info(self, test_app: TestClient):
        """Get runtime metadata via REST."""
        os.environ["COMMUNITYOVERVIEW_RUNTIME_MODE"] = "hosted"
        os.environ["COMMUNITYOVERVIEW_ENABLED_EXTENSIONS"] = "federation,analytics"

        response = test_app.get("/api/runtime")
        assert response.status_code == 200
        data = response.json()
        assert data == {
            "runtime_mode": "hosted",
            "enabled_extensions": ["federation", "analytics"],
        }


class TestExportEndpoints:
    """Tests for export endpoints."""

    def test_export_graph(self, test_app: TestClient):
        """Export entire graph."""
        response = test_app.get("/api/export")
        assert response.status_code == 200
        data = response.json()
        assert "nodes" in data
        assert "edges" in data
        assert len(data["nodes"]) == 3
        assert len(data["edges"]) == 2
        assert data["export_boundary"] == {
            "contract_version": "1.0",
            "export_kind": "full",
            "is_narrowed": False,
            "scope_kind": "standalone",
            "selection_mode": "default",
            "selection_source": "default",
            "has_workspace_selection": False,
            "has_graph_selection": False,
            "graph_scope": {
                "local_graph_included": True,
                "included_graph_count": 0,
            },
            "counts": {
                "nodes": 3,
                "edges": 2,
                "omitted_nodes": 0,
                "omitted_edges": 0,
            },
        }

    def test_export_graph_legacy_endpoint(self, test_app: TestClient):
        """Legacy export_graph endpoint works."""
        response = test_app.get("/export_graph")
        assert response.status_code == 200
        data = response.json()
        assert "nodes" in data
        assert data["export_boundary"]["export_kind"] == "full"
        assert data["export_boundary"]["selection_mode"] == "default"

    def test_export_graph_error_hides_traceback(self, test_app: TestClient):
        """A failing export returns a generic 500 with no traceback leaked."""
        graph_service = test_app.app.state.graph_service
        original = graph_service.export_graph

        def _boom():
            raise RuntimeError("secret internal detail at /srv/app/file.py")

        graph_service.export_graph = _boom
        try:
            response = test_app.get("/export_graph")
        finally:
            graph_service.export_graph = original

        assert response.status_code == 500
        data = response.json()
        assert data["error"] == "internal error"
        assert "request_id" in data
        assert "traceback" not in data
        assert "secret internal detail" not in response.text


class TestExecuteToolEndpoint:
    """Tests for direct tool execution endpoint."""

    def test_execute_tool_search(self, test_app: TestClient):
        """Execute search_graph tool directly."""
        response = test_app.post(
            "/execute_tool",
            json={"tool_name": "search_graph", "arguments": {"query": "test"}},
        )
        assert response.status_code == 200
        data = response.json()
        assert "nodes" in data

    def test_execute_tool_not_found(self, test_app: TestClient):
        """Execute non-safe unknown tool is blocked before tool lookup in unauthenticated mode."""
        response = test_app.post(
            "/execute_tool", json={"tool_name": "nonexistent_tool", "arguments": {}}
        )
        assert response.status_code == 403

    def test_execute_tool_get_runtime_info(self, test_app: TestClient):
        """Execute public runtime introspection tool directly."""
        os.environ["COMMUNITYOVERVIEW_RUNTIME_MODE"] = "hosted"
        os.environ["COMMUNITYOVERVIEW_ENABLED_EXTENSIONS"] = "federation,analytics"

        response = test_app.post(
            "/execute_tool", json={"tool_name": "get_runtime_info", "arguments": {}}
        )
        assert response.status_code == 200
        assert response.json() == {
            "runtime_mode": "hosted",
            "enabled_extensions": ["federation", "analytics"],
        }

    def test_execute_tool_no_name(self, test_app: TestClient):
        """Execute tool without name returns 400."""
        response = test_app.post("/execute_tool", json={"arguments": {}})
        assert response.status_code == 400

    def test_execute_tool_error_hides_traceback(self, test_app: TestClient):
        """A tool that raises returns a generic 500 with no traceback leaked."""
        tools_map = test_app.app.state.tools_map

        def _boom(**_kwargs):
            raise RuntimeError("secret internal detail at /srv/app/file.py")

        original = tools_map.get("get_graph_stats")
        tools_map["get_graph_stats"] = _boom
        try:
            response = test_app.post(
                "/execute_tool", json={"tool_name": "get_graph_stats", "arguments": {}}
            )
        finally:
            tools_map["get_graph_stats"] = original

        assert response.status_code == 500
        data = response.json()
        assert data["error"] == "internal error"
        assert "request_id" in data
        assert "traceback" not in data
        # The exception message must not surface to the client.
        assert "secret internal detail" not in response.text


class TestUiCapabilitiesEndpoint:
    """Tests for the /ui/capabilities endpoint."""

    def test_capabilities_returns_200(self, test_app: TestClient):
        """/ui/capabilities endpoint is reachable and returns JSON."""
        response = test_app.get("/ui/capabilities")
        assert response.status_code == 200

    def test_capabilities_has_required_fields(self, test_app: TestClient):
        """/ui/capabilities always includes llm_available and llm_provider."""
        response = test_app.get("/ui/capabilities")
        data = response.json()
        assert "llm_available" in data
        assert "llm_provider" in data
        assert isinstance(data["llm_available"], bool)
        assert isinstance(data["llm_provider"], str)

    def test_capabilities_llm_available_true_when_key_set(self, app_config):
        """llm_available is True when ANTHROPIC_API_KEY is configured."""
        with patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", "LLM_PROVIDER": "claude"}
        ):
            from backend.api_host import create_app

            with patch("backend.ui.chat_logic.create_provider"):
                app = create_app(app_config)
            client = TestClient(app)
            response = client.get("/ui/capabilities")
            assert response.status_code == 200
            assert response.json()["llm_available"] is True

    def test_capabilities_llm_available_false_when_no_key(self, app_config):
        """llm_available is False when no API key is configured."""
        env_patch = {"LLM_PROVIDER": "claude"}
        with patch.dict(os.environ, env_patch):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            from backend.api_host import create_app

            app = create_app(app_config)
            client = TestClient(app)
            response = client.get("/ui/capabilities")
            assert response.status_code == 200
            assert response.json()["llm_available"] is False

    def test_capabilities_llm_available_true_for_openai(self, app_config):
        """llm_available is True when LLM_PROVIDER=openai and OPENAI_API_KEY is set."""
        env_patch = {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test"}
        with patch.dict(os.environ, env_patch):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            from backend.api_host import create_app

            with patch("backend.ui.chat_logic.create_provider"):
                app = create_app(app_config)
            client = TestClient(app)
            response = client.get("/ui/capabilities")
            assert response.status_code == 200
            data = response.json()
            assert data["llm_available"] is True
            assert data["llm_provider"] == "openai"


class TestStartupDiagnosticsLlmCheck:
    """Tests for LLM availability in startup diagnostics."""

    def test_startup_diagnostics_includes_llm_check(self, test_app: TestClient):
        """Startup diagnostics include an 'llm' check entry."""
        response = test_app.get("/diagnostics/startup")
        assert response.status_code == 200
        data = response.json()
        assert "llm" in data["checks"]
        llm_check = data["checks"]["llm"]
        assert "status" in llm_check
        assert "available" in llm_check
        assert "provider" in llm_check

    def test_startup_diagnostics_llm_status_ok_with_key(self, app_config):
        """LLM check status is 'ok' when a key is configured."""
        with patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", "LLM_PROVIDER": "claude"}
        ):
            from backend.api_host import create_app

            with patch("backend.ui.chat_logic.create_provider"):
                app = create_app(app_config)
            client = TestClient(app)
            response = client.get("/diagnostics/startup")
            llm_check = response.json()["checks"]["llm"]
            assert llm_check["status"] == "ok"
            assert llm_check["available"] is True

    def test_startup_diagnostics_llm_status_no_key_without_key(self, app_config):
        """LLM check status is 'no_key' when no key is configured."""
        env_patch = {"LLM_PROVIDER": "claude"}
        with patch.dict(os.environ, env_patch):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            from backend.api_host import create_app

            app = create_app(app_config)
            client = TestClient(app)
            response = client.get("/diagnostics/startup")
            llm_check = response.json()["checks"]["llm"]
            assert llm_check["status"] == "no_key"
            assert llm_check["available"] is False
