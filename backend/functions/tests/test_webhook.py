# covers: backend/functions/webhook.py
"""Webhook admission and bound identity."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.agents.execution import ExecutionJob, ExecutionKind, ExecutionState
from backend.functions.token_store import WebhookTokenStore
from backend.functions.webhook import register_webhook_routes


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    package = tmp_path / "example"
    package.mkdir()
    (package / "main.py").write_text("pass")
    (package / "function.json").write_text(
        json.dumps(
            {
                "contract_version": "2",
                "id": "example",
                "version": "1.0.0",
                "name": "Example",
                "runtime": "python",
                "entry_point": "main.py",
                "triggers": [{"kind": "webhook"}],
                "grants": [],
                "input_schema": {"type": "object", "required": ["value"]},
            }
        )
    )
    store = WebhookTokenStore(tmp_path / "private.db")
    token = store.mint("example", "graph-a", "test")
    job = ExecutionJob(
        agent_id="example",
        idempotency_key="job",
        kind=ExecutionKind.FUNCTION,
        state=ExecutionState.RUNNING,
    )
    service = SimpleNamespace(
        root=tmp_path, enabled={"example": []}, invoke=Mock(return_value=job)
    )

    def authorize(request, graph_id, target):
        if request.headers.get("admin") != graph_id:
            raise HTTPException(403)

    def existing(function_id):
        return store.get(function_id)

    app = FastAPI()
    router = APIRouter()
    register_webhook_routes(router, service, store, authorize, existing)
    app.include_router(router)
    monkeypatch.setenv("FUNCTION_WEBHOOKS_ENABLED", "true")
    return TestClient(app), service, store, token


def test_settings_require_bound_graph_admin_and_follow_token(endpoint):
    client, service, store, token = endpoint
    path = "/functions/example/webhook-settings"
    settings = {"enabled": True, "allow_write": True}
    assert (
        client.put(path, json=settings, headers={"admin": "graph-b"}).status_code == 403
    )
    assert (
        client.put(path, json=settings, headers={"admin": "graph-a"}).status_code == 200
    )
    assert service.webhook_policy.get("example", "graph-a") == (True, True)
    row = store.get("example")
    store.revoke("example", "graph-a", row["id"])
    store.mint("example", "graph-a", "new")
    assert service.webhook_policy.get("example", "graph-a") == (False, False)


def test_auth_limits_input_and_bound_actor(endpoint, monkeypatch):
    client, service, _, token = endpoint
    path = "/functions/example/webhook"
    auth = {
        "Authorization": f"Bearer {token}",
        "x-communityoverview-actor-id": "spoof",
        "x-communityoverview-graph-id": "other",
    }
    assert client.post(path, json={"value": 1}, headers=auth).status_code == 404
    client.put(
        path + "-settings",
        json={"enabled": True, "allow_write": False},
        headers={"admin": "graph-a"},
    )
    assert client.post(path + "?token=" + token, json={"value": 1}).status_code == 401
    assert client.post(path, content=b"x" * 65537, headers=auth).status_code == 413
    assert client.post(path, json={}, headers=auth).status_code == 422
    service.invoke.assert_not_called()
    response = client.post(
        path, json={"value": 1}, headers={**auth, "Idempotency-Key": "key"}
    )
    assert response.status_code == 200
    args = service.invoke.call_args
    assert args.kwargs["actor_binding"] == {
        "actor_id": "example",
        "actor_type": "function_webhook",
        "auth_source": "webhook",
        "graph_id": "graph-a",
    }
    assert args.kwargs["idempotency_key"] == "key"
    monkeypatch.setattr(service.webhook_policy, "admit", lambda _: False)
    assert client.post(path, content=b"invalid", headers=auth).status_code == 429
    monkeypatch.setenv("FUNCTION_WEBHOOKS_ENABLED", "false")
    assert client.post(path, content=b"invalid", headers=auth).status_code == 404


@pytest.mark.asyncio
async def test_rejections_do_not_receive_body(endpoint, monkeypatch):
    client, service, _, token = endpoint
    app = client.app
    client.put(
        "/functions/example/webhook-settings",
        json={"enabled": True, "allow_write": False},
        headers={"admin": "graph-a"},
    )

    async def receive():
        raise AssertionError("body was read")

    async def send(message):
        pass

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/functions/example/webhook",
        "raw_path": b"/functions/example/webhook",
        "query_string": b"",
        "headers": [],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    await app(scope.copy(), receive, send)
    scope["headers"] = [(b"authorization", f"Bearer {token}".encode())]
    monkeypatch.setattr(service.webhook_policy, "admit", lambda _: False)
    await app(scope.copy(), receive, send)
    monkeypatch.setenv("FUNCTION_WEBHOOKS_ENABLED", "false")
    await app(scope.copy(), receive, send)


def test_rate_limit_is_shared_by_policy_instances(endpoint):
    from backend.functions.webhook import WebhookPolicy

    _, service, store, _ = endpoint
    first = service.webhook_policy
    second = WebhookPolicy(store)
    assert all(first.admit("example") for _ in range(60))
    assert not second.admit("example")
    assert second.admit("other")
