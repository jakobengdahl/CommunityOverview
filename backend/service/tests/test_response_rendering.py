"""
Stored graph content that json.loads accepts must not make JSON responses 500.

Metadata nested ~300 levels deep and lone UTF-16 surrogates were accepted on
write but broke FastAPI's default response encoding, so the node's own reads
and every export failed. See backend/service/json_response.py.
"""

import enum
import inspect
import json
import os
from datetime import datetime, timezone

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.datastructures import Default
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.core import GraphStorage, Node, NodeType
from backend.service import GraphService, create_rest_router
from backend.service.json_response import (
    GraphJSONResponse,
    GraphJSONRoute,
    install_error_handlers,
)
from backend.service.serializers import _to_json_native, serialize_to_json

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

    def test_rest_write_and_read_past_the_old_depth_limit(self, client):
        # Past Pydantic's serializer depth; shallow enough that the request
        # parser accepts it on Python 3.11, whose C json recurses per level.
        body = '{"nodes":[{"id":"deeper","type":"Actor","name":"Deeper","metadata":'
        body += '{"a":' * 800 + "{}" + "}" * 800 + '}],"edges":[]}'

        assert _post_raw(client, "/api/nodes", body).status_code == 200
        response = client.get("/api/nodes/deeper")
        assert response.status_code == 200
        assert _depth(response.json()["node"]["metadata"]) == 800

    def test_node_nested_past_the_recursion_limit_is_readable(self, storage, client):
        # Deeper than any recursive encoder or decoder can go, with a NaN that
        # sends rendering down the fallback path too. Nothing here may recurse
        # per level, so the body is checked as bytes, not parsed.
        storage.add_nodes(
            [
                Node(
                    id="deepest",
                    type=NodeType.ACTOR,
                    name="Deepest",
                    metadata={"tree": _nested(3000), "score": float("nan")},
                )
            ],
            [],
        )

        for path in ("/api/nodes/deepest", "/api/export"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.content.count(b'{"a":') == 3000
            assert b'"score":null' in response.content


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
            '"metadata":{"score":NaN,"high":Infinity,"low":-Infinity}}],"edges":[]}'
        )
        assert _post_raw(client, "/api/nodes", body).status_code == 200

        response = client.get("/api/nodes/nan")

        assert response.status_code == 200
        assert response.json()["node"]["metadata"] == {
            "score": None,
            "high": None,
            "low": None,
        }

    def test_body_is_compact_json(self):
        assert GraphJSONResponse({"a": [1, "b"]}).body == b'{"a":[1,"b"]}'

    def test_route_errors_keep_their_status(self, client):
        assert client.get("/api/nodes/missing").status_code == 404

    def test_non_json_native_values_are_encoded_like_fastapi(self):
        moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

        body = GraphJSONResponse({"at": moment, "name": "å"}).body

        assert json.loads(body) == {"at": moment.isoformat(), "name": "å"}
        assert "å".encode("utf-8") in body

    def test_fallback_rendering_matches_json_and_leaves_content_alone(self):
        shared = [float("-inf"), "å"]
        content = {"t": (float("nan"), 1), "x": shared, "y": shared, 2: True}

        body = GraphJSONResponse(content).body

        assert body == '{"t":[null,1],"x":[null,"å"],"y":[null,"å"],"2":true}'.encode()
        assert content["x"] == [float("-inf"), "å"]

    def test_fallback_matches_json_dumps_on_awkward_values(self):
        # The NaN sends rendering down the iterative path; everything else
        # must come out exactly as json.dumps writes it.
        awkward = {
            "none": None,
            "text": 'quote " backslash \\ newline \n å',
            "tuple": (1, 2.5, -0.0, 10**30),
            True: False,
            2.5: "float key",
            None: "none key",
        }
        expected = json.dumps(awkward, ensure_ascii=False, separators=(",", ":"))

        body = GraphJSONResponse({"nan": float("nan"), "v": awkward}).body

        assert body == ('{"nan":null,"v":' + expected + "}").encode("utf-8")

    def test_fallback_escapes_a_lone_surrogate(self):
        body = GraphJSONResponse({"nan": float("nan"), "s": LONE_SURROGATE}).body

        assert body == b'{"nan":null,"s":"\\ud800"}'

    def test_circular_content_is_reported_not_looped(self):
        content: list = [float("nan")]
        content.append(content)

        with pytest.raises(ValueError):
            GraphJSONResponse(content)


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


