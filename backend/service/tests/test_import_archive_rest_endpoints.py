"""
REST-level tests for GET /export/archive and POST /import/archive, wiring
backend.service.import_service / graph_archive through backend.service.rest_api's
router factory. See docs/adr/0007-vector-aware-export-archive.md.

Also carries a small regression check that the pre-existing GET /export and
POST /import endpoints are unaffected by this change (the full behavioural
coverage for those two lives in test_import_rest_endpoints.py, which this PR
does not modify and which continues to pass unchanged).
"""

import io
import os
import time
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agents.execution import InMemoryExecutionStore
from backend.core import GraphStorage, Node, NodeType
from backend.service import GraphService, create_rest_router
from backend.service.graph_archive import (
    EMBEDDINGS_MEMBER,
    GRAPH_MEMBER,
    MANIFEST_MEMBER,
)


@pytest.fixture
def import_job_store() -> InMemoryExecutionStore:
    return InMemoryExecutionStore()


@pytest.fixture
def storage(temp_dir) -> GraphStorage:
    json_path = os.path.join(temp_dir, "test.json")
    storage = GraphStorage(json_path=json_path)
    storage.add_nodes(
        [
            Node(id="n1", type=NodeType.ACTOR, name="Alice"),
            Node(id="n2", type=NodeType.ACTOR, name="Bob"),
        ],
        [],
    )
    return storage


@pytest.fixture
def client(
    storage: GraphStorage, import_job_store: InMemoryExecutionStore
) -> TestClient:
    service = GraphService(storage)
    router = create_rest_router(service, import_job_store=import_job_store)
    app = FastAPI()
    app.include_router(router, prefix="/api/graph")
    return TestClient(app)


def _wait_for_terminal(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/graph/import/{job_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in ("succeeded", "failed", "cancelled"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"import job {job_id} did not reach a terminal state in time")


class TestExportArchiveEndpoint:
    def test_returns_a_zip_with_graph_embeddings_and_manifest(self, client: TestClient):
        response = client.get("/api/graph/export/archive")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "attachment" in response.headers["content-disposition"]

        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            names = set(zf.namelist())
            assert names == {GRAPH_MEMBER, EMBEDDINGS_MEMBER, MANIFEST_MEMBER}
            import json

            document = json.loads(zf.read(GRAPH_MEMBER))
            assert {n["id"] for n in document["nodes"]} == {"n1", "n2"}
            manifest = json.loads(zf.read(MANIFEST_MEMBER))
            assert manifest["embedding_model"]
            assert manifest["embedding_dimension"] == 384
            assert manifest["node_count"] == 2


class TestImportArchiveEndpoint:
    def test_round_trip_export_then_import_restores_vectors(
        self, client: TestClient, tmp_path
    ):
        export_response = client.get("/api/graph/export/archive")
        assert export_response.status_code == 200
        archive_bytes = export_response.content

        # A second, empty instance imports the archive exported above.
        second_json_path = str(tmp_path / "second.json")
        second_storage = GraphStorage(json_path=second_json_path)
        second_service = GraphService(second_storage)
        second_store = InMemoryExecutionStore()
        second_router = create_rest_router(
            second_service, import_job_store=second_store
        )
        second_app = FastAPI()
        second_app.include_router(second_router, prefix="/api/graph")
        second_client = TestClient(second_app)

        response = second_client.post(
            "/api/graph/import/archive",
            files={"file": ("graph-export.zip", archive_bytes, "application/zip")},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["graph_replaced"] is True
        assert body["node_count"] == 2
        assert body["job_id"] is None
        assert body["embeddings_status"] == "restored"
        assert body["archive_compatible"] is True

        nodes_response = second_client.get("/api/graph/nodes/n1")
        assert nodes_response.status_code == 200

    def test_an_incompatible_manifest_still_imports_and_reports_regeneration(
        self, client: TestClient, tmp_path
    ):
        export_response = client.get("/api/graph/export/archive")
        archive_bytes = export_response.content

        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
            import json

            manifest = json.loads(zf.read(MANIFEST_MEMBER))
            manifest["embedding_model"] = "a-different-model-entirely"
            graph_bytes = zf.read(GRAPH_MEMBER)
            embeddings_bytes = zf.read(EMBEDDINGS_MEMBER)

        import hashlib

        manifest["checksums"] = {
            GRAPH_MEMBER: f"sha256:{hashlib.sha256(graph_bytes).hexdigest()}",
            EMBEDDINGS_MEMBER: f"sha256:{hashlib.sha256(embeddings_bytes).hexdigest()}",
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(GRAPH_MEMBER, graph_bytes)
            zf.writestr(EMBEDDINGS_MEMBER, embeddings_bytes)
            zf.writestr(MANIFEST_MEMBER, json.dumps(manifest))
        mismatched_archive = buffer.getvalue()

        second_storage = GraphStorage(json_path=str(tmp_path / "second.json"))
        second_service = GraphService(second_storage)
        second_store = InMemoryExecutionStore()
        second_router = create_rest_router(
            second_service, import_job_store=second_store
        )
        second_app = FastAPI()
        second_app.include_router(second_router, prefix="/api/graph")
        second_client = TestClient(second_app)

        response = second_client.post(
            "/api/graph/import/archive",
            files={"file": ("graph-export.zip", mismatched_archive, "application/zip")},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["graph_replaced"] is True
        assert body["archive_compatible"] is False
        assert body["embeddings_status"] == "queued"
        assert "a-different-model-entirely" in body["embeddings_message"]

        job = _wait_for_terminal(second_client, body["job_id"])
        assert job["status"] == "succeeded"

    def test_a_plain_graph_only_zip_still_imports_successfully(self, tmp_path):
        import json

        document = {
            "nodes": [{"id": "n1", "type": "Actor", "name": "Alice"}],
            "edges": [],
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(GRAPH_MEMBER, json.dumps(document))

        storage = GraphStorage(json_path=str(tmp_path / "target.json"))
        service = GraphService(storage)
        store = InMemoryExecutionStore()
        router = create_rest_router(service, import_job_store=store)
        app = FastAPI()
        app.include_router(router, prefix="/api/graph")
        client = TestClient(app)

        response = client.post(
            "/api/graph/import/archive",
            files={"file": ("plain.zip", buffer.getvalue(), "application/zip")},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["node_count"] == 1
        assert body["embeddings_status"] == "queued"

    def test_a_corrupt_zip_is_rejected_with_422_and_leaves_the_graph_untouched(
        self, client: TestClient
    ):
        response = client.post(
            "/api/graph/import/archive",
            files={"file": ("bad.zip", b"not a zip at all", "application/zip")},
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["error_code"] == "archive_integrity_failed"

        nodes_response = client.get("/api/graph/nodes/n1")
        assert nodes_response.status_code == 200


class TestPlainExportAndImportEndpointsAreUnaffected:
    """A quick regression check, not a full re-test of PR #664's own suite
    (see test_import_rest_endpoints.py for that)."""

    def test_plain_export_shape_is_unchanged(self, client: TestClient):
        response = client.get("/api/graph/export")

        assert response.status_code == 200
        body = response.json()
        assert {n["id"] for n in body["nodes"]} == {"n1", "n2"}
        assert "archive_bytes" not in body

    def test_plain_import_still_works_and_is_independent_of_the_archive_path(
        self, client: TestClient
    ):
        document = {
            "nodes": [{"id": "m1", "type": "Actor", "name": "Carol"}],
            "edges": [],
        }
        response = client.post("/api/graph/import", json=document)

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["node_count"] == 1
        assert "archive_compatible" not in body
