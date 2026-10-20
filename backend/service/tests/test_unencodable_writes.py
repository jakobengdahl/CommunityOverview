"""
A write the server acknowledges is durable, or it is refused.

A node id or metadata string holding a lone UTF-16 surrogate ("\\ud800", which
a JSON body may carry as an escape) has no UTF-8 form. The file backend wrote
its journal and graph.json as UTF-8, so such a write was acknowledged, failed
to persist in the background, and was lost at the next restart together with
every later write while the node existed. It is now written as a \\u escape,
which reads back as the same string.

Metadata nested close to where json's encoder and decoder stop recursing
cannot reliably be stored and read back, so it is refused where a write
enters, far above realistic depths (MAX_METADATA_DEPTH).

"Restart" below means a second storage opened over the same files after the
write queue drained, without a flush: what a crash right after the response
would leave.
"""

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agents.execution import InMemoryExecutionStore
from backend.core import Edge, GraphStorage, Node, NodeType
from backend.core.embedding_sidecar import parse_sidecar_bytes, serialize_sidecar_bytes
from backend.core.models import MAX_METADATA_DEPTH
from backend.federation.config import FederationFileConfig
from backend.federation.manager import FederationManager
from backend.service import GraphService, create_rest_router, import_service

SURROGATE = "\ud800"
SURROGATE_ID = f"x{SURROGATE}"


def _nested(levels: int) -> dict:
    """Metadata nesting `levels` objects below itself."""
    root: dict = {}
    current = root
    for _ in range(levels):
        current["a"] = {}
        current = current["a"]
    return root


def _nested_lists(levels: int) -> list:
    root: list = []
    current = root
    for _ in range(levels - 1):
        current.append([])
        current = current[0]
    return root


def _archive(document: Dict[str, Any]) -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("graph.json", json.dumps(document))
    return archive.getvalue()


def _merge(client, document: Dict[str, Any]):
    body = {"document": document, "policy": {"default": "overwrite"}}
    preview = _send(client, "POST", "/api/import/merge/preview", body)
    assert preview.status_code == 200, preview.text
    digest = preview.json()["plan_digest"]
    return _send(client, "POST", "/api/import/merge", {**body, "plan_digest": digest})


@pytest.fixture(autouse=True)
def _no_embedding_drain(monkeypatch):
    # The drain thread would race the restart on the graph files.
    monkeypatch.setattr(import_service, "_start_embedding_drain", lambda *a: None)


@pytest.fixture
def path(tmp_path) -> Path:
    return tmp_path / "graph.json"


@pytest.fixture
def storage(path):
    store = GraphStorage(json_path=str(path))
    yield store
    store.flush()
    store.shutdown_events()


@pytest.fixture
def client(storage) -> TestClient:
    service = GraphService(storage)
    app = FastAPI()
    app.include_router(
        create_rest_router(service, import_job_store=InMemoryExecutionStore()),
        prefix="/api",
    )
    return TestClient(app)


def _send(client, method: str, url: str, body: Dict[str, Any]):
    # json.dumps escapes the surrogate; the test client's own encoder would
    # refuse to send it.
    return client.request(
        method,
        url,
        content=json.dumps(body),
        headers={"content-type": "application/json"},
    )


def _restarted(storage, path) -> GraphStorage:
    storage._io_executor.submit(lambda: None).result()  # drained, not flushed
    return GraphStorage(json_path=str(path))


def _add(client, *nodes, edges=()):
    return _send(
        client, "POST", "/api/nodes", {"nodes": list(nodes), "edges": list(edges)}
    )


def _actor(node_id: str, **fields) -> Dict[str, Any]:
    return {"id": node_id, "type": "Actor", "name": "Actor", **fields}


