# covers: backend/functions/token_store.py, backend/functions/routes.py
import hashlib
import sqlite3
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from backend.functions.routes import register_token_routes
from backend.functions.token_store import WebhookTokenStore
from backend.runtime.authorization import GraphAuthorizationDecision


def test_token_lifecycle_and_bound_graph_authorization(tmp_path):
    store = WebhookTokenStore(tmp_path / "private" / "webhook_tokens.db")
    contexts = []
    allowed = {"graph-a"}

    class Hook:
        def evaluate(self, context):
            contexts.append(context)
            return GraphAuthorizationDecision(allowed=context.graph_id in allowed)

    service = SimpleNamespace(graph_service=SimpleNamespace(authorization_hook=Hook()))
    app = FastAPI()
    router = APIRouter()
    register_token_routes(router, service, store)
    app.include_router(router)
    client = TestClient(app)
    url = "/functions/example/webhook-token"
    assert (
        client.post(url, json={"graph_id": "graph-b", "label": "test"}).status_code
        == 403
    )
    response = client.post(url, json={"graph_id": "graph-a", "label": "test"})
    assert response.status_code == 201
    first = response.json()["token"]
    row = store.resolve(first)
    assert (row["function_id"], row["graph_id"], row["label"]) == (
        "example",
        "graph-a",
        "test",
    )
    with sqlite3.connect(store.path) as db:
        assert (
            db.execute("SELECT token_hash FROM webhook_tokens").fetchone()[0]
            == hashlib.sha256(first.encode()).hexdigest()
        )
    assert first.encode() not in store.path.read_bytes()
    assert (
        client.post(url, json={"graph_id": "graph-a", "label": "other"}).status_code
        == 409
    )
    allowed.clear()
    assert client.post(url + "/rotate").status_code == 403
    assert client.delete(url).status_code == 403
    assert store.resolve(first) == row
    allowed.add("graph-a")
    rotated = client.post(url + "/rotate").json()["token"]
    assert store.resolve(first) is None
    assert store.resolve(rotated)["id"] == row["id"]
    assert client.delete(url).status_code == 204
    assert store.resolve(rotated) is None
    assert store.get("example") is None
    assert all(c.action == "admin" and c.graph_id == "graph-a" for c in contexts[1:])


def test_graph_export_excludes_private_token_store(tmp_path):
    from backend.core import GraphStorage
    from backend.service import GraphService

    graph = GraphStorage(str(tmp_path / "graph.json"))
    service = GraphService(graph)
    before = service.export_graph()
    store = WebhookTokenStore(tmp_path / "webhook_tokens.db")
    store.mint("example", "graph-a", "credential-label")
    after = service.export_graph()
    before.pop("exportDate")
    after.pop("exportDate")
    assert after == before
    graph.shutdown_events()
