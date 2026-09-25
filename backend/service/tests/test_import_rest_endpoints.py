"""
REST-level tests for POST /import and GET /import/{job_id}, wiring
backend.service.import_service through backend.service.rest_api's router
factory. See docs/adr/0006-graph-import-replace-mode.md.
"""

import os
import threading
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


class TestImportReturnsBeforeEmbeddingsComplete:
    def test_the_response_returns_well_before_slow_embedding_generation_finishes(
        self,
        temp_dir,
        import_job_store: InMemoryExecutionStore,
        monkeypatch,
    ):
        """POST /import must not block on embedding generation (ADR 0006,
        section 2: "commit is synchronous; embeddings are not"). Proven here
        with an encode step that cannot finish until the test lets it: the
        HTTP response must come back while that step is still held, and the
        job only reaches a terminal state once it is released.

        A gate rather than a sleep: timing the response against a sleeping
        encode step failed under CI load (1.387s against a 2.0s delay) with
        the endpoint behaving correctly. An endpoint that waits on the encode
        step returns only after the gate's timeout has run out, by which point
        the step has finished and the first assertion below fails. Two loose
        clocks remain, and only a stall of the test thread can trip either:
        the gate's 30 s timeout, which must not run out between the POST and
        the immediate status check or the stub releases itself; and the 10 s
        terminal wait after the release. Short of such a stall the outcome
        does not depend on how fast the runner is."""
        json_path = os.path.join(temp_dir, "test.json")
        storage = GraphStorage(json_path=json_path)
        service = GraphService(storage)
        router = create_rest_router(service, import_job_store=import_job_store)
        app = FastAPI()
        app.include_router(router, prefix="/api/graph")
        client = TestClient(app)

        # Only bounds how long a blocking endpoint hangs the test before it
        # fails; a correct endpoint never waits on it.
        gate_timeout_seconds = 30.0
        release = threading.Event()
        finished = threading.Event()

        def _gated_compute(nodes):
            release.wait(timeout=gate_timeout_seconds)
            finished.set()
            return {node.id: [0.1, 0.2, 0.3] for node in nodes}

        monkeypatch.setattr(
            storage.vector_store, "compute_node_embeddings", _gated_compute
        )

        try:
            response = client.post("/api/graph/import", json=_valid_document())

            assert not finished.is_set(), (
                "POST /import returned only after embedding generation "
                "finished - it must return before embeddings finish, not "
                "block on them"
            )
            assert response.status_code == 200
            body = response.json()
            assert body["success"] is True
            assert body["embeddings_status"] == "queued"

            # Immediately after the response, the job must still be
            # non-terminal - the held step genuinely has not completed yet.
            immediate = client.get(f"/api/graph/import/{body['job_id']}").json()
            assert immediate["status"] in ("queued", "running")
        finally:
            release.set()

        job = _wait_for_terminal(client, body["job_id"], timeout=10.0)
        assert job["status"] == "succeeded"
        assert job["embeddings_status"] == "succeeded"
        assert finished.is_set()