class TestLoneSurrogatesAreDurable:
    def test_an_added_node_with_a_surrogate_id_survives_a_restart(
        self, client, storage, path
    ):
        assert _add(client, _actor(SURROGATE_ID)).status_code == 200

        assert SURROGATE_ID in _restarted(storage, path).nodes

    def test_a_surrogate_in_metadata_survives_a_restart_unchanged(
        self, client, storage, path
    ):
        metadata = {"note": f"a{SURROGATE}b", f"k{SURROGATE}": [SURROGATE]}
        assert _add(client, _actor("m", metadata=metadata)).status_code == 200

        assert _restarted(storage, path).nodes["m"].metadata == metadata

    def test_writes_after_a_surrogate_node_are_durable_too(self, client, storage, path):
        assert _add(client, _actor(SURROGATE_ID)).status_code == 200
        assert _add(client, _actor("later")).status_code == 200
        response = _send(
            client, "PATCH", "/api/nodes/later", {"updates": {"name": "Renamed"}}
        )
        assert response.status_code == 200

        restarted = _restarted(storage, path)
        assert restarted.nodes["later"].name == "Renamed"
        assert SURROGATE_ID in restarted.nodes

    def test_an_update_writing_a_surrogate_survives_a_restart(
        self, client, storage, path
    ):
        assert _add(client, _actor("u")).status_code == 200
        response = _send(
            client,
            "PATCH",
            "/api/nodes/u",
            {"updates": {"metadata": {"note": SURROGATE}}},
        )
        assert response.status_code == 200

        assert _restarted(storage, path).nodes["u"].metadata == {"note": SURROGATE}

    def test_an_edge_with_a_surrogate_in_metadata_survives_a_restart(
        self, client, storage, path
    ):
        edge = {"id": "e", "source": "a", "target": "b", "metadata": {"n": SURROGATE}}
        assert _add(client, _actor("a"), _actor("b"), edges=[edge]).status_code == 200

        assert _restarted(storage, path).edges["e"].metadata == {"n": SURROGATE}

    def test_a_whole_graph_import_with_a_surrogate_survives_a_restart(
        self, client, storage, path
    ):
        document = {"nodes": [_actor(SURROGATE_ID, metadata={"n": SURROGATE})]}
        response = _send(client, "POST", "/api/import", document)
        assert response.status_code == 200, response.text

        restarted = _restarted(storage, path)
        assert restarted.nodes[SURROGATE_ID].metadata == {"n": SURROGATE}

    def test_a_large_graph_whose_surrogate_comes_last_is_saved_whole(
        self, client, storage, path
    ):
        # The snapshot falls back after earlier nodes are already on disk.
        nodes = [_actor(f"n{i}", description="x" * 500) for i in range(100)]
        document = {"nodes": [*nodes, _actor(SURROGATE_ID)]}
        response = _send(client, "POST", "/api/import", document)
        assert response.status_code == 200, response.text

        assert len(GraphStorage(json_path=str(path)).nodes) == 101

    def test_an_archive_import_with_a_surrogate_survives_a_restart(
        self, client, storage, path
    ):
        archive = _archive({"nodes": [_actor(SURROGATE_ID)]})
        response = client.post(
            "/api/import/archive",
            files={"file": ("g.zip", archive, "application/zip")},
        )
        assert response.status_code == 200, response.text

        assert SURROGATE_ID in _restarted(storage, path).nodes

    def test_a_merge_import_with_a_surrogate_survives_a_restart(
        self, client, storage, path
    ):
        document = {"nodes": [_actor(SURROGATE_ID, metadata={"n": SURROGATE})]}
        response = _merge(client, document)
        assert response.status_code == 200, response.text

        restarted = _restarted(storage, path)
        assert restarted.nodes[SURROGATE_ID].metadata == {"n": SURROGATE}

    def test_the_mutation_history_keeps_the_event(self, client, storage, path):
        assert _add(client, _actor(SURROGATE_ID)).status_code == 200

        history = _restarted(storage, path).get_node_history(SURROGATE_ID)
        assert [record["entity_id"] for record in history] == [SURROGATE_ID]


