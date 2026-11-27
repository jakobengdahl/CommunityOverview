# covers: backend/functions/invocation.py
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
    InMemoryExecutionStore,
    ExecutionState,
    RetryPolicy,
    SqliteExecutionStore,
)
from backend.agents.run_history import function_run_to_dict
from backend.functions import invocation
from backend.functions.runner import FunctionRunResult, RunnerProbe
from backend.functions.routes import register_function_routes
from backend.runtime.authorization import (
    GraphAccessNarrowing,
    GraphAuthorizationDecision,
)


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


@pytest.mark.parametrize("trigger", ["manual", "webhook"])
def test_busy_rejects_without_queue_and_same_delivery_replays(host, trigger):
    service, execute, manifest, directory = host
    if trigger == "webhook":
        manifest["contract_version"] = "2"
        manifest["triggers"].append({"kind": "webhook"})
        (directory / "function.json").write_text(json.dumps(manifest))
        service.webhook_policy = Mock()
        service.webhook_policy.get.return_value = (True, True)

    def call(**kwargs):
        return service.invoke(
            "example",
            {},
            trigger=trigger,
            actor_binding={"actor_id": "operator"},
            **kwargs,
        )

    entered, release = Event(), Event()

    def run(*args):
        entered.set()
        assert release.wait(5)
        return FunctionRunResult("succeeded", output={})

    execute.side_effect = run
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call, idempotency_key="delivery")
        try:
            assert entered.wait(5)
            running = call(idempotency_key="delivery")
            assert running.state == ExecutionState.RUNNING
            if trigger == "webhook":
                binding = {"actor_id": "operator"}
                assert (
                    service.replay_webhook("example", "delivery", binding).id
                    == running.id
                )
                rejected = service.replay_webhook("example", "preflight", binding)
                assert rejected.result["error"]["code"] == "busy"
                assert (
                    service.replay_webhook("example", "preflight", binding).id
                    == rejected.id
                )
            busy = call(idempotency_key="other")
            assert busy.result["error"]["code"] == "busy"
        finally:
            release.set()
        assert future.result().id == running.id
    assert execute.call_count == 1
    assert call(idempotency_key="delivery").id == running.id
    if trigger == "webhook":
        assert (
            service.replay_webhook("example", "delivery", {"actor_id": "operator"}).id
            == running.id
        )
        assert (
            service.replay_webhook("example", "missing", {"actor_id": "operator"})
            is None
        )
    assert call(idempotency_key="fresh").state == ExecutionState.SUCCEEDED
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
    assert client.post.call_args.kwargs["headers"] == {
        "Idempotency-Key": "delivery",
        "X-Requested-With": "function-cli",
    }
    assert client.post.call_args.kwargs["json"] == {"input": {}, "correlation_id": None}

    # Exercise the host's real Basic-auth write guard with the CLI request.
    from backend.api_host.config import AppConfig
    from backend.api_host.middleware import add_auth_middleware

    app = FastAPI()
    router = APIRouter()
    register_function_routes(router, host[0])
    app.include_router(router, prefix="/api")
    add_auth_middleware(
        app, AppConfig(auth_enabled=True, auth_username="admin", auth_password="secret")
    )
    with TestClient(app) as http:
        response = http.post(
            "/api/functions/example/invoke",
            auth=("admin", "secret"),
            **client.post.call_args.kwargs,
        )
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    host[1].assert_called_once()


def test_crashed_run_is_never_claimed_or_executed_again(host):
    service, execute, _, directory = host
    execute.side_effect = SystemExit
    with pytest.raises(SystemExit):
        invoke(service, idempotency_key="crashed")
    reopened = SqliteExecutionStore(
        directory.parent / "history.db", retry_policy=RetryPolicy(max_attempts=1)
    )
    try:
        service.store = reopened
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
        ("version", "1٢.0.0"),
        ("version", "1.1٢.0"),
        ("version", "1.0.1٢"),
        ("entry_point", "../outside.py"),
        ("contract_version", "6"),
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
    service.runner.probe.assert_not_called()
    (directory / "function.json").write_text(json.dumps(manifest))
    assert invoke(service, idempotency_key="delivery").id == rejected.id
    assert invoke(service, idempotency_key="fixed").state == ExecutionState.SUCCEEDED


