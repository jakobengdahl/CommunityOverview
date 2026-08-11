"""
Tests for the external node pulse-trigger endpoints.

External systems fire a visual pulse into a user's live visualization by calling
a dedicated, token-authenticated trigger URL. The token is minted per live
session (``POST /sessions/{id}/trigger-token``) and presented to the pulse
endpoint (``POST /sessions/{id}/pulse``) either as an ``Authorization: Bearer``
header or a ``?token=`` query param. A valid call emits a ``node_pulse`` command
over the existing SSE session-push channel.

Covers:
- minting returns a token and materialises the session
- the pulse endpoint rejects missing / wrong tokens (401) before doing anything
- a valid bearer token, and the query-param fallback, both enqueue the command
- the emitted command carries node_id / clamped style / sanitised colour
- session-id format validation and body validation (422)
- re-minting rotates the token (revocation)
"""

from fastapi.testclient import TestClient


def _mint(test_app: TestClient, session_id: str) -> str:
    resp = test_app.post(f"/sessions/{session_id}/trigger-token")
    assert resp.status_code == 200, resp.text
    return resp.json()["trigger_token"]


class TestMintTriggerToken:
    def test_mint_returns_token_and_creates_session(self, test_app: TestClient):
        session_id = "1000-2000"
        resp = test_app.post(f"/sessions/{session_id}/trigger-token")
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == session_id
        assert isinstance(body["trigger_token"], str) and body["trigger_token"]
        assert body["pulse_path"] == f"/sessions/{session_id}/pulse"
        assert test_app.app.state.session_registry.session_exists(session_id)

    def test_mint_rejects_invalid_session_id(self, test_app: TestClient):
        resp = test_app.post("/sessions/not-valid/trigger-token")
        assert resp.status_code == 400

    def test_reminting_rotates_and_revokes_prior_token(self, test_app: TestClient):
        session_id = "1111-2222"
        first = _mint(test_app, session_id)
        second = _mint(test_app, session_id)
        assert first != second

        # The old token no longer authenticates a pulse.
        stale = test_app.post(
            f"/sessions/{session_id}/pulse",
            json={"node_id": "n1"},
            headers={"Authorization": f"Bearer {first}"},
        )
        assert stale.status_code == 401


class TestPulseAuth:
    def test_pulse_without_token_is_unauthorized(self, test_app: TestClient):
        session_id = "3000-4000"
        _mint(test_app, session_id)
        resp = test_app.post(f"/sessions/{session_id}/pulse", json={"node_id": "n1"})
        assert resp.status_code == 401

    def test_pulse_with_wrong_token_is_unauthorized(self, test_app: TestClient):
        session_id = "3001-4001"
        _mint(test_app, session_id)
        resp = test_app.post(
            f"/sessions/{session_id}/pulse",
            json={"node_id": "n1"},
            headers={"Authorization": "Bearer nope"},
        )
        assert resp.status_code == 401

    def test_pulse_on_unminted_session_is_unauthorized(self, test_app: TestClient):
        # No token was ever minted for this session; even a plausible token fails
        # and the response does not reveal whether the session exists.
        resp = test_app.post(
            "/sessions/9090-9090/pulse",
            json={"node_id": "n1"},
            headers={"Authorization": "Bearer anything"},
        )
        assert resp.status_code == 401


class TestPulseDelivery:
    def _last_command(self, test_app: TestClient, session_id: str):
        queue = test_app.app.state.session_registry._sessions[session_id]["queue"]
        assert not queue.empty()
        return queue.get_nowait()

    def test_valid_bearer_token_enqueues_pulse_command(self, test_app: TestClient):
        session_id = "5000-6000"
        token = _mint(test_app, session_id)
        resp = test_app.post(
            f"/sessions/{session_id}/pulse",
            json={"node_id": "customer-42", "style": "grow", "duration_ms": 2000},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        # Reports dispatch (command_id), not receipt, and does not echo raw input.
        assert resp.json()["success"] is True
        assert resp.json()["command_id"]
        assert "node_id" not in resp.json()

        cmd = self._last_command(test_app, session_id)
        assert cmd["type"] == "node_pulse"
        assert cmd["node_id"] == "customer-42"
        assert cmd["pulse"]["style"] == "grow"
        assert cmd["pulse"]["duration_ms"] == 2000
        assert cmd["command_id"]

    def test_query_param_token_fallback_works(self, test_app: TestClient):
        session_id = "5001-6001"
        token = _mint(test_app, session_id)
        resp = test_app.post(
            f"/sessions/{session_id}/pulse?token={token}",
            json={"node_id": "n1"},
        )
        assert resp.status_code == 200
        cmd = self._last_command(test_app, session_id)
        assert cmd["node_id"] == "n1"

    def test_unknown_style_falls_back_to_glow(self, test_app: TestClient):
        session_id = "5002-6002"
        token = _mint(test_app, session_id)
        test_app.post(
            f"/sessions/{session_id}/pulse",
            json={"node_id": "n1", "style": "explode"},
            headers={"Authorization": f"Bearer {token}"},
        )
        cmd = self._last_command(test_app, session_id)
        assert cmd["pulse"]["style"] == "glow"

    def test_unsafe_colour_is_dropped_valid_colour_kept(self, test_app: TestClient):
        session_id = "5003-6003"
        token = _mint(test_app, session_id)

        test_app.post(
            f"/sessions/{session_id}/pulse",
            json={"node_id": "n1", "color": "#ff8800"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert self._last_command(test_app, session_id)["pulse"]["color"] == "#ff8800"

        test_app.post(
            f"/sessions/{session_id}/pulse",
            json={"node_id": "n1", "color": "red; } body{display:none}"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert self._last_command(test_app, session_id)["pulse"]["color"] is None


class TestPulseValidation:
    def test_invalid_session_id_format(self, test_app: TestClient):
        resp = test_app.post(
            "/sessions/bad-id/pulse",
            json={"node_id": "n1"},
            headers={"Authorization": "Bearer x"},
        )
        assert resp.status_code == 400

    def test_missing_node_id_is_unprocessable(self, test_app: TestClient):
        session_id = "7000-8000"
        token = _mint(test_app, session_id)
        resp = test_app.post(
            f"/sessions/{session_id}/pulse",
            json={"style": "glow"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422

    def test_duration_out_of_range_is_unprocessable(self, test_app: TestClient):
        session_id = "7001-8001"
        token = _mint(test_app, session_id)
        resp = test_app.post(
            f"/sessions/{session_id}/pulse",
            json={"node_id": "n1", "duration_ms": 999999},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422

    def test_node_id_with_unsafe_characters_is_rejected(self, test_app: TestClient):
        session_id = "7002-8002"
        token = _mint(test_app, session_id)
        for bad in ["<script>", "a b", 'x"y', "node`1"]:
            resp = test_app.post(
                f"/sessions/{session_id}/pulse",
                json={"node_id": bad},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 422, bad

    def test_uuid_and_slug_node_ids_are_accepted(self, test_app: TestClient):
        session_id = "7003-8003"
        token = _mint(test_app, session_id)
        for good in ["532c02ad-81ff-4334-8952-421577c393fb", "init-baseline-saas-v1"]:
            resp = test_app.post(
                f"/sessions/{session_id}/pulse",
                json={"node_id": good},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, good
