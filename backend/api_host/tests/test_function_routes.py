"""Tests for the /functions endpoints (docs/USER_FUNCTION_CONTRACT.md §5, §11)."""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api_host.function_routes import register_function_routes
from backend.functions.runner import HardenedSubprocessRunner
from backend.functions.tests.test_service import Harness
from backend.runtime.authorization import GraphAuthorizationDecision
from backend.runtime.request_context import ACTOR_ID_HEADER

pytestmark = pytest.mark.usefixtures("mock_embedding_model")


class OutsiderHook:
    """Denies everything to the actor "outsider", allows everyone else."""

    def evaluate(self, context):
        allowed = context.actor.get("actor_id") != "outsider"
        return GraphAuthorizationDecision(allowed=allowed, mode="custom", source="test")


@pytest.fixture(scope="module")
def runner():
    return HardenedSubprocessRunner()


@pytest.fixture
def mock_embedding_model():
    import backend.core.vector_store as vs
    from backend.service.tests.conftest import MockSentenceTransformer

    original = vs._ensure_sentence_transformers
    vs._ensure_sentence_transformers = lambda: MockSentenceTransformer
    vs._SentenceTransformer = None
    yield
    vs._ensure_sentence_transformers = original
    vs._SentenceTransformer = None


@pytest.fixture
def setup(tmp_path, runner):
    from backend.core import GraphStorage

    storage = GraphStorage(
        json_path=str(tmp_path / "graph.json"),
        embeddings_path=str(tmp_path / "embeddings.pkl"),
    )
    root = tmp_path / "functions"
    root.mkdir()
    harness = Harness(storage, root, runner, hook=OutsiderHook())
    harness.add("count", "return {'ok': True}")
    service = harness.service
    app = FastAPI()
    register_function_routes(app, service, service._graph_service)
    return harness, TestClient(app)


def as_actor(actor):
    return {ACTOR_ID_HEADER: actor}


def wait(client, run_id, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/functions/runs/{run_id}", headers=as_actor("u")).json()
        if run["status"] != "running":
            return run
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_invoking_starts_a_run_that_can_be_read_back(setup):
    _, client = setup
    response = client.post(
        "/functions/count/invoke",
        json={"input": {}, "idempotency_key": "k1"},
        headers={
            **as_actor("u"),
            "x-correlation-id": "corr-9",
            "x-event-origin": "cli",
        },
    )
    assert response.status_code == 202
    started = response.json()
    assert started["status"] == "running"
    run = wait(client, started["id"])
    assert run["status"] == "succeeded"
    assert run["output"] == {"ok": True}
    assert (run["correlation_id"], run["origin"]) == ("corr-9", "cli")
    repeated = client.post(
        "/functions/count/invoke",
        json={"idempotency_key": "k1"},
        headers=as_actor("u"),
    )
    assert repeated.status_code == 200
    assert repeated.json()["id"] == started["id"]
    listed = client.get("/functions/runs?function_id=count", headers=as_actor("u"))
    assert [r["id"] for r in listed.json()] == [started["id"]]


def test_a_rejected_invocation_returns_its_record(setup):
    harness, client = setup
    harness.add("off", "return {}", enable=False)
    response = client.post("/functions/off/invoke", json={}, headers=as_actor("u"))
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert response.json()["error"]["code"] == "not_enabled"


def test_a_caller_without_read_access_is_refused_and_nothing_is_recorded(setup):
    harness, client = setup
    for method, path in [
        ("post", "/functions/count/invoke"),
        ("post", "/functions/absent/invoke"),
        ("get", "/functions"),
        ("get", "/functions/count"),
        ("get", "/functions/runs"),
        ("get", "/functions/runs/job-1"),
        ("get", "/functions/runner"),
        ("post", "/functions/runs/job-1/cancel"),
    ]:
        response = getattr(client, method)(path, headers=as_actor("outsider"))
        assert response.status_code == 403, path
    assert harness.store.list_jobs() == []


def test_unknown_functions_and_runs_are_not_found(setup):
    _, client = setup
    headers = as_actor("u")
    assert (
        client.post("/functions/absent/invoke", json={}, headers=headers).status_code
        == 404
    )
    assert client.get("/functions/absent", headers=headers).status_code == 404
    assert client.get("/functions/runs/job-none", headers=headers).status_code == 404
    assert (
        client.post("/functions/runs/job-none/cancel", headers=headers).status_code
        == 409
    )


def test_a_malformed_request_is_a_client_error(setup):
    _, client = setup
    response = client.post(
        "/functions/count/invoke", json={"idempotency_key": ""}, headers=as_actor("u")
    )
    assert response.status_code == 400


def test_the_listing_and_runner_status_are_readable(setup):
    _, client = setup
    listed = client.get("/functions", headers=as_actor("u")).json()
    assert [f["id"] for f in listed] == ["count"]
    status = client.get("/functions/runner", headers=as_actor("u")).json()
    assert status["level"] in ("full", "degraded")
    assert status["runtimes"] == ["python"]