@pytest.mark.parametrize("version", ["1", "2", "3"])
@pytest.mark.parametrize("target", [None, "graph"])
def test_invalid_graph_manifest_replays_before_validation(host, version, target):
    service, execute, manifest, directory = host
    manifest.update(contract_version=version, limits={"timeout_seconds": 121})
    if target is not None:
        manifest["write_target"] = target
    (directory / "function.json").write_text(json.dumps(manifest))
    rejected = invoke(service, idempotency_key="delivery")
    assert rejected.result["error"]["code"] == "invalid_manifest"
    assert invoke(service, idempotency_key="delivery").id == rejected.id
    binding = {"actor_id": "operator"}
    assert service.replay_webhook("example", "delivery", binding).id == rejected.id
    assert (
        service.refuse(
            "example",
            "invalid_manifest",
            actor_binding=binding,
            idempotency_key="delivery",
        ).id
        == rejected.id
    )
    execute.assert_not_called()


def test_write_target_change_during_admission_is_refused(host, monkeypatch):
    service, execute, manifest, directory = host
    manifest.update(contract_version="4", write_target="graph")
    path = directory / "function.json"
    path.write_text(json.dumps(manifest))
    refusal = service._refusal

    def change_target(*args):
        path.write_text(json.dumps({**manifest, "write_target": "session"}))
        return refusal(*args)

    monkeypatch.setattr(service, "_refusal", change_target)
    with pytest.raises(PermissionError, match="binding changed"):
        invoke(service, idempotency_key="delivery")
    assert not service.store.list_jobs(kind=ExecutionKind.FUNCTION)
    execute.assert_not_called()


@pytest.mark.parametrize("target", ["directory", "manifest", "entry", "package"])
def test_package_paths_cannot_escape_the_function_directory(host, tmp_path, target):
    _, _, _, directory = host
    outside = tmp_path / "outside"
    outside.write_text("pass")
    if target == "directory":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(ValueError, match="Invalid function directory"):
            invocation.load_manifest(tmp_path, "alias")
        return
    path = (
        directory
        / {"manifest": "function.json", "entry": "main.py", "package": "extra.py"}[
            target
        ]
    )
    path.unlink(missing_ok=True)
    path.symlink_to(outside)
    with pytest.raises(ValueError):
        invocation.load_manifest(tmp_path, "example")


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


@pytest.mark.parametrize("adapter", ["memory", "sqlite-memory"])
def test_enabled_functions_require_persistent_shared_history(host, adapter):
    service, execute, _, _ = host
    store = (
        InMemoryExecutionStore(retry_policy=RetryPolicy(max_attempts=1))
        if adapter == "memory"
        else SqliteExecutionStore(":memory:", retry_policy=RetryPolicy(max_attempts=1))
    )
    try:
        with pytest.raises(ValueError, match="file-backed SQLite"):
            invocation.FunctionInvocationService(
                service.graph_service, store, enabled=service.enabled
            )
        disabled = invocation.FunctionInvocationService(service.graph_service, store)
        disabled.enabled.update(service.enabled)
        with pytest.raises(ValueError, match="file-backed SQLite"):
            invoke(disabled)
        assert store.list_jobs() == []
        execute.assert_not_called()
    finally:
        if adapter == "sqlite-memory":
            store.close()


def test_independent_history_connections_share_busy_and_deduplication(host):
    service, execute, _, directory = host
    other_store = SqliteExecutionStore(
        directory.parent / "history.db", retry_policy=RetryPolicy(max_attempts=1)
    )
    other = invocation.FunctionInvocationService(
        service.graph_service,
        other_store,
        root=service.root,
        enabled=service.enabled,
        runner=service.runner,
    )
    entered, release = Event(), Event()

    def run(*args):
        entered.set()
        assert release.wait(5)
        return FunctionRunResult("succeeded", output={})

    execute.side_effect = run
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(invoke, service, idempotency_key="shared")
            try:
                assert entered.wait(5)
                replay = invoke(other, idempotency_key="shared")
                assert replay.state == ExecutionState.RUNNING
                assert invoke(other).result["error"]["code"] == "busy"
            finally:
                release.set()
            assert future.result().id == replay.id
        assert invoke(other, idempotency_key="shared").id == replay.id
        assert execute.call_count == 1
    finally:
        other_store.close()


