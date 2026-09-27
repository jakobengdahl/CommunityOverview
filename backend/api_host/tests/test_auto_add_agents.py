"""Tests for the session-scoped auto-add-agent REST endpoints.

    POST   /sessions/{id}/auto-add-agents
    GET    /sessions/{id}/auto-add-agents
    DELETE /sessions/{id}/auto-add-agents/{agent_id}

Creation/removal go through the graph authorization/mutate seam and listing
through the read seam (permissive in open core). The matching/isolation behaviour is covered in
``backend/core/tests/test_session_auto_add.py`` — here we lock in the HTTP
contract (validation, shape, lifecycle).
"""

import logging

import pytest
from fastapi.testclient import TestClient

from backend.runtime.authorization import (
    AUTHORIZATION_MODE_ENV,
    GRAPH_ACTION_MUTATE,
    GRAPH_ACTION_READ,
    DefaultGraphAuthorizationHook,
    GraphAuthorizationDecision,
)
from backend.runtime.request_context import ACTOR_ID_HEADER

SESSION = "1000-2000"
# A second valid id, in the long form, so a denial keyed on the fixture id fails.
OTHER_SESSION = "1111-2222-3333-4444"
_SESSIONS = pytest.mark.parametrize("session_id", [SESSION, OTHER_SESSION])

# The suffix that only the *raw* AutoAddRuleError message carries — the client
# must never see it, but the server log must.
_RAW_ONLY_DETAIL = "a rule with neither would add every created node to the view"

_TARGET = "session_auto_add_agent"


def _denied_body(action: str, mode: str, reason: str) -> dict:
    return {
        "success": False,
        "error": "Graph access denied",
        "message": reason,
        "error_code": "access_denied",
        "authorization": {
            "action": action,
            "target": _TARGET,
            "mode": mode,
            "source": "environment",
        },
    }


_DENY_ALL_REASON = "Graph access is disabled by the current authorization mode."
_READ_ONLY_REASON = "Graph mutations are disabled by the current authorization mode."


class RecordingHook(DefaultGraphAuthorizationHook):
    """The default env-driven hook, recording every (action, target) it is asked."""

    def __init__(self):
        self.seen = []

    def evaluate(self, context):
        self.seen.append((context.action, context.target))
        return super().evaluate(context)


def _install_recording_hook(test_app: TestClient) -> RecordingHook:
    hook = RecordingHook()
    test_app.app.state.graph_service._authorization_hook = hook
    return hook


class ActorGateHook:
    """Allows only the actor named in the request header, recording each actor seen.

    Env-mode hooks ignore headers, so only a hook that reads the context actor
    can tell whether the endpoint bound the request headers before asking.
    """

    def __init__(self, allowed_actor_id: str):
        self.allowed_actor_id = allowed_actor_id
        self.actors = []

    def evaluate(self, context):
        self.actors.append(context.actor)
        if (
            context.actor["actor_id"] == self.allowed_actor_id
            and context.actor["source"] == "request"
        ):
            return GraphAuthorizationDecision(allowed=True, source="test-actor")
        return GraphAuthorizationDecision(
            allowed=False, reason=_ACTOR_REASON, mode="actor", source="test-actor"
        )


_ACTOR = "member-123"
_ACTOR_REASON = "Actor is not allowed."


def _create_agent(test_app: TestClient, session_id: str = SESSION, **kwargs) -> str:
    created = test_app.post(
        f"/sessions/{session_id}/auto-add-agents", json={"keywords": ["ai"]}, **kwargs
    )
    assert created.status_code == 200, created.text
    return created.json()["agent"]["agent_id"]


