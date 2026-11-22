# covers: backend/core/user_preferences.py, backend/service/rest_api.py
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api_host.middleware import AUTHENTICATED_PRINCIPAL_SCOPE_KEY
from backend.core import GraphStorage, user_preferences
from backend.core.user_preferences import UserPreferencesStore
from backend.service import GraphService
from backend.service.rest_api import create_rest_router


def _client(tmp_path):
    """``x-test-principal: kind:id`` stands in for the auth middleware's principal."""
    app = FastAPI()
    service = GraphService(GraphStorage(json_path=str(tmp_path / "g.json")))
    store = UserPreferencesStore(tmp_path / "preferences")
    app.include_router(create_rest_router(service, user_preferences=store))

    @app.middleware("http")
    async def authenticate(request, call_next):
        if principal := request.headers.get("x-test-principal"):
            kind, _, ident = principal.partition(":")
            request.scope[AUTHENTICATED_PRINCIPAL_SCOPE_KEY] = {
                "kind": kind,
                "id": ident,
            }
        return await call_next(request)

    return TestClient(app)


def _hide(client, principal=None):
    headers = {"x-test-principal": principal} if principal else {}
    response = client.get("/user-preferences", headers=headers)
    assert response.status_code == 200
    return response.json()["preferences"]["hideClassicSurfaces"]


def test_preference_is_per_user_and_defaults_off(tmp_path):
    """Missing means off; a save affects only its own user; anonymous requests
    and the shared instance token read the default and cannot save."""
    client = _client(tmp_path)
    assert _hide(client, "user:alice") is False

    saved = client.put(
        "/user-preferences",
        json={"hideClassicSurfaces": True},
        headers={"x-test-principal": "user:alice"},
    )
    assert saved.status_code == 200
    assert saved.json()["preferences"]["hideClassicSurfaces"] is True
    assert _hide(client, "user:alice") is True
    assert _hide(client, "user:bob") is False
    assert _hide(client) is False
    assert client.get("/user-preferences").json()["signed_in"] is False

    for headers in ({}, {"x-test-principal": "bearer:instance-token"}):
        refused = client.put(
            "/user-preferences", json={"hideClassicSurfaces": True}, headers=headers
        )
        assert refused.status_code == 403
    assert _hide(client, "bearer:instance-token") is False


def test_failed_save_keeps_the_previous_value(tmp_path, monkeypatch):
    client = _client(tmp_path)
    alice = {"x-test-principal": "user:alice"}
    client.put("/user-preferences", json={"hideClassicSurfaces": True}, headers=alice)

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(user_preferences.os, "replace", fail)
    failed = client.put(
        "/user-preferences", json={"hideClassicSurfaces": False}, headers=alice
    )
    assert failed.status_code == 503
    assert _hide(client, "user:alice") is True