def test_replay_survives_auth_transport_change_and_retention_redaction(host):
    service, execute, _, _ = host
    service.graph_service.authorization_hook.evaluate.return_value = (
        GraphAuthorizationDecision(
            allowed=True,
            graph_access=GraphAccessNarrowing(
                enabled=True, allow_local_graph=False, include_graph_ids=("g1",)
            ),
        )
    )
    job = service.invoke(
        "example",
        {},
        idempotency_key="delivery",
        actor_binding={"actor_id": "operator", "auth_source": "basic"},
    )
    assert job.state == ExecutionState.SUCCEEDED
    assert (
        service.invoke(
            "example",
            {},
            idempotency_key="delivery",
            actor_binding={"actor_id": "operator", "auth_source": "session"},
        ).id
        == job.id
    )
    with pytest.raises(PermissionError):
        service.invoke(
            "example",
            {},
            idempotency_key="delivery",
            actor_binding={"actor_id": "other"},
        )
    assert service.store.redact([job.id]) == 1
    replay = invoke(service, idempotency_key="delivery")
    assert replay.id == job.id and replay.redacted_at is not None
    assert replay.result["output"] is None
    service.graph_service.authorization_hook.evaluate.return_value = (
        GraphAuthorizationDecision(allowed=False)
    )
    with pytest.raises(PermissionError):
        invoke(service, idempotency_key="delivery")
    assert execute.call_count == 1


def test_invalid_function_id_cannot_alias_another_functions_delivery(host):
    service, execute, _, _ = host
    job = invoke(service, idempotency_key="def:ghi")
    with pytest.raises(ValueError, match="Invalid function id"):
        service.invoke(
            "example:def",
            {},
            idempotency_key="ghi",
            actor_binding={"actor_id": "other"},
        )
    assert service.store.list_jobs(kind=ExecutionKind.FUNCTION) == [job]
    assert execute.call_count == 1


def test_enqueue_conflict_cannot_expose_another_actors_run(host, monkeypatch):
    service, execute, _, _ = host
    job = invoke(service, idempotency_key="delivery")
    monkeypatch.setattr(service.store, "list_jobs", lambda **kwargs: [])
    monkeypatch.setattr(service.store, "enqueue", lambda candidate: job)
    with pytest.raises(PermissionError):
        service.invoke(
            "example",
            {},
            idempotency_key="delivery",
            actor_binding={"actor_id": "other"},
        )
    assert execute.call_count == 1


def test_webhook_version_and_write_switch_use_bound_actor(host):
    service, execute, manifest, directory = host
    manifest["contract_version"] = "2"
    manifest["triggers"] = [{"kind": "manual"}, {"kind": "webhook"}]
    (directory / "function.json").write_text(json.dumps(manifest))
    service.webhook_policy = Mock()
    service.webhook_policy.get.return_value = (True, False)
    binding = {
        "actor_id": "example",
        "actor_type": "function_webhook",
        "graph_id": "graph-a",
    }
    result = service.invoke("example", {}, trigger="webhook", actor_binding=binding)
    assert result.state == ExecutionState.SUCCEEDED
    context = execute.call_args.args[2].context
    assert context.authorization_inputs["graph_id"] == "graph-a"
    assert context.authorization_inputs["actor_type"] == "function_webhook"
    assert not context.grant_enabled("graph:write")
    service.webhook_policy.get.return_value = (True, True)
    assert context.grant_enabled("graph:write")
    service.webhook_policy.get.return_value = (False, True)
    assert not context.grant_enabled("graph:write")