class TestCreate:
    def test_create_returns_agent_and_registers_it(self, test_app: TestClient):
        resp = test_app.post(
            f"/sessions/{SESSION}/auto-add-agents", json={"node_types": ["Actor"]}
        )
        assert resp.status_code == 200, resp.text
        agent = resp.json()["agent"]
        assert agent["session_id"] == SESSION
        assert agent["node_types"] == ["Actor"]
        # Registered in the app's registry and the push session materialised.
        registry = test_app.app.state.auto_add_registry
        assert len(registry.list_rules(SESSION)) == 1
        assert test_app.app.state.session_registry.session_exists(SESSION)

    def test_invalid_session_id_rejected(self, test_app: TestClient):
        resp = test_app.post(
            "/sessions/not-valid/auto-add-agents", json={"node_types": ["Actor"]}
        )
        assert resp.status_code == 400

    def test_empty_pattern_rejected(self, test_app: TestClient):
        resp = test_app.post(f"/sessions/{SESSION}/auto-add-agents", json={})
        assert resp.status_code == 400
        assert "at least one" in resp.json()["error"]

    def test_rejection_does_not_leak_raw_exception_text(self, test_app: TestClient):
        """The 400 body carries only the sanitized message + a stable code.

        CodeQL alert 35: the raw AutoAddRuleError text must not reach an external
        caller. The response exposes the fixed message and stable code, plus an
        opaque correlation id — never the exception's own detail string.
        """
        resp = test_app.post(f"/sessions/{SESSION}/auto-add-agents", json={})
        assert resp.status_code == 400
        body = resp.json()
        # Sanitized, stable client contract.
        assert body["code"] == "empty_pattern"
        assert body["correlation_id"]
        # Raw exception detail must not appear anywhere in the serialized body.
        assert _RAW_ONLY_DETAIL not in resp.text

    def test_rejection_logs_detail_with_correlation_id(
        self, test_app: TestClient, caplog
    ):
        """The full exception detail survives — but only in a server log line,
        tagged with the same correlation id returned to the client."""
        with caplog.at_level(logging.WARNING, logger="backend.api_host.session_stream"):
            resp = test_app.post(f"/sessions/{SESSION}/auto-add-agents", json={})
        assert resp.status_code == 400
        correlation_id = resp.json()["correlation_id"]

        matching = [
            r
            for r in caplog.records
            if r.name == "backend.api_host.session_stream"
            and correlation_id in r.getMessage()
        ]
        assert matching, "expected a server log line carrying the correlation id"
        logged = matching[0].getMessage()
        # The log — and only the log — retains the raw diagnostic detail.
        assert _RAW_ONLY_DETAIL in logged
        assert "empty_pattern" in logged


class TestListAndDelete:
    def test_list_then_delete(self, test_app: TestClient):
        created = test_app.post(
            f"/sessions/{SESSION}/auto-add-agents", json={"keywords": ["ai"]}
        )
        agent_id = created.json()["agent"]["agent_id"]

        listed = test_app.get(f"/sessions/{SESSION}/auto-add-agents")
        assert listed.status_code == 200
        assert [a["agent_id"] for a in listed.json()["agents"]] == [agent_id]

        deleted = test_app.delete(f"/sessions/{SESSION}/auto-add-agents/{agent_id}")
        assert deleted.status_code == 200
        assert (
            test_app.get(f"/sessions/{SESSION}/auto-add-agents").json()["agents"] == []
        )

    def test_delete_unknown_returns_404(self, test_app: TestClient):
        resp = test_app.delete(f"/sessions/{SESSION}/auto-add-agents/nope")
        assert resp.status_code == 404

    def test_list_empty_session(self, test_app: TestClient):
        resp = test_app.get(f"/sessions/{SESSION}/auto-add-agents")
        assert resp.status_code == 200
        assert resp.json()["agents"] == []


class TestListAuthorization:
    @_SESSIONS
    def test_deny_all_blocks_list(self, test_app: TestClient, monkeypatch, session_id):
        _create_agent(test_app, session_id)
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")

        resp = test_app.get(f"/sessions/{session_id}/auto-add-agents")

        assert resp.status_code == 403
        assert resp.json() == _denied_body(
            GRAPH_ACTION_READ, "deny-all", _DENY_ALL_REASON
        )

    @_SESSIONS
    def test_read_only_mode_still_lists(
        self, test_app: TestClient, monkeypatch, session_id
    ):
        agent_id = _create_agent(test_app, session_id)
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        resp = test_app.get(f"/sessions/{session_id}/auto-add-agents")

        assert resp.status_code == 200
        assert [a["agent_id"] for a in resp.json()["agents"]] == [agent_id]

    @_SESSIONS
    def test_deny_all_blocks_list_on_an_empty_session(
        self, test_app: TestClient, monkeypatch, session_id
    ):
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")

        resp = test_app.get(f"/sessions/{session_id}/auto-add-agents")

        assert resp.status_code == 403
        assert resp.json() == _denied_body(
            GRAPH_ACTION_READ, "deny-all", _DENY_ALL_REASON
        )


class TestGateActionAndTarget:
    def test_list_asks_the_gate_once_as_a_read(self, test_app: TestClient):
        hook = _install_recording_hook(test_app)

        resp = test_app.get(f"/sessions/{SESSION}/auto-add-agents")

        assert resp.status_code == 200
        assert hook.seen == [(GRAPH_ACTION_READ, _TARGET)]

    def test_create_asks_the_gate_once_as_a_mutation(self, test_app: TestClient):
        hook = _install_recording_hook(test_app)

        resp = test_app.post(
            f"/sessions/{SESSION}/auto-add-agents", json={"keywords": ["ai"]}
        )

        assert resp.status_code == 200, resp.text
        assert hook.seen == [(GRAPH_ACTION_MUTATE, _TARGET)]

    def test_delete_asks_the_gate_once_as_a_mutation(self, test_app: TestClient):
        agent_id = _create_agent(test_app)
        hook = _install_recording_hook(test_app)

        resp = test_app.delete(f"/sessions/{SESSION}/auto-add-agents/{agent_id}")

        assert resp.status_code == 200
        assert hook.seen == [(GRAPH_ACTION_MUTATE, _TARGET)]


