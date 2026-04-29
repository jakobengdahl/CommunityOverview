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
            "actor_id": "",
            "actor_type": "",
            "is_authenticated": False,
            "auth_source": "anonymous",
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
            "workspace_id": "",
            "workspace_kind": "",
            "graph_id": "",
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
            "workspace_id": "override-workspace",
            "workspace_kind": "sandbox",
            "graph_id": "override-graph",
            "source": "override",
        }
