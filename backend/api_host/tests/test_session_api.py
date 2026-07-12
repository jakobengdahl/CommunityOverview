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


class TestSessionLookupRateLimit:
    """The auth-bypassed lookup endpoints throttle id-guessing brute force."""

    def _shrink_bucket(self, test_app: TestClient, capacity: float) -> None:
        from backend.core.session_manager import _TokenBucket

        mgr = test_app.app.state.session_manager
        mgr._lookup_bucket = _TokenBucket(capacity, 0.0)

    def test_get_session_returns_429_when_exhausted(self, test_app: TestClient):
        # One token: the first valid-format guess passes (404), the next is 429.
        self._shrink_bucket(test_app, capacity=1)
        assert test_app.get("/api/sessions/9999-9999").status_code == 404
        assert test_app.get("/api/sessions/9999-9998").status_code == 429

    def test_stream_handshake_returns_429_when_exhausted(self, test_app: TestClient):
        # Zero tokens so the handshake is rejected before the SSE generator
        # (which never returns) starts — a valid id that passed would block.
        self._shrink_bucket(test_app, capacity=0)
        resp = test_app.get(
            "/api/sessions/1234-5678/stream", params={"client_id": "c1"}
        )
        assert resp.status_code == 429

    def test_rate_limit_keys_on_real_client_behind_proxy(
        self, temp_graph_file, temp_static_dirs
    ):
        """With a trusted proxy, each real client gets its own budget and a
        client cannot spoof X-Forwarded-For to mint a fresh one."""
        from backend.api_host import create_app, AppConfig
        from backend.core.session_manager import _TokenBucket

        web_path, widget_path = temp_static_dirs
        config = AppConfig(
            graph_file=temp_graph_file,
            web_static_path=web_path,
            widget_static_path=widget_path,
            auth_enabled=False,
            trusted_proxy_hops=1,
        )
        app = create_app(config)
        app.state.session_manager._lookup_bucket = _TokenBucket(1.0, 0.0)
        client = TestClient(app)

        # Real client A (last entry, added by the trusted proxy) — first guess ok.
        assert (
            client.get(
                "/api/sessions/9999-9999", headers={"X-Forwarded-For": "1.1.1.1"}
            ).status_code
            == 404
        )
        # Client B — a different real IP has an independent bucket.
        assert (
            client.get(
                "/api/sessions/9999-9998", headers={"X-Forwarded-For": "2.2.2.2"}
            ).status_code
            == 404
        )
        # Client A again — its bucket is now exhausted.
        assert (
            client.get(
                "/api/sessions/9999-9997", headers={"X-Forwarded-For": "1.1.1.1"}
            ).status_code
            == 429
        )
        # A spoofed leading entry does not change A's key, so still throttled.
        assert (
            client.get(
                "/api/sessions/9999-9996",
                headers={"X-Forwarded-For": "9.9.9.9, 1.1.1.1"},
            ).status_code
            == 429
        )


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
                    {
                        "op": "node_moved",
                        "node_id": "node-1",
                        "position": {"x": 5, "y": 6},
                    },
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
            json={
                "client_id": "c1",
                "ops": [{"op": "node_moved", "node_id": "a", "position": {"x": "no"}}],
            },
        )
        assert resp.status_code == 400


class TestResolve:
    def test_resolve_true_returns_nodes(self, test_app: TestClient):
        """?resolve=true rehydrates node references against the graph."""
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        # node-1 exists in the sample graph fixture
        test_app.post(
            f"/api/sessions/{sid}/ops",
            json={
                "client_id": "c1",
                "ops": [
                    {"op": "nodes_added", "node_ids": ["node-1", "does-not-exist"]}
                ],
            },
        )
        resp = test_app.get(f"/api/sessions/{sid}?resolve=true")
        assert resp.status_code == 200
        resolved = resp.json()["resolved"]
        ids = {n["id"] for n in resolved["nodes"]}
        assert "node-1" in ids
        assert "does-not-exist" not in ids


class TestOpBatchBodyCap:
    def test_oversized_op_batch_returns_413(self, test_app: TestClient):
        """A single op that exceeds the per-batch byte cap is rejected (§3.9)."""
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        # A layout_applied with a huge positions map stays under the op-count cap
        # but well over the 256 KB byte cap.
        positions = {f"node-{i}": {"x": i, "y": i} for i in range(20000)}
        resp = test_app.post(
            f"/api/sessions/{sid}/ops",
            json={
                "client_id": "c1",
                "ops": [{"op": "layout_applied", "positions": positions}],
            },
        )
        assert resp.status_code == 413

    def test_state_endpoints_removed(self, test_app: TestClient):
        """The transitional full-state PUT and the legacy PATCH shim are gone."""
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        # No route for .../state anymore → 404 (path unregistered), never 2xx.
        assert test_app.put(
            f"/api/sessions/{sid}/state", json={"state": {}}
        ).status_code in (404, 405)
        assert test_app.patch(f"/sessions/{sid}/state", json={}).status_code in (
            404,
            405,
        )

    def test_missing_required_field_returns_422_with_structured_detail(
        self, test_app: TestClient
    ):
        """The ops body is now parsed manually (for the pre-parse byte cap); a
        missing required field must still 422 with a FastAPI-shaped error list."""
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        resp = test_app.post(f"/api/sessions/{sid}/ops", json={"ops": []})
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert isinstance(detail, list)
        assert any(err.get("loc") == ["client_id"] for err in detail)

    def test_malformed_json_body_returns_422(self, test_app: TestClient):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        resp = test_app.post(
            f"/api/sessions/{sid}/ops",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422