class TestMalformedIdBeforeGate:
    @pytest.mark.parametrize(
        "method,path,kwargs",
        [
            (
                "post",
                "/sessions/not-valid/auto-add-agents",
                {"json": {"keywords": ["ai"]}},
            ),
            ("get", "/sessions/not-valid/auto-add-agents", {}),
            ("delete", "/sessions/not-valid/auto-add-agents/some-agent", {}),
        ],
        ids=["create", "list", "delete"],
    )
    def test_malformed_id_is_a_400_even_when_denied(
        self, test_app: TestClient, monkeypatch, method, path, kwargs
    ):
        hook = _install_recording_hook(test_app)
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")

        resp = getattr(test_app, method)(path, **kwargs)

        assert resp.status_code == 400
        assert resp.json() == {"error": "invalid session_id format"}
        assert hook.seen == []


class TestMutationAuthorization:
    @_SESSIONS
    def test_read_only_mode_blocks_create(
        self, test_app: TestClient, monkeypatch, session_id
    ):
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        resp = test_app.post(
            f"/sessions/{session_id}/auto-add-agents", json={"keywords": ["ai"]}
        )

        assert resp.status_code == 403
        assert resp.json() == _denied_body(
            GRAPH_ACTION_MUTATE, "read-only", _READ_ONLY_REASON
        )
        assert test_app.app.state.auto_add_registry.list_rules(session_id) == []

    @_SESSIONS
    def test_read_only_mode_blocks_delete_and_keeps_the_agent(
        self, test_app: TestClient, monkeypatch, session_id
    ):
        agent_id = _create_agent(test_app, session_id)
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        resp = test_app.delete(f"/sessions/{session_id}/auto-add-agents/{agent_id}")

        assert resp.status_code == 403
        assert resp.json() == _denied_body(
            GRAPH_ACTION_MUTATE, "read-only", _READ_ONLY_REASON
        )
        listed = test_app.get(f"/sessions/{session_id}/auto-add-agents")
        assert [a["agent_id"] for a in listed.json()["agents"]] == [agent_id]


def _actor_denied_body(action: str) -> dict:
    body = _denied_body(action, "actor", _ACTOR_REASON)
    body["authorization"]["source"] = "test-actor"
    return body


class TestGateSeesTheRequestActor:
    """The gate is asked with the actor carried by the request's own headers."""

    @pytest.fixture
    def hook(self, test_app: TestClient) -> ActorGateHook:
        hook = ActorGateHook(_ACTOR)
        test_app.app.state.graph_service._authorization_hook = hook
        return hook

    def test_create_is_allowed_for_the_header_actor(self, test_app, hook):
        _create_agent(test_app, headers={ACTOR_ID_HEADER: _ACTOR})

        assert [a["actor_id"] for a in hook.actors] == [_ACTOR]

    def test_list_is_allowed_for_the_header_actor(self, test_app, hook):
        resp = test_app.get(
            f"/sessions/{SESSION}/auto-add-agents", headers={ACTOR_ID_HEADER: _ACTOR}
        )

        assert resp.status_code == 200
        assert [a["actor_id"] for a in hook.actors] == [_ACTOR]

    def test_delete_is_allowed_for_the_header_actor(self, test_app, hook):
        agent_id = _create_agent(test_app, headers={ACTOR_ID_HEADER: _ACTOR})

        resp = test_app.delete(
            f"/sessions/{SESSION}/auto-add-agents/{agent_id}",
            headers={ACTOR_ID_HEADER: _ACTOR},
        )

        assert resp.status_code == 200
        assert [a["actor_id"] for a in hook.actors] == [_ACTOR, _ACTOR]
        assert test_app.app.state.auto_add_registry.list_rules(SESSION) == []

    @pytest.mark.parametrize(
        "headers", [{}, {ACTOR_ID_HEADER: "someone-else"}], ids=["none", "other"]
    )
    def test_create_is_denied_for_any_other_actor(self, test_app, hook, headers):
        resp = test_app.post(
            f"/sessions/{SESSION}/auto-add-agents",
            json={"keywords": ["ai"]},
            headers=headers,
        )

        assert resp.status_code == 403
        assert resp.json() == _actor_denied_body(GRAPH_ACTION_MUTATE)
        assert test_app.app.state.auto_add_registry.list_rules(SESSION) == []