def test_session_binding_stages_and_replay_requires_the_same_target_and_session(
    host, tmp_path
):
    from backend.core import GraphStorage
    from backend.core.storage_backends import FileGraphPersistenceBackend
    from backend.runtime.session_context import SessionRefused, bind_session
    from backend.service.tests.test_staged_writes import World, _seed

    service, execute, manifest, directory = host
    storage = GraphStorage(
        str(tmp_path / "graph.json"),
        persistence_backend=FileGraphPersistenceBackend(
            tmp_path / "graph.json", staged_sessions=True
        ),
    )
    _seed(storage)
    world = World(storage)
    service.graph_service = world.service
    sid, other = world.staged_session(), world.staged_session()
    manifest.update(contract_version="4", write_target="session")
    (directory / "function.json").write_text(json.dumps(manifest))

    def run(_runner, _spec, broker, _envelope):
        def call(arguments):
            return broker.handle_request(
                {
                    "id": 1,
                    "params": {"name": "graph.update_node", "arguments": arguments},
                }
            )

        assert "error" in call(
            {"node_id": "a", "updates": {"name": "Bad"}, "session_id": other}
        )
        assert "result" in call({"node_id": "a", "updates": {"name": "Staged"}})
        return FunctionRunResult("succeeded", output={}, wall_ms=1)

    execute.side_effect = run
    with bind_session(sid):
        job = invoke(service, idempotency_key="one", correlation_id="corr")
        assert invoke(service, idempotency_key="one").id == job.id
    assert storage.get_node("a").name == "Alpha"
    assert world.layer(other) == {}
    assert world.layer(sid)[("node", "a")]["function_origin"] == {
        "function_id": "example",
        "function_version": "1.0.0",
        "run_id": job.id,
        "correlation_id": "corr",
    }
    with bind_session(other), pytest.raises(PermissionError):
        invoke(service, idempotency_key="one")
    with pytest.raises(ValueError):
        invoke(service, idempotency_key="one")
    assert service.store.redact([job.id]) == 1
    manifest["write_target"] = "graph"
    (directory / "function.json").write_text(json.dumps(manifest))
    with pytest.raises(PermissionError):
        invoke(service, idempotency_key="one")
    with bind_session(sid), pytest.raises(SessionRefused):
        invoke(service, idempotency_key="new")
    assert len(service.store.list_jobs(kind=ExecutionKind.FUNCTION)) == 1
    assert world.service.merge_staged_changes(sid, "merge")["success"]
    assert storage.get_node("a").name == "Staged"
    storage.shutdown_events()


@pytest.mark.parametrize("version", ["1", "2", "3"])
def test_session_target_requires_version_four(host, version):
    service, execute, manifest, directory = host
    original = invoke(service, idempotency_key="existing")
    manifest.update(contract_version=version, write_target="session")
    (directory / "function.json").write_text(json.dumps(manifest))
    with pytest.raises(PermissionError):
        invoke(service, idempotency_key="existing")
    with pytest.raises(PermissionError):
        service.replay_webhook("example", "existing", {"actor_id": "operator"})
    with pytest.raises(PermissionError):
        service.refuse(
            "example",
            "invalid_manifest",
            actor_binding={"actor_id": "operator"},
            idempotency_key="existing",
        )
    assert invoke(service).result["error"]["code"] == "invalid_manifest"
    assert execute.call_count == 1
    assert service.store.get(original.id).state == ExecutionState.SUCCEEDED


def test_session_admission_refuses_unauthorized_or_direct_sessions(host):
    from backend.runtime.session_context import bind_session

    service, execute, manifest, directory = host
    manifest.update(contract_version="4", write_target="session")
    (directory / "function.json").write_text(json.dumps(manifest))
    service.graph_service.session_gate = Mock()
    service.graph_service.session_gate.named_mode.return_value = "direct"
    with bind_session("session"), pytest.raises(ValueError):
        invoke(service, idempotency_key="key")
    service.graph_service.session_gate.named_mode.return_value = "staged"
    service.graph_service.authorization_hook.evaluate.side_effect = lambda context: (
        GraphAuthorizationDecision(allowed=context.graph_operation != "stage")
    )
    with bind_session("session"), pytest.raises(PermissionError):
        invoke(service, idempotency_key="key")
    context = service.graph_service.authorization_hook.evaluate.call_args.args[0]
    assert (
        context.action,
        context.target,
        context.graph_operation,
        context.session_id,
    ) == ("mutate", "invoke_function", "stage", "session")
    assert service.store.list_jobs(kind=ExecutionKind.FUNCTION) == []
    execute.assert_not_called()


