# covers: backend/functions/webhook.py, backend/functions/run_files.py
"""Webhook admission, bound identity and run-scoped webhook files."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock

import anyio

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.agents.execution import (
    ExecutionJob,
    ExecutionKind,
    ExecutionState,
    RetryPolicy,
    SqliteExecutionStore,
)
from backend.functions import invocation, run_files, webhook
from backend.functions.runner import FunctionRunResult, RunnerProbe
from backend.functions.token_store import WebhookTokenStore
from backend.functions.webhook import register_webhook_routes
from backend.runtime.authorization import GraphAuthorizationDecision


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    package = tmp_path / "example"
    package.mkdir()
    (package / "main.py").write_text("pass")
    (package / "function.json").write_text(
        json.dumps(
            {
                "contract_version": "2",
                "id": "example",
                "version": "1.0.0",
                "name": "Example",
                "runtime": "python",
                "entry_point": "main.py",
                "triggers": [{"kind": "webhook"}],
                "grants": [],
                "input_schema": {"type": "object", "required": ["value"]},
            }
        )
    )
    store = WebhookTokenStore(tmp_path / "private.db")
    token = store.mint("example", "graph-a", "test")
    job = ExecutionJob(
        agent_id="example",
        idempotency_key="job",
        kind=ExecutionKind.FUNCTION,
        state=ExecutionState.RUNNING,
    )
    service = SimpleNamespace(
        root=tmp_path,
        enabled={"example": []},
        invoke=Mock(return_value=job),
        replay_webhook=Mock(return_value=None),
    )

    def authorize(request, graph_id, target):
        if request.headers.get("admin") != graph_id:
            raise HTTPException(403)

    def existing(function_id):
        return store.get(function_id)

    app = FastAPI()
    router = APIRouter()
    register_webhook_routes(router, service, store, authorize, existing)
    app.include_router(router)
    monkeypatch.setenv("FUNCTION_WEBHOOKS_ENABLED", "true")
    return TestClient(app), service, store, token


def test_settings_require_bound_graph_admin_and_follow_token(endpoint):
    client, service, store, token = endpoint
    path = "/functions/example/webhook-settings"
    settings = {"enabled": True, "allow_write": True}
    assert (
        client.put(path, json=settings, headers={"admin": "graph-b"}).status_code == 403
    )
    assert (
        client.put(path, json=settings, headers={"admin": "graph-a"}).status_code == 200
    )
    assert service.webhook_policy.get("example", "graph-a") == (True, True)
    row = store.get("example")
    store.revoke("example", "graph-a", row["id"])
    store.mint("example", "graph-a", "new")
    assert service.webhook_policy.get("example", "graph-a") == (False, False)


def test_auth_limits_input_and_bound_actor(endpoint, monkeypatch):
    client, service, _, token = endpoint
    path = "/functions/example/webhook"
    auth = {
        "Authorization": f"Bearer {token}",
        "x-communityoverview-actor-id": "spoof",
        "x-communityoverview-graph-id": "other",
    }
    assert client.post(path, json={"value": 1}, headers=auth).status_code == 404
    client.put(
        path + "-settings",
        json={"enabled": True, "allow_write": False},
        headers={"admin": "graph-a"},
    )
    assert client.post(path + "?token=" + token, json={"value": 1}).status_code == 401
    assert client.post(path, content=b"x" * 65537, headers=auth).status_code == 413
    assert client.post(path, json={}, headers=auth).status_code == 422
    service.invoke.assert_not_called()
    response = client.post(
        path,
        json={"value": 1, "graph_id": "graph-b"},
        headers={**auth, "Idempotency-Key": "key"},
    )
    assert response.status_code == 200
    args = service.invoke.call_args
    assert args.kwargs["actor_binding"] == {
        "actor_id": "function_webhook:example",
        "actor_type": "function_webhook",
        "auth_source": "webhook",
        "graph_id": "graph-a",
    }
    assert args.kwargs["idempotency_key"] == "key"
    assert args.args[1]["graph_id"] == "graph-b"
    assert service.replay_webhook.call_args.args[2] == args.kwargs["actor_binding"]
    service.replay_webhook.return_value = service.invoke.return_value
    manifest_path = service.root / "example" / "function.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["input_schema"]["required"] = ["new_field"]
    manifest_path.write_text(json.dumps(manifest))
    replay = client.post(
        path, json={"value": 1}, headers={**auth, "Idempotency-Key": "key"}
    )
    assert replay.status_code == 200 and replay.json()["id"] == response.json()["id"]
    service.invoke.assert_called_once()
    service.replay_webhook.return_value = None
    service.invoke.return_value.result = {"status": "failed", "error": {"code": "busy"}}
    busy = client.post(path, json={"new_field": 1}, headers=auth)
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "busy"
    monkeypatch.setattr(service.webhook_policy, "admit", lambda _: False)
    assert client.post(path, content=b"invalid", headers=auth).status_code == 429
    monkeypatch.setenv("FUNCTION_WEBHOOKS_ENABLED", "false")
    assert client.post(path, content=b"invalid", headers=auth).status_code == 404


@pytest.mark.asyncio
async def test_rejections_do_not_receive_body(endpoint, monkeypatch):
    client, service, _, token = endpoint
    app = client.app
    client.put(
        "/functions/example/webhook-settings",
        json={"enabled": True, "allow_write": False},
        headers={"admin": "graph-a"},
    )

    async def receive():
        raise AssertionError("body was read")

    async def send(message):
        pass

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/functions/example/webhook",
        "raw_path": b"/functions/example/webhook",
        "query_string": b"",
        "headers": [(b"content-type", b"multipart/form-data; boundary=x")],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    await app(scope.copy(), receive, send)
    scope["headers"].append((b"authorization", f"Bearer {token}".encode()))
    service.replay_webhook.return_value = ExecutionJob(
        agent_id="example",
        idempotency_key="busy",
        kind=ExecutionKind.FUNCTION,
        state=ExecutionState.CANCELLED,
        result={"status": "rejected", "error": {"code": "busy"}},
    )
    messages = []

    async def capture(message):
        messages.append(message)

    await app(scope.copy(), receive, capture)
    assert messages[0]["status"] == 409
    service.invoke.assert_not_called()
    service.replay_webhook.return_value = None
    monkeypatch.setattr(service.webhook_policy, "admit", lambda _: False)
    await app(scope.copy(), receive, send)
    monkeypatch.setenv("FUNCTION_WEBHOOKS_ENABLED", "false")
    await app(scope.copy(), receive, send)


def test_rate_limit_is_shared_by_policy_instances(endpoint, monkeypatch):
    from backend.functions.webhook import WebhookPolicy

    _, service, store, _ = endpoint
    first = service.webhook_policy
    second = WebhookPolicy(store)
    now = [1000.0]
    monkeypatch.setattr("backend.functions.webhook.time.time", lambda: now[0])
    assert first.admit("example")
    now[0] += 59
    assert all(first.admit("example") for _ in range(59))
    assert not second.admit("example")
    assert second.admit("other")
    now[0] += 1
    assert second.admit("example")
    assert not first.admit("example")
    now[0] += 59
    assert first.admit("example")


@pytest.fixture
def files_host(tmp_path, monkeypatch):
    package = tmp_path / "example"
    package.mkdir()
    (package / "main.py").write_text("pass")
    (package / "function.json").write_text(
        json.dumps(
            {
                "contract_version": "3",
                "id": "example",
                "version": "1.0.0",
                "name": "Example",
                "runtime": "python",
                "entry_point": "main.py",
                "triggers": [{"kind": "webhook"}],
                "grants": ["graph:write"],
                "input_schema": {"type": "object", "required": ["value"]},
            }
        )
    )
    tokens = WebhookTokenStore(tmp_path / "private.db")
    token = tokens.mint("example", "graph-a", "test")
    hook = Mock()
    hook.evaluate.return_value = GraphAuthorizationDecision(allowed=True)
    graph = SimpleNamespace(authorization_hook=hook)
    runner = Mock()
    runner.probe.return_value = RunnerProbe("full", (), ("python",))
    history = SqliteExecutionStore(
        tmp_path / "history.db", retry_policy=RetryPolicy(max_attempts=1)
    )
    service = invocation.FunctionInvocationService(
        graph,
        history,
        root=tmp_path,
        enabled={"example": ["graph:write"]},
        runner=runner,
    )
    created = []

    class Recorded(run_files.RunFiles):
        def __init__(self, parent):
            super().__init__(parent)
            created.append(self._directory)

    monkeypatch.setattr(webhook, "RunFiles", Recorded)
    execute = Mock(return_value=FunctionRunResult("succeeded", output={}))
    monkeypatch.setattr(invocation, "run_function", execute)
    monkeypatch.setenv("FUNCTION_WEBHOOKS_ENABLED", "true")
    app = FastAPI()
    router = APIRouter()
    register_webhook_routes(router, service, tokens, Mock(), tokens.get)
    app.include_router(router)
    service.webhook_policy.set(
        "example",
        "graph-a",
        tokens.get("example")["id"],
        SimpleNamespace(enabled=True, allow_write=False),
    )

    def post(key=None, files=None, value=None):
        headers = {"Authorization": f"Bearer {token}"}
        if key:
            headers["Idempotency-Key"] = key
        return client.post(
            "/functions/example/webhook",
            data={"input": value or json.dumps({"value": 1})},
            files=files or {"a": ("notes.txt", b"hello webhook", "text/plain")},
            headers=headers,
        )

    def running():
        return [
            job
            for job in history.list_jobs(agent_id="example")
            if job.state == ExecutionState.RUNNING
        ]

    with TestClient(app) as client:
        yield SimpleNamespace(
            service=service,
            graph=graph,
            tokens=tokens,
            root=tmp_path / "function-run-files",
            execute=execute,
            created=created,
            post=post,
            running=running,
        )
    history.close()


@pytest.mark.parametrize("fails", [False, True])
def test_files_are_run_inputs_outside_envelope_and_deleted(files_host, fails):
    host = files_host
    seen = {}

    def run(runner, spec, broker, envelope):
        seen["input"] = envelope["invocation"]["input"]
        seen["package"] = set(spec.files)
        seen["present"] = os.path.isdir(host.created[0])
        seen["files"] = [(f.filename, f.size) for f in broker.context.run_files.list()]
        if fails:
            raise RuntimeError("the run fails")
        return FunctionRunResult("succeeded", output={})

    host.execute.side_effect = run
    response = host.post(
        files={
            "a": ("notes.txt", b"hello webhook", "text/plain"),
            "b": ("blob.bin", b"\x00\x01\x02", "application/octet-stream"),
        }
    )
    assert response.status_code == 200
    assert response.json()["status"] == ("failed" if fails else "succeeded")
    # Bytes and paths stay out of the envelope and the package.
    assert seen["input"] == {"value": 1} and seen["package"] == {
        "main.py",
        "function.json",
    }
    assert seen["files"] == [("notes.txt", 13), ("blob.bin", 3)]
    assert seen["present"] and not os.path.exists(host.created[0])


def test_concurrent_replays_claim_once_and_refusals_store_nothing(
    files_host, monkeypatch
):
    host = files_host
    entered, release = Event(), Event()

    def run(*args):
        entered.set()
        assert release.wait(5)
        return FunctionRunResult("succeeded", output={})

    host.execute.side_effect = run
    # Every replay passes the first idempotency lookup before any of them claims.
    barrier = Barrier(3)
    lookup = host.service.replay_webhook
    calls = []

    def racing_lookup(*args):
        calls.append(args)
        if len(calls) <= 3:
            barrier.wait(5)
        return lookup(*args)

    monkeypatch.setattr(host.service, "replay_webhook", racing_lookup)
    with ThreadPoolExecutor(max_workers=3) as pool:
        posts = [pool.submit(host.post, "delivery") for _ in range(3)]
        assert entered.wait(5)
        assert host.post("other").status_code == 409
        release.set()
        responses = [post.result() for post in posts]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(host.created) == 1 and not os.path.exists(host.created[0])
    host.execute.assert_called_once()
    assert host.post("delivery").json()["status"] == "succeeded"
    host.graph.authorization_hook.evaluate.return_value = GraphAuthorizationDecision(
        allowed=False
    )
    assert host.post("denied").json()["error"]["code"] == "access_denied"
    assert len(host.created) == 1


@pytest.mark.parametrize(
    "body,status,code",
    [
        (
            {"files": {"a": ("big.txt", b"x" * 14, "text/plain")}},
            413,
            "input_too_large",
        ),
        (
            {
                "files": [
                    ("f", (f"{i}.txt", b"x", "text/plain"))
                    for i in range(run_files.MAX_RUN_FILES + 1)
                ]
            },
            413,
            "input_too_large",
        ),
        ({"value": "[1]"}, 422, "invalid_input"),
        ({"value": "{"}, 422, "invalid_input"),
    ],
)
def test_refused_body_records_rejection_and_deletes_files(
    files_host, monkeypatch, body, status, code
):
    host = files_host
    monkeypatch.setattr(run_files, "MAX_RUN_FILE_BYTES", 13)
    assert host.post("key", **body).status_code == status
    assert host.created and not any(os.path.exists(d) for d in host.created)
    host.execute.assert_not_called()
    replay = host.post("key").json()
    assert replay["status"] == "rejected" and replay["error"]["code"] == code


def test_slow_body_holds_no_run_and_retry_succeeds(files_host, monkeypatch):
    host = files_host
    read = webhook._read_multipart
    seen = {}

    async def slow(request, boundary, files):
        value = await read(request, boundary, files)
        seen["running"] = host.running()
        await anyio.sleep(5)
        return value

    monkeypatch.setattr(webhook, "READ_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(webhook, "_read_multipart", slow)
    assert host.post("key").status_code == 408
    # The busy flag is claimed only after the body arrived.
    assert seen["running"] == [] and host.running() == []
    assert not os.path.exists(host.created[0])
    monkeypatch.setattr(webhook, "_read_multipart", read)
    assert host.post("key").json()["status"] == "succeeded"


def test_file_store_failure_claims_nothing(files_host, monkeypatch):
    host = files_host
    recorded = webhook.RunFiles
    monkeypatch.setattr(webhook, "RunFiles", Mock(side_effect=OSError))
    with pytest.raises(OSError):
        host.post("key")
    host.execute.assert_not_called()
    assert host.running() == [] and webhook._receiving == {}
    monkeypatch.setattr(webhook, "RunFiles", recorded)
    assert host.post("key").json()["status"] == "succeeded"


def test_startup_removes_leftover_run_files(files_host):
    host = files_host
    leftover = host.root / "function-run-crashed"
    leftover.mkdir(parents=True)
    (leftover / "file-1").write_bytes(b"left behind")
    register_webhook_routes(
        APIRouter(), host.service, host.tokens, Mock(), host.tokens.get
    )
    assert host.root.is_dir() and list(host.root.iterdir()) == []