class TestStoredSurrogatesStayUsable:
    """A graph may already hold one: a graph.json written with escapes."""

    @pytest.fixture
    def path(self, tmp_path) -> Path:
        path = tmp_path / "graph.json"
        node = Node(id=SURROGATE_ID, type=NodeType.ACTOR, name="Old")
        node.metadata = {"n": SURROGATE}
        path.write_text(json.dumps({"nodes": [node.to_dict()], "edges": []}))
        return path

    def test_it_loads_checkpoints_and_reloads(self, storage, path):
        assert storage.nodes[SURROGATE_ID].metadata == {"n": SURROGATE}
        storage.save().result()
        storage.flush()

        assert GraphStorage(json_path=str(path)).nodes[SURROGATE_ID].metadata == {
            "n": SURROGATE
        }

    def test_it_exports_as_json_and_as_an_archive(self, client):
        exported = client.get("/api/export")
        assert exported.status_code == 200
        assert [n["id"] for n in exported.json()["nodes"]] == [SURROGATE_ID]

        archive = client.get("/api/export/archive")
        assert archive.status_code == 200
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
            graph = json.loads(zf.read("graph.json").decode("utf-8"))
        assert [n["id"] for n in graph["nodes"]] == [SURROGATE_ID]


def test_ordinary_text_is_still_written_as_utf8(client, storage, path):
    assert _add(client, _actor("se", metadata={"ort": "Malmö"})).status_code == 200
    storage._io_executor.submit(lambda: None).result()
    journal = storage._persistence_backend.journal_path.read_bytes()
    history = path.with_name("graph.history.ndjson").read_bytes()
    storage.flush()

    for written in (journal, history, path.read_bytes()):
        assert "Malmö".encode("utf-8") in written


def test_the_embedding_sidecar_round_trips_a_surrogate_id():
    vectors = {SURROGATE_ID: [0.5, 0.25], "plain": [1.0, 0.0]}
    parsed = parse_sidecar_bytes(serialize_sidecar_bytes(vectors), "embeddings.bin")

    assert set(parsed) == set(vectors)


