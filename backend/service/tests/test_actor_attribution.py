"""Targeted tests for actor attribution in mutation results and events."""

from backend.authorization import use_request_authorization


class TestMutationAttribution:
    def test_standalone_mutation_defaults_to_no_attribution(self, empty_service: "GraphService"):
        captured_events = []
        empty_service.storage.add_system_listener(captured_events.append)

        result = empty_service.add_nodes(
            nodes=[{"id": "actor-standalone", "type": "Actor", "name": "Standalone Actor"}],
            edges=[],
            event_origin="web-ui",
        )

        assert result["success"] is True
        assert "attribution" not in result
        assert len(captured_events) == 1
        assert captured_events[0].origin.event_origin == "web-ui"
        assert captured_events[0].origin.attribution is None

    def test_request_bound_mutation_includes_actor_and_scope_attribution(self, empty_service: "GraphService"):
        captured_events = []
        empty_service.storage.add_system_listener(captured_events.append)

        with use_request_authorization(headers={
            "x-communityoverview-actor-id": "member-123",
            "x-communityoverview-actor-type": "member",
            "x-communityoverview-auth-source": "gateway",
            "x-communityoverview-workspace-id": "workspace-7",
            "x-communityoverview-workspace-kind": "team",
            "x-communityoverview-graph-id": "graph-42",
        }):
            result = empty_service.add_nodes(
                nodes=[{"id": "actor-request", "type": "Actor", "name": "Request Actor"}],
                edges=[],
                event_origin="web-ui",
            )

        assert result["success"] is True
        assert result["attribution"] == {
            "actor": {
                "actor_id": "member-123",
                "actor_type": "member",
                "is_authenticated": True,
                "auth_source": "gateway",
                "source": "request",
            },
            "scope": {
                "workspace_id": "workspace-7",
                "workspace_kind": "team",
                "graph_id": "graph-42",
                "source": "request",
            },
        }

        assert len(captured_events) == 1
        event = captured_events[0]
        assert event.origin.event_origin == "web-ui"
        assert event.origin.attribution is not None
        assert event.origin.attribution.to_dict() == result["attribution"]
