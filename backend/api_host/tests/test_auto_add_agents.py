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
)

SESSION = "1000-2000"

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


def _create_agent(test_app: TestClient) -> str:
    created = test_app.post(
        f"/sessions/{SESSION}/auto-add-agents", json={"keywords": ["ai"]}
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
    def test_deny_all_blocks_list(self, test_app: TestClient, monkeypatch):
        test_app.post(f"/sessions/{SESSION}/auto-add-agents", json={"keywords": ["ai"]})
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")

        resp = test_app.get(f"/sessions/{SESSION}/auto-add-agents")

        assert resp.status_code == 403
        assert resp.json() == _denied_body(
            GRAPH_ACTION_READ, "deny-all", _DENY_ALL_REASON
        )

    def test_read_only_mode_still_lists(self, test_app: TestClient, monkeypatch):
        created = test_app.post(
            f"/sessions/{SESSION}/auto-add-agents", json={"keywords": ["ai"]}
        )
        agent_id = created.json()["agent"]["agent_id"]
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        resp = test_app.get(f"/sessions/{SESSION}/auto-add-agents")

        assert resp.status_code == 200
        assert [a["agent_id"] for a in resp.json()["agents"]] == [agent_id]

    def test_deny_all_blocks_list_on_an_empty_session(
        self, test_app: TestClient, monkeypatch
    ):
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")

        resp = test_app.get(f"/sessions/{SESSION}/auto-add-agents")

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
    def test_read_only_mode_blocks_create(self, test_app: TestClient, monkeypatch):
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        resp = test_app.post(
            f"/sessions/{SESSION}/auto-add-agents", json={"keywords": ["ai"]}
        )

        assert resp.status_code == 403
        assert resp.json() == _denied_body(
            GRAPH_ACTION_MUTATE, "read-only", _READ_ONLY_REASON
        )
        assert test_app.app.state.auto_add_registry.list_rules(SESSION) == []

    def test_read_only_mode_blocks_delete_and_keeps_the_agent(
        self, test_app: TestClient, monkeypatch
    ):
        agent_id = _create_agent(test_app)
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        resp = test_app.delete(f"/sessions/{SESSION}/auto-add-agents/{agent_id}")

        assert resp.status_code == 403
        assert resp.json() == _denied_body(
            GRAPH_ACTION_MUTATE, "read-only", _READ_ONLY_REASON
        )
        listed = test_app.get(f"/sessions/{SESSION}/auto-add-agents")
        assert [a["agent_id"] for a in listed.json()["agents"]] == [agent_id]