class TestDeepMetadata:
    def test_metadata_at_the_cap_is_accepted_and_durable(self, client, storage, path):
        metadata = _nested(MAX_METADATA_DEPTH)
        assert _add(client, _actor("d", metadata=metadata)).status_code == 200

        assert _restarted(storage, path).nodes["d"].metadata == metadata

    def test_the_cap_is_800_levels_exactly(self, client, storage):
        # Literal numbers, so raising the cap fails here and not only the
        # tests that are written relative to MAX_METADATA_DEPTH.
        assert MAX_METADATA_DEPTH == 800
        assert _add(client, _actor("ok", metadata=_nested(800))).status_code == 200

        refused = _add(client, _actor("no", metadata=_nested(801)))

        assert refused.status_code == 400
        assert "no" not in storage.nodes

    def test_history_and_edge_at_the_cap_survive_a_restart(self, client, storage, path):
        metadata = _nested(MAX_METADATA_DEPTH)
        edge = {"id": "e", "source": "d", "target": "o", "metadata": metadata}
        assert (
            _add(client, _actor("d", metadata=metadata), _actor("o"), edges=[edge])
        ).status_code == 200

        restarted = _restarted(storage, path)

        # History appends swallow failures, so check they actually landed.
        history = restarted.get_node_history("d")
        assert [entry["after"]["metadata"] for entry in history] == [metadata]
        assert restarted.edges["e"].metadata == metadata
        assert restarted.get_edge_history("e")

    def test_an_add_past_the_cap_is_refused_and_adds_nothing(self, client, storage):
        edge = {"id": "e", "source": "ok", "target": "deep"}
        response = _add(
            client,
            _actor("ok"),
            _actor("deep", metadata=_nested(MAX_METADATA_DEPTH + 1)),
            edges=[edge],
        )

        assert response.status_code == 400
        assert "nests deeper" in response.json()["detail"]
        assert not storage.nodes and not storage.edges

    def test_an_edge_past_the_cap_is_refused(self, client, storage):
        edge = {
            "id": "e",
            "source": "a",
            "target": "b",
            "metadata": _nested(MAX_METADATA_DEPTH + 1),
        }
        response = _add(client, _actor("a"), _actor("b"), edges=[edge])

        assert response.status_code == 400
        assert not storage.nodes and not storage.edges

    def test_a_folded_extra_field_counts_toward_the_depth(self, client, storage):
        # Unknown keys fold into metadata, one level down.
        node = _actor("x", extra=_nested(MAX_METADATA_DEPTH))

        assert _add(client, node).status_code == 400

    @pytest.mark.parametrize("merge", [False, True])
    def test_an_update_past_the_cap_is_refused_and_changes_nothing(
        self, client, storage, path, merge
    ):
        assert _add(client, _actor("u", metadata={"keep": 1})).status_code == 200
        response = _send(
            client,
            "PATCH",
            "/api/nodes/u",
            {
                "updates": {"metadata": {"k": _nested(MAX_METADATA_DEPTH)}},
                "metadata_merge": merge,
            },
        )

        assert response.status_code == 422
        assert "nests deeper" in response.json()["detail"]
        assert _restarted(storage, path).nodes["u"].metadata == {"keep": 1}

    def test_an_update_through_a_folded_extra_field_is_refused(self, client, storage):
        assert _add(client, _actor("u")).status_code == 200
        response = _send(
            client,
            "PATCH",
            "/api/nodes/u",
            {"updates": {"extra": _nested(MAX_METADATA_DEPTH)}},
        )

        assert response.status_code == 422
        assert storage.nodes["u"].metadata == {}

    def test_nesting_through_lists_counts_too(self, client, storage):
        at_cap = _actor("ok", metadata={"l": _nested_lists(MAX_METADATA_DEPTH)})
        past = _actor("no", metadata={"l": _nested_lists(MAX_METADATA_DEPTH + 1)})

        assert _add(client, at_cap).status_code == 200
        assert _add(client, past).status_code == 400

    def test_an_edge_update_past_the_cap_is_refused(self, client, storage):
        edge = {"id": "e", "source": "a", "target": "b"}
        assert _add(client, _actor("a"), _actor("b"), edges=[edge]).status_code == 200
        response = _send(
            client,
            "PATCH",
            "/api/edges/e",
            {"updates": {"metadata": _nested(MAX_METADATA_DEPTH + 1)}},
        )

        assert response.status_code == 422
        assert storage.edges["e"].metadata == {}

    def test_a_whole_graph_import_past_the_cap_is_refused(self, client, storage):
        assert _add(client, _actor("kept")).status_code == 200
        document = {"nodes": [_actor("d", metadata=_nested(MAX_METADATA_DEPTH + 1))]}
        response = _send(client, "POST", "/api/import", document)

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["error_code"] == "validation_failed"
        assert detail["errors"][0]["location"] == "nodes[0]"
        assert set(storage.nodes) == {"kept"}

    def test_an_import_with_a_deep_edge_is_refused(self, client, storage):
        document = {
            "nodes": [_actor("a"), _actor("b")],
            "edges": [
                {
                    "id": "e",
                    "source": "a",
                    "target": "b",
                    "metadata": _nested(MAX_METADATA_DEPTH + 1),
                }
            ],
        }
        response = _send(client, "POST", "/api/import", document)

        assert response.status_code == 422
        assert response.json()["detail"]["errors"][0]["location"] == "edges[0]"
        assert not storage.nodes

    def test_an_archive_import_past_the_cap_is_refused(self, client, storage):
        archive = _archive(
            {"nodes": [_actor("d", metadata=_nested(MAX_METADATA_DEPTH + 1))]}
        )
        response = client.post(
            "/api/import/archive",
            files={"file": ("g.zip", archive, "application/zip")},
        )

        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "validation_failed"
        assert not storage.nodes

    def test_a_merge_import_with_a_deep_edge_is_rejected_by_the_plan(
        self, client, storage
    ):
        document = {
            "nodes": [_actor("a"), _actor("b")],
            "edges": [
                {
                    "id": "e",
                    "source": "a",
                    "target": "b",
                    "metadata": _nested(MAX_METADATA_DEPTH + 1),
                }
            ],
        }
        response = _merge(client, document)

        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "import_merge_blocked"
        assert not storage.nodes and not storage.edges

    def test_a_merge_import_past_the_cap_is_rejected_by_the_plan(self, client, storage):
        document = {"nodes": [_actor("d", metadata=_nested(MAX_METADATA_DEPTH + 1))]}
        response = _merge(client, document)

        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "import_merge_blocked"
        assert "d" not in storage.nodes