class TestSerializeToJson:
    def test_deep_data_converts_like_the_json_round_trip(self):
        moment = datetime(2026, 1, 2, tzinfo=timezone.utc)
        data = {"tree": _nested(3000), 1: (moment, float("nan"))}

        converted = serialize_to_json(data)

        assert _depth(converted["tree"]) == 3000
        assert converted["1"][0] == moment.isoformat()
        assert converted["1"][1] != converted["1"][1]  # NaN survives, as in json
        assert 1 in data and isinstance(data[1], tuple)

    def test_iterative_fallback_turns_subclasses_into_base_types(self):
        class Kind(str, enum.Enum):
            A = "a"

        class Level(enum.IntEnum):
            HIGH = 3

        converted = _to_json_native({"k": Kind.A, "n": Level.HIGH})

        assert type(converted["k"]) is str and converted["k"] == "a"
        assert type(converted["n"]) is int and converted["n"] == 3

    def test_iterative_fallback_equals_the_json_round_trip(self):
        # Exercised directly: on Python 3.13 json.dumps itself copes with
        # 3000 levels, so the test above takes the fallback only on 3.11.
        moment = datetime(2026, 1, 2, tzinfo=timezone.utc)
        shared = ["å", 2.5]
        data = {"t": (moment, None, True), 3: shared, None: shared, "d": _nested(5)}

        expected = json.loads(json.dumps(data, default=lambda o: o.isoformat()))

        converted = _to_json_native(data)
        assert converted == expected
        assert type(converted["t"][2]) is bool
        assert _depth(_to_json_native(_nested(3000))) == 3000


class TestErrorResponses:
    @pytest.fixture
    def error_client(self, storage: GraphStorage) -> TestClient:
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(create_rest_router(GraphService(storage)), prefix="/api")
        return TestClient(app, raise_server_exceptions=False)

    def test_duplicate_id_echoing_a_lone_surrogate_keeps_its_conflict_status(
        self, error_client
    ):
        node = '{"nodes":[{"id":"dup\\ud800","type":"Actor","name":"A"}],"edges":[]'
        assert _post_raw(error_client, "/api/nodes", node + "}").status_code == 200

        response = _post_raw(
            error_client, "/api/nodes", node + ',"expect_absent":true}'
        )

        assert response.status_code == 409
        assert response.json()["detail"]["entity_id"] == "dup\ud800"

    def test_validation_error_input_with_a_lone_surrogate_stays_422(self, error_client):
        body = '{"nodes":"bad\\ud800","edges":[]}'

        response = _post_raw(error_client, "/api/nodes", body)

        assert response.status_code == 422
        assert response.json()["detail"][0]["input"] == "bad\ud800"

    def test_ordinary_error_bodies_match_the_framework_defaults(self, storage):
        def build(install: bool) -> TestClient:
            app = FastAPI()
            if install:
                install_error_handlers(app)
            app.include_router(create_rest_router(GraphService(storage)), prefix="/api")
            return TestClient(app, raise_server_exceptions=False)

        for method, path, body in (
            ("get", "/api/nodes/missing-å", None),
            ("post", "/api/nodes", '{"nodes":"bad"}'),
        ):
            responses = []
            for install in (False, True):
                kwargs = (
                    {}
                    if body is None
                    else {
                        "content": body,
                        "headers": {"content-type": "application/json"},
                    }
                )
                responses.append(getattr(build(install), method)(path, **kwargs))
            default, custom = responses
            assert default.status_code == custom.status_code >= 400
            assert default.content == custom.content
            assert default.headers["content-type"] == custom.headers["content-type"]

    def test_headers_and_bodyless_statuses_are_preserved(self):
        app = FastAPI()
        install_error_handlers(app)

        @app.get("/denied")
        def denied():
            raise HTTPException(401, "no", headers={"WWW-Authenticate": "Bearer"})

        @app.get("/not-modified")
        def not_modified():
            raise HTTPException(304, headers={"ETag": "v1"})

        @app.get("/no-content")
        def no_content():
            raise HTTPException(204, headers={"X-Kept": "1"})

        client = TestClient(app)
        assert client.get("/denied").headers["www-authenticate"] == "Bearer"
        for path, status, header in (
            ("/not-modified", 304, ("etag", "v1")),
            ("/no-content", 204, ("x-kept", "1")),
        ):
            response = client.get(path)
            assert response.status_code == status
            assert response.content == b""
            assert response.headers[header[0]] == header[1]

    def test_starlette_level_http_exceptions_are_covered(self):
        app = FastAPI()
        install_error_handlers(app)

        @app.get("/raw")
        def raw():
            raise StarletteHTTPException(409, "x" + LONE_SURROGATE)

        response = TestClient(app).get("/raw")

        assert response.status_code == 409
        assert response.json() == {"detail": "x" + LONE_SURROGATE}
