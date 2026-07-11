"""Targeted tests for public request actor/scope context seams."""

import pytest


class TestRequestActorService:
    def test_defaults_are_standalone_safe(self, empty_service, monkeypatch):
        for var in (
            "COMMUNITYOVERVIEW_ACTOR_ID",
            "COMMUNITYOVERVIEW_ACTOR_TYPE",
            "COMMUNITYOVERVIEW_AUTH_SOURCE",
        ):
            monkeypatch.delenv(var, raising=False)

        assert empty_service.get_request_actor_info() == {
            "actor_type": "",
            "is_authenticated": False,
            "auth_source": "anonymous",
            "has_actor": False,
            "source": "default",
        }

    def test_request_headers_override_environment_defaults(self, empty_service, monkeypatch):
        monkeypatch.setenv("COMMUNITYOVERVIEW_ACTOR_ID", "env-actor")
        monkeypatch.setenv("COMMUNITYOVERVIEW_ACTOR_TYPE", "service")
        monkeypatch.setenv("COMMUNITYOVERVIEW_AUTH_SOURCE", "gateway")

        result = empty_service.get_request_actor_info(headers={
            "x-communityoverview-actor-id": "header-actor",
            "x-communityoverview-actor-type": "member",
            "x-communityoverview-auth-source": "proxy",
        })

        assert result == {
            "actor_type": "member",
            "is_authenticated": True,
            "auth_source": "proxy",
            "has_actor": True,
            "source": "request",
        }

    def test_internal_seam_still_resolves_rich_actor_context(self, monkeypatch):
        from backend.runtime.request_context import get_request_actor_context

        monkeypatch.setenv("COMMUNITYOVERVIEW_ACTOR_ID", "env-actor")
        monkeypatch.setenv("COMMUNITYOVERVIEW_ACTOR_TYPE", "service")
        monkeypatch.setenv("COMMUNITYOVERVIEW_AUTH_SOURCE", "gateway")

        result = get_request_actor_context(headers={
            "x-communityoverview-actor-id": "header-actor",
            "x-communityoverview-actor-type": "member",
            "x-communityoverview-auth-source": "proxy",
        })

        assert result == {
            "actor_id": "header-actor",
            "actor_type": "member",
            "is_authenticated": True,
            "auth_source": "proxy",
            "source": "request",
        }


class TestRequestScopeService:
    def test_defaults_are_standalone_safe(self, empty_service, monkeypatch):
        for var in (
            "COMMUNITYOVERVIEW_WORKSPACE_ID",
            "COMMUNITYOVERVIEW_WORKSPACE_KIND",
            "COMMUNITYOVERVIEW_GRAPH_SCOPE_ID",
        ):
            monkeypatch.delenv(var, raising=False)

        assert empty_service.get_request_scope_info() == {
            "workspace_kind": "",
            "has_workspace": False,
            "has_graph": False,
            "has_selection": False,
            "selection_mode": "default",
            "selection_source": "default",
            "source": "default",
        }

    def test_explicit_override_wins_over_headers_and_environment(self, empty_service, monkeypatch):
        monkeypatch.setenv("COMMUNITYOVERVIEW_WORKSPACE_ID", "env-workspace")
        monkeypatch.setenv("COMMUNITYOVERVIEW_WORKSPACE_KIND", "team")
        monkeypatch.setenv("COMMUNITYOVERVIEW_GRAPH_SCOPE_ID", "env-graph")

        result = empty_service.get_request_scope_info(
            headers={
                "x-communityoverview-workspace-id": "header-workspace",
                "x-communityoverview-workspace-kind": "personal",
                "x-communityoverview-graph-id": "header-graph",
            },
            workspace_id="override-workspace",
            workspace_kind="sandbox",
            graph_id="override-graph",
        )

        assert result == {
            "workspace_kind": "sandbox",
            "has_workspace": True,
            "has_graph": True,
            "has_selection": True,
            "selection_mode": "workspace_graph",
            "selection_source": "override",
            "source": "override",
        }

    def test_internal_seam_still_resolves_rich_scope_context(self, monkeypatch):
        from backend.runtime.request_context import get_request_scope_context

        monkeypatch.setenv("COMMUNITYOVERVIEW_WORKSPACE_ID", "env-workspace")
        monkeypatch.setenv("COMMUNITYOVERVIEW_WORKSPACE_KIND", "team")
        monkeypatch.setenv("COMMUNITYOVERVIEW_GRAPH_SCOPE_ID", "env-graph")

        result = get_request_scope_context(
            headers={
                "x-communityoverview-workspace-id": "header-workspace",
                "x-communityoverview-workspace-kind": "personal",
                "x-communityoverview-graph-id": "header-graph",
            },
            workspace_id="override-workspace",
            workspace_kind="sandbox",
            graph_id="override-graph",
        )

        assert result == {
            "workspace_id": "override-workspace",
            "workspace_kind": "sandbox",
            "graph_id": "override-graph",
            "source": "override",
            "selection_source": "override",
            "selection_mode": "workspace_graph",
            "has_workspace": True,
            "has_graph": True,
            "has_selection": True,
        }

    def test_graph_selection_seam_remains_non_sensitive_and_tracks_mode(self, monkeypatch):
        from backend.runtime.request_context import get_public_request_graph_selection_context

        monkeypatch.setenv("COMMUNITYOVERVIEW_WORKSPACE_KIND", "team")

        result = get_public_request_graph_selection_context(graph_id="graph-only")

        assert result == {
            "workspace_kind": "team",
            "has_workspace": False,
            "has_graph": True,
            "has_selection": True,
            "selection_mode": "graph",
            "selection_source": "override",
            "source": "override",
        }
        assert "workspace_id" not in result
        assert "graph_id" not in result
