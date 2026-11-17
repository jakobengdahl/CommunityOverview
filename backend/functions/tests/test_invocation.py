# covers: backend/functions/invocation.py, backend/functions/routes.py, backend/functions/cli.py, backend/agents/run_history.py
"""Invocation admission, concurrency, durable outcomes and trusted identity."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from backend.agents.execution import (
    ExecutionKind,
    ExecutionState,
    RetryPolicy,
    SqliteExecutionStore,
)
from backend.agents.run_history import function_run_to_dict
from backend.functions import invocation
from backend.functions.runner import FunctionRunResult, RunnerProbe
from backend.functions.routes import register_function_routes
from backend.runtime.authorization import GraphAuthorizationDecision


@pytest.fixture
def host(tmp_path, monkeypatch):
    directory = tmp_path / "example"
    directory.mkdir()
    manifest = {
        "contract_version": "1",
        "id": "example",
        "version": "1.0.0",
        "name": "Example",
        "runtime": "python",
        "entry_point": "main.py",
        "triggers": [{"kind": "manual"}],
        "grants": ["graph:write"],
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
    }
    (directory / "function.json").write_text(json.dumps(manifest))
    (directory / "main.py").write_text("pass")
    store = SqliteExecutionStore(
        tmp_path / "history.db", retry_policy=RetryPolicy(max_attempts=1)
    )
    hook = Mock()
    hook.evaluate.return_value = GraphAuthorizationDecision(allowed=True)
    runner = Mock()
    runner.probe.return_value = RunnerProbe("full", (), ("python",))
    execute = Mock(
        return_value=FunctionRunResult("succeeded", output={"done": True}, wall_ms=2)
    )
    monkeypatch.setattr(invocation, "run_function", execute)
    service = invocation.FunctionInvocationService(
        SimpleNamespace(authorization_hook=hook),
        store,
        root=tmp_path,
        enabled={"example": ["graph:write"]},
        runner=runner,
    )
    yield service, execute, manifest, directory
    store.close()


def invoke(service, **kwargs):
    return service.invoke(
        "example", {}, actor_binding={"actor_id": "operator"}, **kwargs
    )


@pytest.mark.parametrize(
    "failure", ["manifest", "disabled", "grant", "access", "input", "isolation"]
)
def test_admission_failure_never_runs_code(host, failure):
    service, execute, manifest, directory = host
    if failure == "manifest":
        manifest["retry"] = {}
    elif failure == "disabled":
        service.enabled.clear()
    elif failure == "grant":
        service.enabled["example"] = []
    elif failure == "access":
        service.graph_service.authorization_hook.evaluate.return_value = (
            GraphAuthorizationDecision(allowed=False)
        )
    elif failure == "input":
        manifest["input_schema"] = {"required": ["node_id"]}
    else:
        service.isolation_minimum = "full"
        service.runner.probe.return_value = RunnerProbe(
            "degraded", ("filesystem",), ("python",)
        )
    (directory / "function.json").write_text(json.dumps(manifest))
    job = invoke(service)
    assert job.state == ExecutionState.CANCELLED
    assert job.started_at is None
    assert function_run_to_dict(job)["status"] == "rejected"
    execute.assert_not_called()
    assert len(service.store.list_jobs(kind=ExecutionKind.FUNCTION)) == 1


def test_busy_rejects_without_queue_and_same_delivery_replays(host):
    service, execute, _, _ = host
    entered, release = Event(), Event()

    def run(*args):
        entered.set()
        assert release.wait(5)
        return FunctionRunResult("succeeded", output={})

    execute.side_effect = run
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(invoke, service, idempotency_key="delivery")
        try:
            assert entered.wait(5)
            running = invoke(service, idempotency_key="delivery")
            assert running.state == ExecutionState.RUNNING
            busy = invoke(service, idempotency_key="other")
            assert busy.result["error"]["code"] == "busy"
        finally:
            release.set()
        assert future.result().id == running.id
    assert execute.call_count == 1
    assert invoke(service, idempotency_key="delivery").id == running.id
    assert invoke(service, idempotency_key="fresh").state == ExecutionState.SUCCEEDED
    assert execute.call_count == 2


@pytest.mark.parametrize(
    "status,code",
    [("succeeded", None), ("failed", "protocol_error"), ("timed_out", "timeout")],
)
def test_terminal_history_survives_restart_without_retry(host, status, code):
    service, execute, _, directory = host
    execute.return_value = FunctionRunResult(
        status, code, output={} if code is None else None, wall_ms=4
    )
    job = invoke(service, idempotency_key="delivery", correlation_id="correlation")
    record = function_run_to_dict(job)
    assert job.kind == ExecutionKind.FUNCTION
    assert job.idempotency_key == "fn:example:delivery"
    assert job.attempts == 1 and job.lease_expiry is None
    assert record["status"] == status
    assert record["actor"]["actor_id"] == "operator"
    assert record["trigger"] == "manual" and record["duration_ms"] >= 0
    assert record["usage"]["wall_ms"] == 4
    assert record["correlation_id"] == "correlation"
    assert job.finished_at is not None
    reopened = SqliteExecutionStore(
        directory.parent / "history.db", retry_policy=RetryPolicy(max_attempts=1)
    )
    try:
        assert function_run_to_dict(reopened.get(job.id)) == record
        assert reopened.claim_next("worker") is None
    finally:
        reopened.close()


def test_rest_uses_authenticated_scope_and_refuses_body_identity(host):
    service, execute, _, _ = host
    app = FastAPI()

    @app.middleware("http")
    async def bind(request, call_next):
        request.scope["communityoverview.authenticated_actor"] = {
            "actor_id": "operator",
            "auth_source": "basic",
        }
        return await call_next(request)

    router = APIRouter()
    register_function_routes(router, service)
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post(
            "/functions/example/invoke",
            json={"input": {}},
            headers={"X-Actor-Id": "forged"},
        )
        assert response.status_code == 200
        assert response.json()["actor"]["actor_id"] == "operator"
        broker = execute.call_args.args[2]
        assert broker.context.authorization_inputs["actor_id"] == "operator"
        assert "headers" not in broker.context.authorization_inputs
        assert (
            client.post(
                "/functions/example/invoke", json={"actor_id": "forged"}
            ).status_code
            == 422
        )

    anonymous = FastAPI()
    anonymous.include_router(router)
    with TestClient(anonymous) as client:
        response = client.post(
            "/functions/example/invoke",
            json={"input": {}},
            headers={"x-communityoverview-actor-id": "operator"},
        )
        assert response.json()["error"]["code"] == "access_denied"
    assert execute.call_count == 1


def test_cli_calls_manual_route(host, monkeypatch):
    from backend.functions import cli

    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.post.return_value.json.return_value = {"status": "succeeded"}
    monkeypatch.setattr(cli.httpx, "Client", Mock(return_value=client))
    monkeypatch.setattr(cli.httpx, "NetRCAuth", Mock())
    cli.main(["example", "--idempotency-key", "delivery"])
    assert client.post.call_args.args[0].endswith("/functions/example/invoke")
    assert client.post.call_args.kwargs["headers"] == {"Idempotency-Key": "delivery"}
    assert client.post.call_args.kwargs["json"] == {"input": {}, "correlation_id": None}


def test_crashed_run_is_never_claimed_or_executed_again(host):
    service, execute, _, directory = host
    execute.side_effect = SystemExit
    with pytest.raises(SystemExit):
        invoke(service, idempotency_key="crashed")
    reopened = SqliteExecutionStore(
        directory.parent / "history.db", retry_policy=RetryPolicy(max_attempts=1)
    )
    try:
        assert reopened.claim_next("worker") is None
        assert reopened.recover_stale() == []
        assert (
            invoke(service, idempotency_key="crashed").state == ExecutionState.RUNNING
        )
        assert invoke(service, idempotency_key="new").result["error"]["code"] == "busy"
        assert execute.call_count == 1
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime", "node"),
        ("version", "1.0.0-01"),
        ("entry_point", "../outside.py"),
        ("contract_version", "2"),
        ("limits", {"timeout_seconds": 121}),
        ("limits", {"memory_mib": True}),
        ("triggers", [{"kind": "webhook"}]),
        ("grants", ["graph:delete"]),
        ("input_schema", {"$ref": "https://example.invalid/schema"}),
    ],
)
def test_invalid_manifest_is_rejected_and_its_delivery_key_is_retained(
    host, field, value
):
    service, execute, manifest, directory = host
    invalid = {**manifest, field: value}
    (directory / "function.json").write_text(json.dumps(invalid))
    rejected = invoke(service, idempotency_key="delivery")
    assert rejected.result["error"]["code"] == "invalid_manifest"
    execute.assert_not_called()
    (directory / "function.json").write_text(json.dumps(manifest))
    assert invoke(service, idempotency_key="delivery").id == rejected.id
    assert invoke(service, idempotency_key="fixed").state == ExecutionState.SUCCEEDED


def test_invalid_output_fails_and_attribution_obeys_actor_erasure(host):
    service, execute, manifest, directory = host
    manifest["output_schema"] = {"required": ["missing"]}
    (directory / "function.json").write_text(json.dumps(manifest))
    job = invoke(service)
    assert function_run_to_dict(job)["error"]["code"] == "invalid_output"
    assert service.store.erase_actor("operator", "erased") == 1
    assert (
        function_run_to_dict(service.store.get(job.id))["actor"]["actor_id"] == "erased"
    )