def test_http_session_refusal_does_not_consume_graph_delivery_key(host):
    from backend.api_host.session_binding import add_session_binding_middleware
    from backend.runtime.session_context import SESSION_ID_HEADER

    service, execute, manifest, directory = host
    app = FastAPI()

    @app.middleware("http")
    async def bind_actor(request, call_next):
        request.scope["communityoverview.authenticated_actor"] = {
            "actor_id": "operator"
        }
        return await call_next(request)

    router = APIRouter()
    register_function_routes(router, service)
    app.include_router(router, prefix="/api")
    add_session_binding_middleware(app, api_prefix="/api", is_retired=lambda _: False)
    headers = {SESSION_ID_HEADER: "session", "Idempotency-Key": "delivery"}
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/functions/example/invoke", json={}, headers=headers
            ).status_code
            == 400
        )
        assert service.store.list_jobs(kind=ExecutionKind.FUNCTION) == []
        headers.pop(SESSION_ID_HEADER)
        response = client.post(
            "/api/functions/example/invoke", json={}, headers=headers
        )
        assert response.status_code == 200
        manifest.update(contract_version="4", write_target="session")
        (directory / "function.json").write_text(json.dumps(manifest))
        service.graph_service.session_gate = Mock()
        service.graph_service.session_gate.named_mode.return_value = "staged"
        response = client.post(
            "/api/functions/example/invoke",
            json={},
            headers={SESSION_ID_HEADER: "session", "Idempotency-Key": "staged"},
        )
        assert response.status_code == 200
        assert execute.call_args.args[2].context.session_id == "session"
    assert execute.call_count == 2


def test_shared_trigger_forwards_context_and_refuses_excess_depth(host, monkeypatch):
    service, execute, _, _ = host
    refusal = service._refusal
    monkeypatch.setattr(
        service,
        "_refusal",
        lambda function_id, binding, trigger, input: refusal(
            function_id, binding, "manual", input
        ),
    )
    binding = {"actor_id": "operator"}
    accepted = service.invoke(
        "example",
        {},
        trigger="future",
        actor_binding=binding,
        correlation_id="chain",
        chain_depth=3,
    )
    envelope = execute.call_args.args[3]
    assert accepted.state == ExecutionState.SUCCEEDED
    assert envelope["invocation"]["trigger"] == "future"
    assert envelope["invocation"]["chain_depth"] == 3
    assert envelope["origin"]["event_correlation_id"] == "chain"
    rejected = service.invoke(
        "example",
        {},
        trigger="future",
        actor_binding=binding,
        correlation_id="chain",
        chain_depth=4,
    )
    assert rejected.result["error"]["code"] == "chain_depth_exceeded"
    assert rejected.payload["chain_depth"] == 4
    assert rejected.correlation_id == "chain"
    assert execute.call_count == 1