class TestFederatedAdoptDepth:
    def _service(self, tmp_path, depth: int) -> GraphService:
        graph_file = tmp_path / "adopt.json"
        graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
        local = GraphStorage(str(graph_file))
        config = FederationFileConfig.model_validate(
            {
                "federation": {
                    "enabled": True,
                    "graphs": [
                        {
                            "graph_id": "remote",
                            "display_name": "Remote",
                            "trust_level": "partner",
                            "capabilities": {"allow_adopt": True},
                            "endpoints": {"graph_json_url": "https://r.example/g.json"},
                        }
                    ],
                }
            }
        )
        manager = FederationManager(config)
        remote = {
            "id": "r1",
            "type": "Actor",
            "name": "Remote",
            "metadata": _nested(depth),
        }
        cache_nodes, _ = manager._build_cache(config.federation.graphs[0], [remote], [])
        manager._cache["remote"].nodes = cache_nodes
        manager._cache["remote"].status = "healthy"
        return GraphService(local, federation_manager=manager)

    def test_a_node_nesting_past_the_cap_is_refused_and_adds_nothing(self, tmp_path):
        service = self._service(tmp_path, MAX_METADATA_DEPTH + 1)

        result = service.adopt_federated_node("federated::remote::r1")

        assert result["success"] is False
        assert "Error validating input" in result["message"]
        assert result["added_node_ids"] == [] and result["added_edge_ids"] == []
        assert not service._storage.nodes

    def test_a_node_nesting_to_the_cap_is_adopted(self, tmp_path):
        service = self._service(tmp_path, MAX_METADATA_DEPTH)

        result = service.adopt_federated_node("federated::remote::r1")

        assert result["success"] is True, result.get("message")


class TestStoredDeepMetadataStaysUsable:
    """Written before the cap: loads, exports, and takes other updates."""

    @pytest.fixture
    def path(self, tmp_path) -> Path:
        path = tmp_path / "graph.json"
        deep = _nested(MAX_METADATA_DEPTH + 10)
        node = Node(id="deep", type=NodeType.ACTOR, name="Deep")
        node.metadata = deep
        other = Node(id="other", type=NodeType.ACTOR, name="Other")
        edge = Edge(id="e", source="deep", target="other", metadata=deep)
        path.write_text(
            json.dumps(
                {
                    "nodes": [node.to_dict(), other.to_dict()],
                    "edges": [edge.to_dict()],
                }
            )
        )
        return path

    def test_it_loads_exports_and_takes_a_rename(self, client, storage, path):
        assert client.get("/api/export").status_code == 200
        response = _send(
            client, "PATCH", "/api/nodes/deep", {"updates": {"name": "Renamed"}}
        )
        assert response.status_code == 200

        restarted = _restarted(storage, path)
        assert restarted.nodes["deep"].name == "Renamed"
        assert restarted.nodes["deep"].metadata == _nested(MAX_METADATA_DEPTH + 10)

    def test_its_edge_takes_a_label_change(self, client, storage, path):
        response = _send(
            client, "PATCH", "/api/edges/e", {"updates": {"label": "Relabelled"}}
        )
        assert response.status_code == 200

        assert _restarted(storage, path).edges["e"].label == "Relabelled"
