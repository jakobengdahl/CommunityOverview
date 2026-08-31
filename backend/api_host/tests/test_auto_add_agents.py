"""Tests for the session-scoped auto-add-agent REST endpoints.

    POST   /sessions/{id}/auto-add-agents
    GET    /sessions/{id}/auto-add-agents
    DELETE /sessions/{id}/auto-add-agents/{agent_id}

Creation/removal go through the graph authorization/mutate seam (permissive in
open core); listing is a read. The matching/isolation behaviour is covered in
``backend/core/tests/test_session_auto_add.py`` — here we lock in the HTTP
contract (validation, shape, lifecycle).
"""

import logging

from fastapi.testclient import TestClient

SESSION = "1000-2000"

# The suffix that only the *raw* AutoAddRuleError message carries — the client
# must never see it, but the server log must.
_RAW_ONLY_DETAIL = "a rule with neither would add every created node to the view"


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
