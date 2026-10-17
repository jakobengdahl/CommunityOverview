"""
Stored graph content that json.loads accepts must not make JSON responses 500.

Metadata nested ~300 levels deep and lone UTF-16 surrogates were accepted on
write but broke FastAPI's default response encoding, so the node's own reads
and every export failed. See backend/service/json_response.py.
"""

import json
import os
from datetime import datetime, timezone

import inspect

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.datastructures import Default
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient

from backend.core import GraphStorage, Node, NodeType
from backend.service import GraphService, create_rest_router
from backend.service.json_response import GraphJSONResponse, GraphJSONRoute

LONE_SURROGATE = "\ud800"


def _nested(depth: int) -> dict:
    root: dict = {}
    current = root
    for _ in range(depth):
        current["a"] = {}
        current = current["a"]
    return root


def _depth(value: dict) -> int:
    depth = 0
    while value:
        value = value["a"]
        depth += 1
    return depth


@pytest.fixture
def storage(temp_dir) -> GraphStorage:
    return GraphStorage(json_path=os.path.join(temp_dir, "graph.json"))


@pytest.fixture
def client(storage: GraphStorage) -> TestClient:
    app = FastAPI()
    app.include_router(create_rest_router(GraphService(storage)), prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


def _post_raw(client: TestClient, path: str, body: str):
    return client.post(path, content=body, headers={"content-type": "application/json"})


class TestDeeplyNestedMetadata:
    def test_stored_node_nested_300_deep_is_readable(self, storage, client):
        storage.add_nodes(
            [Node(id="deep", type=NodeType.ACTOR, name="Deep", metadata=_nested(300))],
            [],
        )

        response = client.get("/api/nodes/deep")

        assert response.status_code == 200
        assert _depth(response.json()["node"]["metadata"]) == 300

    def test_export_with_node_nested_300_deep_succeeds(self, storage, client):
        storage.add_nodes(
            [Node(id="deep", type=NodeType.ACTOR, name="Deep", metadata=_nested(300))],
            [],
        )

        response = client.get("/api/export")

        assert response.status_code == 200
        (node,) = [n for n in response.json()["nodes"] if n["id"] == "deep"]
        assert _depth(node["metadata"]) == 300

    def test_rest_write_and_read_far_past_the_old_depth_limit(self, client):
        # Past both Pydantic's serializer depth and jsonable_encoder's
        # recursion limit, so neither may sit on the response path.
        body = json.dumps(
            {
                "nodes": [
                    {
                        "id": "deeper",
                        "type": "Actor",
                        "name": "Deeper",
                        "metadata": _nested(3000),
                    }
                ],
                "edges": [],
            }
        )

        assert _post_raw(client, "/api/nodes", body).status_code == 200
        response = client.get("/api/nodes/deeper")
        assert response.status_code == 200
        assert _depth(response.json()["node"]["metadata"]) == 3000
        assert client.get("/api/export").status_code == 200

    def test_deep_metadata_holding_nan_is_readable(self, client):
        # NaN takes the null-substitution path, which must not recurse per level.
        body = json.dumps(
            {
                "nodes": [
                    {
                        "id": "deep-nan",
                        "type": "Actor",
                        "name": "Deep NaN",
                        "metadata": {"tree": _nested(3000), "score": float("nan")},
                    }
                ],
                "edges": [],
            }
        )

        assert _post_raw(client, "/api/nodes", body).status_code == 200
        response = client.get("/api/nodes/deep-nan")
        assert response.status_code == 200
        metadata = response.json()["node"]["metadata"]
        assert metadata["score"] is None
        assert _depth(metadata["tree"]) == 3000
        assert client.get("/api/export").status_code == 200


class TestLoneSurrogates:
    # A JSON body may carry "\ud800" escapes; json.loads turns them into lone
    # surrogates, which strict UTF-8 cannot encode.
    BODY = (
        '{"nodes":['
        '{"id":"bad\\ud800","type":"Actor","name":"Surrogate id"},'
        '{"id":"field","type":"Actor","name":"Surrogate field",'
        '"metadata":{"note":"x\\ud800y"}}'
        '],"edges":[]}'
    )

    def test_export_with_lone_surrogate_id_succeeds(self, client):
        assert _post_raw(client, "/api/nodes", self.BODY).status_code == 200

        response = client.get("/api/export")

        assert response.status_code == 200
        ids = {n["id"] for n in response.json()["nodes"]}
        assert f"bad{LONE_SURROGATE}" in ids

    def test_node_with_lone_surrogate_field_is_readable(self, client):
        assert _post_raw(client, "/api/nodes", self.BODY).status_code == 200

        response = client.get("/api/nodes/field")

        assert response.status_code == 200
        assert response.json()["node"]["metadata"]["note"] == f"x{LONE_SURROGATE}y"
        # The body is still well-formed JSON: the surrogate is an escape.
        assert b"\\ud800" in response.content

    def test_search_hit_with_lone_surrogate_succeeds(self, client):
        assert _post_raw(client, "/api/nodes", self.BODY).status_code == 200

        response = client.post("/api/search", json={"query": "Surrogate"})

        assert response.status_code == 200
        assert len(response.json()["nodes"]) == 2


class TestRenderingOfOrdinaryContent:
    def test_non_ascii_text_stays_unescaped_utf8(self, storage, client):
        storage.add_nodes([Node(id="se", type=NodeType.ACTOR, name="Myndighet å")], [])

        response = client.get("/api/nodes/se")

        assert response.status_code == 200
        assert "Myndighet å".encode("utf-8") in response.content
        assert b"\\u00e5" not in response.content

    def test_non_finite_metadata_renders_as_null(self, client):
        body = (
            '{"nodes":[{"id":"nan","type":"Actor","name":"NaN",'
            '"metadata":{"score":NaN,"high":Infinity}}],"edges":[]}'
        )
        assert _post_raw(client, "/api/nodes", body).status_code == 200

        response = client.get("/api/nodes/nan")

        assert response.status_code == 200
        assert response.json()["node"]["metadata"] == {"score": None, "high": None}

    def test_body_is_compact_json(self):
        assert GraphJSONResponse({"a": [1, "b"]}).body == b'{"a":[1,"b"]}'

    def test_route_errors_keep_their_status(self, client):
        assert client.get("/api/nodes/missing").status_code == 404

    def test_non_json_native_values_are_encoded_like_fastapi(self):
        moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

        body = GraphJSONResponse({"at": moment}).body

        assert json.loads(body) == {"at": moment.isoformat()}


class TestRoutesLeftAsTheyAre:
    @pytest.fixture
    def router(self) -> APIRouter:
        router = APIRouter(route_class=GraphJSONRoute)

        @router.get("/text", response_class=PlainTextResponse)
        async def text() -> str:
            return "plain"

        @router.get("/sync")
        def sync_route() -> dict:
            return {"sync": True}

        return router

    def test_explicit_response_class_is_kept(self, router):
        app = FastAPI()
        app.include_router(router)

        response = TestClient(app).get("/text")

        assert response.text == "plain"
        assert response.headers["content-type"].startswith("text/plain")

    def test_sync_endpoint_stays_sync(self, router):
        # A sync endpoint must keep running in the threadpool, not on the loop.
        (route,) = [r for r in router.routes if r.path == "/sync"]

        assert not inspect.iscoroutinefunction(route.endpoint)
        app = FastAPI()
        app.include_router(router)
        assert TestClient(app).get("/sync").json() == {"sync": True}

    def test_generator_endpoints_are_not_wrapped(self):
        def sync_stream():
            yield "chunk"

        async def async_stream():
            yield "chunk"

        def plain():
            return {}

        for endpoint in (sync_stream, async_stream):
            route = GraphJSONRoute(
                "/stream", endpoint, response_class=Default(JSONResponse)
            )
            assert route.endpoint is endpoint
        # Control: with the same arguments an ordinary endpoint is wrapped.
        route = GraphJSONRoute("/plain", plain, response_class=Default(JSONResponse))
        assert route.endpoint is not plain
