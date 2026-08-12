"""Tests for the session-scoped auto-add-agent REST endpoints.

    POST   /sessions/{id}/auto-add-agents
    GET    /sessions/{id}/auto-add-agents
    DELETE /sessions/{id}/auto-add-agents/{agent_id}

Creation/removal go through the graph authorization/mutate seam (permissive in
open core); listing is a read. The matching/isolation behaviour is covered in
``backend/core/tests/test_session_auto_add.py`` — here we lock in the HTTP
contract (validation, shape, lifecycle).
"""

from fastapi.testclient import TestClient

SESSION = "1000-2000"


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
