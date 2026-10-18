"""
Stored graph content that json.loads accepts must not make JSON responses 500.

Metadata nested ~300 levels deep and lone UTF-16 surrogates were accepted on
write but broke FastAPI's default response encoding, so the node's own reads
and every export failed. See backend/service/json_response.py.
"""

import enum
import inspect
import json
import sys
from datetime import datetime, timezone

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.datastructures import Default
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.core import GraphStorage, Node, NodeType
from backend.core.storage_backends import BackendCapabilities
from backend.service import GraphService, create_rest_router
from backend.service.json_response import (
    GraphJSONResponse,
    GraphJSONRoute,
    _iterencode,
    install_error_handlers,
)
from backend.service import serializers
from backend.service.serializers import (
    _to_json_native,
    json_key_text,
    serialize_to_json,
)

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


class _DiscardingBackend:
    """Snapshot backend that keeps nothing.

    The deep nodes below cannot always be persisted (a lone surrogate, or
    nesting past what the file backend's encoder takes); building them into a
    storage without persistence keeps these tests independent of whether the
    save succeeds or its failure is swallowed.
    """

    def exists(self) -> bool:
        return False

    def load_graph_data(self) -> dict:
        return {"nodes": [], "edges": []}

    def save_graph_data(self, data: dict) -> None:
        pass

    def default_graph_name(self) -> str:
        return "discarding"

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()


@pytest.fixture
def storage() -> GraphStorage:
    return GraphStorage(persistence_backend=_DiscardingBackend())


class _Odd(int):
    def __repr__(self) -> str:
        return "odd-int"

    __str__ = __repr__


class _OddFloat(float):
    def __repr__(self) -> str:
        return "odd-float"

    __str__ = __repr__


@pytest.fixture
def low_recursion_limit():
    """Make RecursionError reachable for modest nesting on any Python.

    Python 3.11 ties the C json encoder to this limit; later versions use a
    fixed C stack bound that ignores it, which `_beyond_the_encoder` covers.
    """
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        yield
    finally:
        sys.setrecursionlimit(previous)


def _beyond_the_encoder() -> dict:
    """Nesting json.dumps gives up on whatever the Python version."""
    content = _nested(50_000)
    with pytest.raises(RecursionError):
        json.dumps(content)  # control: the branch under test is reachable
    return content


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


class TestRecursionFallback:
    def test_rendering_past_the_encoder_depth_uses_the_iterative_encoder(
        self, low_recursion_limit
    ):
        body = GraphJSONResponse({"tree": _beyond_the_encoder()}).body

        assert body.count(b'{"a":') == 50_000

    def test_serialize_to_json_past_the_encoder_depth_converts_iteratively(
        self, low_recursion_limit
    ):
        converted = serialize_to_json({"tree": _beyond_the_encoder()})

        assert _depth(converted["tree"]) == 50_000


class TestIterativeEncoderEdges:
    def test_unsupported_type_is_a_type_error_naming_it(self):
        with pytest.raises(TypeError, match="Object of type object is not JSON"):
            _iterencode({"x": [object()]}, ensure_ascii=False)

    def test_int_and_float_subclasses_render_as_their_json_base(self):
        content = {_Odd(5): _Odd(6), _OddFloat(2.5): [_OddFloat(0.5)]}

        text = _iterencode(content, ensure_ascii=False)

        assert text == json.dumps(content, separators=(",", ":"))
        assert text == '{"5":6,"2.5":[0.5]}'

    def test_key_text_of_subclasses_matches_json_dumps(self):
        assert json_key_text(_Odd(5)) == "5"
        assert json_key_text(_OddFloat(2.5)) == "2.5"


class TestRouteStatusAndPassthrough:
    @pytest.fixture
    def client(self) -> TestClient:
        router = APIRouter(route_class=GraphJSONRoute)

        @router.post("/made", status_code=201)
        async def made() -> dict:
            return {"ok": LONE_SURROGATE}

        @router.get("/own")
        async def own() -> dict:
            return PlainTextResponse("own body", status_code=202)

        @router.get("/gone")
        def gone() -> dict:
            raise HTTPException(410, "gone")

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_declared_status_code_is_kept(self, client):
        response = client.post("/made")

        assert response.status_code == 201
        assert response.json() == {"ok": LONE_SURROGATE}

    def test_a_returned_response_is_passed_through_untouched(self, client):
        response = client.get("/own")

        assert response.status_code == 202
        assert response.text == "own body"
        assert response.headers["content-type"].startswith("text/plain")

    def test_raised_http_exceptions_keep_their_status(self, client):
        response = client.get("/gone")

        assert response.status_code == 410
        assert response.json() == {"detail": "gone"}


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


class TestSharedAndCircularReferences:
    @pytest.fixture(autouse=True)
    def bounded_conversion(self, monkeypatch):
        # Every container the converter visits converts its keys, so a lost
        # memo (which would walk a cycle forever) trips this bound and fails
        # the test in place instead of hanging or growing without limit.
        calls = 0

        def bounded(key):
            nonlocal calls
            calls += 1
            if calls > 1000:
                raise AssertionError("conversion did not terminate on a cycle")
            return json_key_text(key)

        monkeypatch.setattr(serializers, "json_key_text", bounded)

    def test_a_dict_that_contains_itself_converts_to_a_copy_that_does(self):
        data: dict = {"n": 1}
        data["self"] = data

        converted = _to_json_native(data)

        assert converted is not data
        assert converted["self"] is converted and converted["n"] == 1

    def test_a_list_cycle_through_a_dict_terminates(self):
        items: list = []
        data = {"items": items}
        items.append(data)

        converted = _to_json_native(data)

        assert converted["items"][0] is converted

    def test_shared_references_stay_shared(self):
        shared = {"v": 1}

        converted = _to_json_native({"x": shared, "y": [shared]})

        assert converted["x"] is converted["y"][0]
        assert converted["x"] is not shared

    def test_serialize_to_json_reports_a_cycle_as_json_does(self):
        data: dict = {}
        data["self"] = data

        with pytest.raises(ValueError):
            serialize_to_json(data)


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
