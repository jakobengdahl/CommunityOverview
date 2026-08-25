"""
Tests for the shared-session REST endpoints under /api/sessions (steps 1 & 3).

Covers the CRUD surface, node-ref resolution (?resolve=true), the ops endpoint
with its conflict/error semantics, and id validation. The realtime SSE stream is
exercised at the unit level in test_session_manager.py; here we assert the HTTP
contract of the request/response endpoints.
"""

import base64
import io

from fastapi.testclient import TestClient
from PIL import Image


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

    def test_rename_materialises_a_session_that_was_never_created(
        self, test_app: TestClient
    ):
        """R7: PATCH for an id that only exists client-side (never POSTed) must
        materialise it rather than 404, or the name is lost once it later saves
        with a null server name."""
        sid = "9999-9997"
        test_app.delete(
            f"/api/sessions/{sid}"
        )  # clean slate (shared temp dir across test runs)
        resp = test_app.patch(f"/api/sessions/{sid}", json={"name": "renamed"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "renamed"
        assert test_app.get(f"/api/sessions/{sid}").status_code == 200

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


class TestLegacyStreamRateLimit:
    """The legacy /sessions/{id}/stream endpoint applies the same lookup rate limit."""

    @staticmethod
    def _shrink_bucket(test_app: TestClient, capacity: float) -> None:
        from backend.core.session_manager import _TokenBucket

        mgr = test_app.app.state.session_manager
        mgr._lookup_bucket = _TokenBucket(capacity, 0.0)

    @staticmethod
    def _stub_legacy_stream(test_app: TestClient) -> None:
        async def _single_event_stream(_session_id):
            yield {"type": "ping"}

        test_app.app.state.session_registry.stream = _single_event_stream

    def test_legacy_stream_returns_429_when_exhausted(self, test_app: TestClient):
        self._shrink_bucket(test_app, capacity=0)
        resp = test_app.get("/sessions/1234-5678/stream")
        assert resp.status_code == 429

    def test_legacy_stream_returns_429_when_bucket_was_spent_by_api_lookup(
        self, test_app: TestClient
    ):
        self._shrink_bucket(test_app, capacity=1)
        assert test_app.get("/api/sessions/9999-9999").status_code == 404
        assert test_app.get("/sessions/1234-5678/stream").status_code == 429

    def test_legacy_stream_allows_request_within_budget(self, test_app: TestClient):
        self._shrink_bucket(test_app, capacity=1)
        self._stub_legacy_stream(test_app)
        resp = test_app.get("/sessions/1234-5678/stream")
        assert resp.status_code == 200

    def test_legacy_stream_rate_limit_keys_on_real_client_behind_proxy(
        self, temp_graph_file, temp_static_dirs
    ):
        """Per-IP budget applies; clients cannot spoof X-Forwarded-For."""
        import os
        from unittest.mock import patch
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
        with patch("backend.ui.chat_logic.create_provider"):
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
                app = create_app(config)
        app.state.session_manager._lookup_bucket = _TokenBucket(1.0, 0.0)

        async def _single_event_stream(_session_id):
            yield {"type": "ping"}

        app.state.session_registry.stream = _single_event_stream
        client = TestClient(app)

        assert (
            client.get(
                "/sessions/1111-1111/stream", headers={"X-Forwarded-For": "1.1.1.1"}
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/sessions/2222-2222/stream", headers={"X-Forwarded-For": "2.2.2.2"}
            ).status_code
            == 200
        )
        # Client A is now exhausted.
        assert (
            client.get(
                "/sessions/3333-3333/stream",
                headers={"X-Forwarded-For": "1.1.1.1"},
            ).status_code
            == 429
        )
        # Spoofed leading entry doesn't change A's key — still throttled.
        assert (
            client.get(
                "/sessions/4444-4444/stream",
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

    def test_ops_write_to_claimed_annotation_by_another_client_409(
        self, test_app: TestClient
    ):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        test_app.post(
            f"/api/sessions/{sid}/ops",
            json={
                "client_id": "c1",
                "ops": [
                    {
                        "op": "annotation_created",
                        "annotation": {"id": "note-1", "type": "note"},
                    },
                    {"op": "selection_claimed", "element_ids": ["note-1"]},
                ],
            },
        )
        resp = test_app.post(
            f"/api/sessions/{sid}/ops",
            json={
                "client_id": "c2",
                "ops": [
                    {
                        "op": "annotation_updated",
                        "annotation": {"id": "note-1", "text": "hijacked"},
                    }
                ],
            },
        )
        assert resp.status_code == 409
        assert "note-1" in resp.json()["detail"]
        state = test_app.get(f"/api/sessions/{sid}").json()["state"]
        assert state["annotations"][0].get("text") != "hijacked"


class TestSessionActivityAndUndo:
    def _create_note(
        self, test_app: TestClient, sid: str, client_id: str = "c1", text: str = "hi"
    ) -> None:
        resp = test_app.post(
            f"/api/sessions/{sid}/ops",
            json={
                "client_id": client_id,
                "ops": [
                    {
                        "op": "annotation_created",
                        "annotation": {"id": "note-1", "type": "note", "text": text},
                    }
                ],
            },
        )
        assert resp.status_code == 200

    def test_activity_lists_newest_first(self, test_app: TestClient):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        self._create_note(test_app, sid, text="first")
        test_app.post(
            f"/api/sessions/{sid}/ops",
            json={
                "client_id": "c1",
                "ops": [
                    {
                        "op": "annotation_updated",
                        "annotation": {
                            "id": "note-1",
                            "type": "note",
                            "text": "second",
                        },
                    }
                ],
            },
        )

        resp = test_app.get(f"/api/sessions/{sid}/activity")
        assert resp.status_code == 200
        records = resp.json()["activity"]
        assert [r["op"] for r in records] == [
            "annotation_updated",
            "annotation_created",
        ]

    def test_undo_reverts_the_actors_last_action(self, test_app: TestClient):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        self._create_note(test_app, sid)

        resp = test_app.post(f"/api/sessions/{sid}/undo", json={"client_id": "c1"})
        assert resp.status_code == 200
        assert resp.json()["undone_op"] == "annotation_created"

        state = test_app.get(f"/api/sessions/{sid}").json()["state"]
        assert state["annotations"] == []

    def test_undo_is_409_while_another_client_holds_the_claim(
        self, test_app: TestClient
    ):
        """Undo is a browser write, so it answers to the same claim rule
        POST /ops does. Actor-scoping does not cover this: the action being
        undone is the caller's own, but the annotation it touches was claimed
        by someone else in between."""
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        self._create_note(test_app, sid, text="hello")
        test_app.post(
            f"/api/sessions/{sid}/ops",
            json={
                "client_id": "c2",
                "ops": [{"op": "selection_claimed", "element_ids": ["note-1"]}],
            },
        )

        resp = test_app.post(f"/api/sessions/{sid}/undo", json={"client_id": "c1"})
        assert resp.status_code == 409
        assert "claimed by another client" in resp.json()["detail"]

        # Refused without touching anything: the note is still there and the
        # action is still undoable once the claim clears.
        state = test_app.get(f"/api/sessions/{sid}").json()["state"]
        assert state["annotations"][0]["text"] == "hello"

    def test_undo_succeeds_once_the_claim_is_released(self, test_app: TestClient):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        self._create_note(test_app, sid, text="hello")
        for op in ("selection_claimed", "selection_released"):
            test_app.post(
                f"/api/sessions/{sid}/ops",
                json={
                    "client_id": "c2",
                    "ops": [{"op": op, "element_ids": ["note-1"]}],
                },
            )

        resp = test_app.post(f"/api/sessions/{sid}/undo", json={"client_id": "c1"})
        assert resp.status_code == 200
        assert resp.json()["undone_op"] == "annotation_created"

    def test_undo_delete_restores_the_annotation(self, test_app: TestClient):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        self._create_note(test_app, sid, text="hello")
        test_app.post(
            f"/api/sessions/{sid}/ops",
            json={
                "client_id": "c1",
                "ops": [{"op": "annotation_deleted", "annotation_id": "note-1"}],
            },
        )

        resp = test_app.post(f"/api/sessions/{sid}/undo", json={"client_id": "c1"})
        assert resp.status_code == 200
        assert resp.json()["undone_op"] == "annotation_deleted"

        state = test_app.get(f"/api/sessions/{sid}").json()["state"]
        assert state["annotations"][0]["text"] == "hello"

    def test_undo_conflict_returns_409(self, test_app: TestClient):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        self._create_note(test_app, sid, client_id="c1", text="mine")
        test_app.post(
            f"/api/sessions/{sid}/ops",
            json={
                "client_id": "c2",
                "ops": [
                    {
                        "op": "annotation_updated",
                        "annotation": {
                            "id": "note-1",
                            "type": "note",
                            "text": "theirs",
                        },
                    }
                ],
            },
        )

        resp = test_app.post(f"/api/sessions/{sid}/undo", json={"client_id": "c1"})
        assert resp.status_code == 409

    def test_undo_with_no_history_returns_404(self, test_app: TestClient):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        resp = test_app.post(f"/api/sessions/{sid}/undo", json={"client_id": "c1"})
        assert resp.status_code == 404

    def test_undo_unknown_session_404(self, test_app: TestClient):
        resp = test_app.post("/api/sessions/9999-9999/undo", json={"client_id": "c1"})
        assert resp.status_code == 404


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

    def test_resolve_true_recovers_edges_between_present_refs(
        self, test_app: TestClient
    ):
        """Reloading a session must re-render the edges its data includes.

        Regression for "edges sometimes missing after reloading a session": edges
        are not stored in session state (an ``edges_added`` op persists none — #323),
        so a reload recovers them solely by resolving the session's node_refs
        against the graph. This is the load-bearing invariant behind that design and
        the exact ``?resolve=true`` call the frontend reload makes
        (App.jsx bootstrap → useSharedSession.loadSessionFromServer). An edge is
        returned iff *both* its endpoints are present node_refs; an edge whose other
        endpoint is not in the session is correctly left out (you cannot draw a
        half-edge). Sample graph fixture: node-1 -[edge-1]-> node-2 -[edge-2]-> node-3.
        """
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        test_app.post(
            f"/api/sessions/{sid}/ops",
            json={
                "client_id": "c1",
                "ops": [{"op": "nodes_added", "node_ids": ["node-1", "node-2"]}],
            },
        )
        resolved = test_app.get(f"/api/sessions/{sid}?resolve=true").json()["resolved"]
        edge_ids = {e["id"] for e in resolved["edges"]}
        # edge-1 connects two present refs → recovered on reload.
        assert "edge-1" in edge_ids
        # edge-2's other endpoint (node-3) is not in the session → not recovered.
        assert "edge-2" not in edge_ids
        # Shape the canvas edge filter relies on (GraphCanvas visibleEdges needs
        # e.id / e.source / e.target); a wrong shape would silently drop the edge.
        edge1 = next(e for e in resolved["edges"] if e["id"] == "edge-1")
        assert edge1["source"] == "node-1"
        assert edge1["target"] == "node-2"


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


class TestSessionOpsImageIngestGuard:
    """This endpoint is the widest of the image-ingest bypasses: no MCP layer is
    involved, so anything holding a session id can post an annotation op
    directly. The guard is unit-tested in
    ``backend/core/tests/test_session_annotations_image_guard.py``; these assert
    it is actually reachable over HTTP and reported as a 400."""

    _EMBEDDED = (
        "data:image/webp;base64,UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA=="
    )

    def _image_op(self, url: str) -> dict:
        return {
            "op": "annotation_created",
            "annotation": {
                "id": "img-1",
                "type": "image",
                "position": {"x": 0, "y": 0},
                "image": {"url": url, "width": 10, "height": 10},
            },
        }

    def test_remote_image_url_is_rejected_400_and_persists_nothing(
        self, test_app: TestClient
    ):
        sid = test_app.post("/api/sessions", json={}).json()["id"]

        resp = test_app.post(
            f"/api/sessions/{sid}/ops",
            json={
                "client_id": "c1",
                "base_seq": 0,
                "ops": [self._image_op("https://example.com/logo.png")],
            },
        )

        assert resp.status_code == 400
        state = test_app.get(f"/api/sessions/{sid}").json()["state"]
        assert state["annotations"] == []

    def test_embedded_image_url_is_accepted(self, test_app: TestClient):
        sid = test_app.post("/api/sessions", json={}).json()["id"]

        resp = test_app.post(
            f"/api/sessions/{sid}/ops",
            json={
                "client_id": "c1",
                "base_seq": 0,
                "ops": [self._image_op(self._EMBEDDED)],
            },
        )

        assert resp.status_code == 200
        state = test_app.get(f"/api/sessions/{sid}").json()["state"]
        assert [a["image"]["url"] for a in state["annotations"]] == [self._EMBEDDED]


def _png_data_url(size=(4, 4), color=(255, 0, 0)):
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _bmp_data_url():
    image = Image.new("RGB", (4, 4), (1, 2, 3))
    buffer = io.BytesIO()
    image.save(buffer, format="BMP")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/bmp;base64,{encoded}"


class TestSessionImageIngestEndpoint:
    """POST /api/sessions/{id}/annotations/image — the human GUI's clipboard-
    paste / file-upload half of image ingest (task-annotation-image-ingest-
    embedding). Shares the exact validate/optimize/embed pipeline the MCP
    create_image_annotation tool exercises (unit-tested end to end in
    backend/service/tests/test_mcp_image_annotation_tool.py); these tests
    cover the REST endpoint's own glue — error mapping, the id-collision
    guard, and above all the SSE echo-attribution fix: the op the ingest
    triggers must broadcast under a client id distinct from the posting
    browser's own, or sessionSyncClient.js drops it as a self-authored echo
    and the pasting browser never sees its own paste."""

    def test_ingests_from_image_data_and_persists_the_embedded_copy(
        self, test_app: TestClient
    ):
        sid = test_app.post("/api/sessions", json={}).json()["id"]

        resp = test_app.post(
            f"/api/sessions/{sid}/annotations/image",
            json={
                "client_id": "browser-1",
                "x": 10,
                "y": 20,
                "image_data": _png_data_url(),
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        annotation = body["annotation"]
        assert annotation["image"]["url"].startswith("data:image/webp;base64,")
        assert body["revision"] == 1

        state = test_app.get(f"/api/sessions/{sid}").json()["state"]
        assert len(state["annotations"]) == 1
        stored = state["annotations"][0]
        assert stored["image"]["url"].startswith("data:image/webp;base64,")
        # The pasting browser's real identity is preserved as the human-visible
        # creator even though the op that broadcasts it (below) is attributed
        # to a different, dedicated id.
        assert stored["created_by"] == "browser-1"

    def test_ingest_op_is_attributed_to_a_marker_distinct_from_the_posting_browser(
        self, test_app: TestClient
    ):
        """The core regression: sessionSyncClient.js drops the SSE echo of an
        op whose client_id equals the receiving browser's own — so the op this
        endpoint triggers must NOT carry the posting browser's client_id, or
        that same browser's own paste would never come back to it over its
        own subscription.

        The manager-level plumbing that turns a write's client_id into both
        the SSE broadcast's `client_id` field and the activity log's `actor`
        is exercised directly (unchanged by this task) in
        backend/core/tests/test_session_manager.py's
        TestUpsertImageAnnotation.test_creates_and_broadcasts. What is new
        here is only that this REST endpoint must call it with a marker, not
        with the caller's own client_id — proved observably through the
        activity log (actor) and the per-actor /undo endpoint, without having
        to hold the SSE stream open over HTTP (which this endpoint's response
        already makes redundant: see its docstring)."""
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        browser_client_id = "browser-1"

        resp = test_app.post(
            f"/api/sessions/{sid}/annotations/image",
            json={
                "client_id": browser_client_id,
                "x": 0,
                "y": 0,
                "image_data": _png_data_url(),
            },
        )
        assert resp.status_code == 200

        # The activity record's actor is exactly the op's broadcast client_id
        # (SessionStore.apply_state_op: `actor = applied.pop("client_id")`) —
        # so this is the same fact the SSE frame would have carried.
        as_browser = test_app.get(
            f"/api/sessions/{sid}/activity", params={"actor": browser_client_id}
        ).json()["activity"]
        assert as_browser == []

        as_marker = test_app.get(
            f"/api/sessions/{sid}/activity", params={"actor": "human-image-ingest"}
        ).json()["activity"]
        assert len(as_marker) == 1
        assert as_marker[0]["op"] == "annotation_created"

        # Observable consequence for undo too: the posting browser's own undo
        # must not see this as its own action (D2/actor-scoped undo).
        undo_as_browser = test_app.post(
            f"/api/sessions/{sid}/undo", json={"client_id": browser_client_id}
        )
        assert undo_as_browser.status_code == 404
        undo_as_marker = test_app.post(
            f"/api/sessions/{sid}/undo", json={"client_id": "human-image-ingest"}
        )
        assert undo_as_marker.status_code == 200
        assert undo_as_marker.json()["undone_op"] == "annotation_created"

    def test_ingests_from_image_url_through_the_same_shared_pipeline(
        self, test_app: TestClient, monkeypatch
    ):
        """image_url goes through the identical fetch_image_bytes/optimize_image
        pipeline the MCP tool uses (backend/core/image_ingest.py) — proves this
        endpoint does not reimplement ingest, only reuses it."""
        import httpx2 as httpx
        from backend.core import image_ingest

        png_bytes = base64.b64decode(_png_data_url().split(",", 1)[1])

        def handler(request):
            return httpx.Response(
                200, content=png_bytes, headers={"content-type": "image/png"}
            )

        class _StubHttpxModule:
            HTTPError = httpx.HTTPError

            def __init__(self, transport):
                self._transport = transport

            def Client(self, **kwargs):
                return httpx.Client(transport=self._transport, **kwargs)

        monkeypatch.setattr(
            image_ingest, "httpx", _StubHttpxModule(httpx.MockTransport(handler))
        )

        sid = test_app.post("/api/sessions", json={}).json()["id"]
        resp = test_app.post(
            f"/api/sessions/{sid}/annotations/image",
            json={
                "client_id": "browser-1",
                "x": 0,
                "y": 0,
                "image_url": "https://example.com/pic.png",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["annotation"]["image"]["url"].startswith(
            "data:image/webp;base64,"
        )

    def test_requires_exactly_one_of_image_data_or_image_url(
        self, test_app: TestClient
    ):
        sid = test_app.post("/api/sessions", json={}).json()["id"]

        neither = test_app.post(
            f"/api/sessions/{sid}/annotations/image",
            json={"client_id": "c1", "x": 0, "y": 0},
        )
        assert neither.status_code == 400

        both = test_app.post(
            f"/api/sessions/{sid}/annotations/image",
            json={
                "client_id": "c1",
                "x": 0,
                "y": 0,
                "image_data": _png_data_url(),
                "image_url": "https://example.com/pic.png",
            },
        )
        assert both.status_code == 400

    def test_unsupported_image_type_returns_415_and_persists_nothing(
        self, test_app: TestClient
    ):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        resp = test_app.post(
            f"/api/sessions/{sid}/annotations/image",
            json={"client_id": "c1", "x": 0, "y": 0, "image_data": _bmp_data_url()},
        )
        assert resp.status_code == 415
        state = test_app.get(f"/api/sessions/{sid}").json()["state"]
        assert state["annotations"] == []

    def test_invalid_base64_returns_400(self, test_app: TestClient):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        resp = test_app.post(
            f"/api/sessions/{sid}/annotations/image",
            json={
                "client_id": "c1",
                "x": 0,
                "y": 0,
                "image_data": "data:image/png;base64,not-valid-base64!!!",
            },
        )
        assert resp.status_code == 400

    def test_unknown_session_returns_404(self, test_app: TestClient):
        resp = test_app.post(
            "/api/sessions/9999-9999/annotations/image",
            json={"client_id": "c1", "x": 0, "y": 0, "image_data": _png_data_url()},
        )
        assert resp.status_code == 404

    def test_invalid_session_id_format_returns_400(self, test_app: TestClient):
        resp = test_app.post(
            "/api/sessions/not-a-valid-id/annotations/image",
            json={"client_id": "c1", "x": 0, "y": 0, "image_data": _png_data_url()},
        )
        assert resp.status_code == 400

    def test_annotation_id_colliding_with_a_different_type_is_refused(
        self, test_app: TestClient
    ):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        test_app.post(
            f"/api/sessions/{sid}/ops",
            json={
                "client_id": "c1",
                "base_seq": 0,
                "ops": [
                    {
                        "op": "annotation_created",
                        "annotation": {
                            "id": "shared-id",
                            "type": "note",
                            "position": {"x": 0, "y": 0},
                            "text": "hi",
                        },
                    }
                ],
            },
        )

        resp = test_app.post(
            f"/api/sessions/{sid}/annotations/image",
            json={
                "client_id": "c1",
                "x": 0,
                "y": 0,
                "image_data": _png_data_url(),
                "annotation_id": "shared-id",
            },
        )
        assert resp.status_code == 409
        state = test_app.get(f"/api/sessions/{sid}").json()["state"]
        assert state["annotations"][0]["type"] == "note"

    def test_replacing_an_existing_image_annotation_by_id_succeeds(
        self, test_app: TestClient
    ):
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        first = test_app.post(
            f"/api/sessions/{sid}/annotations/image",
            json={
                "client_id": "c1",
                "x": 0,
                "y": 0,
                "image_data": _png_data_url(color=(255, 0, 0)),
                "annotation_id": "img-1",
            },
        )
        assert first.status_code == 200

        second = test_app.post(
            f"/api/sessions/{sid}/annotations/image",
            json={
                "client_id": "c1",
                "x": 5,
                "y": 5,
                "image_data": _png_data_url(color=(0, 255, 0)),
                "annotation_id": "img-1",
            },
        )
        assert second.status_code == 200
        state = test_app.get(f"/api/sessions/{sid}").json()["state"]
        assert len(state["annotations"]) == 1
        assert state["annotations"][0]["id"] == "img-1"

    def test_replacing_a_claimed_image_annotation_is_rejected_409(
        self, test_app: TestClient
    ):
        """This endpoint writes through upsert_image_annotation directly, not
        apply_ops, so it needs its own claim check (rest_api.py) rather than
        inheriting SessionManager.apply_ops'. The op broadcast for this write
        is always attributed to the shared human-image-ingest marker (see the
        SSE-echo test above), but the *claim* check must use the real posting
        browser's own client_id — 'browser-1' here — or a browser holding its
        own claim would be locked out of replacing its own image."""
        sid = test_app.post("/api/sessions", json={}).json()["id"]
        first = test_app.post(
            f"/api/sessions/{sid}/annotations/image",
            json={
                "client_id": "browser-1",
                "x": 0,
                "y": 0,
                "image_data": _png_data_url(color=(255, 0, 0)),
                "annotation_id": "img-1",
            },
        )
        assert first.status_code == 200

        mgr = test_app.app.state.session_manager
        mgr.claims.claim(sid, "browser-2", ["img-1"])

        blocked = test_app.post(
            f"/api/sessions/{sid}/annotations/image",
            json={
                "client_id": "browser-1",
                "x": 5,
                "y": 5,
                "image_data": _png_data_url(color=(0, 255, 0)),
                "annotation_id": "img-1",
            },
        )
        assert blocked.status_code == 409
        state = test_app.get(f"/api/sessions/{sid}").json()["state"]
        assert (
            state["annotations"][0]["image"]["url"]
            == first.json()["annotation"]["image"]["url"]
        )

        # The claim holder's own replacement still succeeds.
        allowed = test_app.post(
            f"/api/sessions/{sid}/annotations/image",
            json={
                "client_id": "browser-2",
                "x": 5,
                "y": 5,
                "image_data": _png_data_url(color=(0, 255, 0)),
                "annotation_id": "img-1",
            },
        )
        assert allowed.status_code == 200
