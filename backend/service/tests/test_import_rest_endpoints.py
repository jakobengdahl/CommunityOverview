"""
REST-level tests for POST /import and GET /import/{job_id}, wiring
backend.service.import_service through backend.service.rest_api's router
factory. See docs/adr/0006-graph-import-replace-mode.md.
"""

import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agents.execution import InMemoryExecutionStore
from backend.core import GraphStorage, Node, NodeType
from backend.service import GraphService, create_rest_router


def _valid_document():
    return {
        "nodes": [
            {"id": "n1", "type": "Actor", "name": "Alice"},
            {"id": "n2", "type": "Initiative", "name": "Project X"},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2", "type": "BELONGS_TO"}],
    }


@pytest.fixture
def import_job_store() -> InMemoryExecutionStore:
    return InMemoryExecutionStore()


@pytest.fixture
def client(temp_dir, import_job_store: InMemoryExecutionStore) -> TestClient:
    json_path = os.path.join(temp_dir, "test.json")
    storage = GraphStorage(json_path=json_path)
    storage.add_nodes([Node(id="old-1", type=NodeType.ACTOR, name="Pre-existing")], [])
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


class TestImportEndpoint:
    def test_post_import_replaces_the_graph(self, client: TestClient):
        response = client.post("/api/graph/import", json=_valid_document())

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["graph_replaced"] is True
        assert body["node_count"] == 2
        assert body["edge_count"] == 1
        assert "job_id" in body

    def test_post_import_with_invalid_document_returns_422_and_leaves_graph(
        self, client: TestClient
    ):
        bad_document = {"nodes": [{"id": "n1", "type": "NotARealType", "name": "x"}]}

        response = client.post("/api/graph/import", json=bad_document)

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["error_code"] == "validation_failed"
        assert detail["errors"]

        # The pre-existing node from the client fixture must still be there.
        nodes_response = client.get("/api/graph/nodes/old-1")
        assert nodes_response.status_code == 200

    def test_import_job_reaches_a_terminal_state(self, client: TestClient):
        post_response = client.post("/api/graph/import", json=_valid_document())
        job_id = post_response.json()["job_id"]

        job = _wait_for_terminal(client, job_id)

        assert job["id"] == job_id
        assert job["kind"] == "import"
        assert job["status"] == "succeeded"

    def test_get_unknown_import_job_returns_404(self, client: TestClient):
        response = client.get("/api/graph/import/does-not-exist")
        assert response.status_code == 404

    def test_list_import_jobs(self, client: TestClient):
        post_response = client.post("/api/graph/import", json=_valid_document())
        job_id = post_response.json()["job_id"]
        _wait_for_terminal(client, job_id)

        response = client.get("/api/graph/import")

        assert response.status_code == 200
        jobs = response.json()
        assert any(job["id"] == job_id for job in jobs)