@pytest.mark.parametrize("remove_schedule", [False, True])
def test_schedule_slot_dispatch_uses_shared_admission(host, tmp_path, remove_schedule):
    from datetime import datetime, timedelta, timezone
    from backend.functions.schedule_slots import ScheduleSlots

    service, execute, manifest, directory = host
    manifest["contract_version"] = "5"
    manifest["triggers"] = [
        {"kind": "schedule", "interval_seconds": 60}, {"kind": "manual"}
    ]
    (directory / "function.json").write_text(json.dumps(manifest))
    now = datetime.now(timezone.utc)
    slots = ScheduleSlots(tmp_path / "slots.db")
    try:
        slot = slots.enqueue_manifest(tmp_path, "example", now)
        manual = service.invoke(
            "example",
            {},
            actor_binding={"actor_id": "operator"},
            idempotency_key=f"{slot.idempotency_key}:attempt:1",
        )
        assert manual.state == ExecutionState.SUCCEEDED
        result = slots.dispatch(
            service,
            "w",
            actor_binding={"actor_id": "operator"},
            now=now,
            lease_seconds=10,
        )
        envelope = execute.call_args.args[3]
        assert result.state == ExecutionState.SUCCEEDED
        assert envelope["invocation"]["trigger"] == "schedule"
        assert envelope["invocation"]["chain_depth"] == 0
        assert envelope["origin"]["event_correlation_id"] == slot.correlation_id
        assert (
            slots.enqueue_manifest(tmp_path, "example", now).state
            == ExecutionState.SUCCEEDED
        )
        service.graph_service.authorization_hook.evaluate.return_value = (
            GraphAuthorizationDecision(
                allowed=True,
                graph_access=GraphAccessNarrowing(
                    enabled=True, allow_local_graph=False, include_graph_ids=("other",)
                ),
            )
        )
        denied = service.invoke(
            "example",
            {},
            trigger="schedule",
            actor_binding={"actor_id": "scheduler", "graph_id": "graph"},
        )
        assert denied.result["error"]["code"] == "access_denied"
        assert execute.call_count == 2
        # A queued slot must be refused and finished if its manifest changes
        # to a session target after enqueue, and new slots must be rejected.
        next_time = now + timedelta(seconds=1)
        slots.enqueue_manifest(tmp_path, "example", next_time)
        manifest["write_target"] = "session"
        if remove_schedule:
            manifest["triggers"] = [{"kind": "manual"}]
        (directory / "function.json").write_text(json.dumps(manifest))
        with pytest.raises(ValueError):
            slots.enqueue_manifest(tmp_path, "example", next_time)
        refused = slots.dispatch(
            service,
            "w",
            actor_binding={"actor_id": "operator"},
            now=next_time,
            lease_seconds=10,
        )
        assert refused.state == ExecutionState.CANCELLED
        assert refused.result["error"]["code"] == "invalid_manifest"
        assert slots.enqueue("example", next_time).state == ExecutionState.DEAD_LETTER
        assert execute.call_count == 2
        del manifest["write_target"]
        manifest["triggers"] = [{"kind": "schedule", "interval_seconds": 60}]
        for version in ("1", "2", "3", "4"):
            manifest["contract_version"] = version
            (directory / "function.json").write_text(json.dumps(manifest))
            with pytest.raises(ValueError):
                slots.enqueue_manifest(tmp_path, "example", now)
    finally:
        slots.close()


def test_schedule_reconciles_failed_run_after_restart(host, tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from backend.functions.schedule_slots import ScheduleSlots
    from backend.functions.scheduler import FunctionScheduler

    service, execute, manifest, directory = host
    manifest["contract_version"] = "5"
    manifest["triggers"] = [{"kind": "schedule", "interval_seconds": 60}]
    manifest["retry"] = {
        "max_attempts": 2,
        "base_seconds": 0,
        "factor": 2,
        "max_seconds": 300,
    }
    (directory / "function.json").write_text(json.dumps(manifest))
    execute.side_effect = [
        FunctionRunResult("failed", code="runner_lost", wall_ms=1),
        FunctionRunResult("succeeded", output={}, wall_ms=1),
    ]
    now = datetime.now(timezone.utc)
    path = tmp_path / "slots.db"
    slots = ScheduleSlots(path)
    slot = slots.enqueue_manifest(tmp_path, "example", now)
    monkeypatch.setattr(
        slots, "_finish_result", Mock(side_effect=RuntimeError("crash"))
    )
    with pytest.raises(RuntimeError, match="crash"):
        slots.dispatch(
            service,
            "w",
            actor_binding={"actor_id": "operator"},
            now=now,
            lease_seconds=10,
        )
    slots.close()
    slots = ScheduleSlots(path)
    scheduler = FunctionScheduler(service, slots, {"example": "graph"})
    scheduler.tick(now)
    scheduler.tick(now)
    recovered = slots.enqueue_manifest(tmp_path, "example", now)
    assert recovered.id == slot.id
    assert recovered.state == ExecutionState.SUCCEEDED
    assert recovered.attempts == 2
    assert execute.call_count == 2
    scheduler.stop()
