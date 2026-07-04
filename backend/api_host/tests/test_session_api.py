"""
Tests for the shared-session REST endpoints under /api/sessions (steps 1 & 3).

Covers the CRUD surface, node-ref resolution (?resolve=true), the ops endpoint
with its conflict/error semantics, and id validation. The realtime SSE stream is
exercised at the unit level in test_session_manager.py; here we assert the HTTP
contract of the request/response endpoints.
"""

from fastapi.testclient import TestClient


class TestSessionCrud:
    def test_create_and_get(self, test_app: TestClient):
        resp = test_app.post("/api/sessions", json={"name": "My session"})
        assert resp.status_code == 200
        body = resp.json()
        sid = body["id"]
        assert body["name"] == "My session"
        assert body["state"]["node_refs"] == []
        assert body["roster"] == []

        got = test_app.get(f"/api/sessions/{sid}")
        assert got.status_code == 200
        assert got.json()["id"] == sid

    def test_list_sessions(self, test_app: TestClient):
        a = test_app.post("/api/sessions", json={"name": "a"}).json()["id"]
        b = test_app.post("/api/sessions", json={"name": "b"}).json()["id"]
        listed = test_app.get("/api/sessions").json()["sessions"]
        ids = {s["id"] for s in listed}
        assert {a, b} <= ids

    def test_rename(self, test_app: TestClient):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        resp = test_app.patch(f"/api/sessions/{sid}", json={"name": "renamed"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "renamed"

    def test_delete(self, test_app: TestClient):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        resp = test_app.delete(f"/api/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert test_app.get(f"/api/sessions/{sid}").status_code == 404

    def test_get_unknown_returns_404(self, test_app: TestClient):
        assert test_app.get("/api/sessions/9999-9999").status_code == 404

    def test_invalid_id_returns_400(self, test_app: TestClient):
        assert test_app.get("/api/sessions/not-valid").status_code == 400


class TestSessionOps:
    def test_apply_ops_updates_state(self, test_app: TestClient):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        resp = test_app.post(
            f"/api/sessions/{sid}/ops",
            json={
                "client_id": "c1",
                "base_seq": 0,
                "ops": [
                    {"op": "nodes_added", "node_ids": ["node-1", "node-2"]},
                    {"op": "node_moved", "node_id": "node-1", "position": {"x": 5, "y": 6}},
                ],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["seq"] == 2

        state = test_app.get(f"/api/sessions/{sid}").json()["state"]
        assert state["node_refs"] == ["node-1", "node-2"]
        assert state["positions"]["node-1"] == {"x": 5.0, "y": 6.0}

    def test_ops_unknown_session_404(self, test_app: TestClient):
        resp = test_app.post(
            "/api/sessions/9999-9999/ops",
            json={"client_id": "c1", "ops": [{"op": "nodes_added", "node_ids": ["a"]}]},
        )
        assert resp.status_code == 404

    def test_ops_invalid_op_400(self, test_app: TestClient):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        resp = test_app.post(
            f"/api/sessions/{sid}/ops",
            json={"client_id": "c1", "ops": [{"op": "nope"}]},
        )
        assert resp.status_code == 400

    def test_ops_bad_position_400(self, test_app: TestClient):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        resp = test_app.post(
            f"/api/sessions/{sid}/ops",
            json={"client_id": "c1", "ops": [{"op": "node_moved", "node_id": "a", "position": {"x": "no"}}]},
        )
        assert resp.status_code == 400


class TestResolve:
    def test_resolve_true_returns_nodes(self, test_app: TestClient):
        """?resolve=true rehydrates node references against the graph."""
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        # node-1 exists in the sample graph fixture
        test_app.post(
            f"/api/sessions/{sid}/ops",
            json={"client_id": "c1", "ops": [{"op": "nodes_added", "node_ids": ["node-1", "does-not-exist"]}]},
        )
        resp = test_app.get(f"/api/sessions/{sid}?resolve=true")
        assert resp.status_code == 200
        resolved = resp.json()["resolved"]
        ids = {n["id"] for n in resolved["nodes"]}
        assert "node-1" in ids
        assert "does-not-exist" not in ids
