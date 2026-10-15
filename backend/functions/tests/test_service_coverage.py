"""Edges of invocation, records and settings the first review round left
untested (USER_FUNCTION_CONTRACT §4.3, §5, §8, §11)."""

import json
import os
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agents.execution import (
    ExecutionJob,
    ExecutionKind,
    ExecutionState,
    InMemoryExecutionStore,
    RetryPolicy,
    SqliteExecutionStore,
)
from backend.agents.run_history import AgentRunRecorder
from backend.api_host.function_routes import register_function_routes
from backend.functions.manifest import (
    MAX_FILES,
    MAX_MANIFEST_BYTES,
    MAX_PACKAGE_BYTES,
    load_function,
    validate_manifest,
)
from backend.functions.runner import RunnerUnavailable
from backend.functions.service import MAX_INPUT_BYTES
from backend.functions.settings import FunctionsSettings
from backend.functions.tests.test_service import (
    Harness,
    manifest,
    _rejected,
    finished,
    invoke,
)
from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GraphAuthorizationDecision,
)
from backend.runtime.request_context import ACTOR_ID_HEADER


@pytest.fixture(scope="module")
def runner():
    from backend.functions.runner import HardenedSubprocessRunner

    return HardenedSubprocessRunner()


@pytest.fixture
def harness(storage, tmp_path, runner):
    return Harness(storage, tmp_path, runner)


class RaisingHook:
    def evaluate(self, context):
        raise RuntimeError("hook down")


class ReadOnlyHook:
    def evaluate(self, context):
        return GraphAuthorizationDecision(
            allowed=context.action != GRAPH_ACTION_MUTATE, mode="custom", source="t"
        )


# --- authorization (G1) ---------------------------------------------------------------


def _client(harness):
    service = harness.service
    app = FastAPI()
    register_function_routes(app, service, service._graph_service)
    return TestClient(app), service


def test_a_failing_hook_denies_every_route_and_every_invocation(
    storage, tmp_path, runner
):
    harness = Harness(storage, tmp_path, runner, hook=RaisingHook())
    harness.add("count", "return {}")
    client, service = _client(harness)
    for method, path in [
        ("get", "/functions"),
        ("get", "/functions/runs"),
        ("post", "/functions/count/invoke"),
        ("post", "/functions/runs/job-1/cancel"),
    ]:
        assert getattr(client, method)(path).status_code == 403, path
    _rejected(invoke(service, "count"), "access_denied")


def test_cancelling_needs_write_access(storage, tmp_path, runner):
    harness = Harness(storage, tmp_path, runner, hook=ReadOnlyHook())
    harness.add("sleeper", "time.sleep(60)")
    client, service = _client(harness)
    started = invoke(service, "sleeper")
    assert client.post(f"/functions/runs/{started['id']}/cancel").status_code == 403
    service.cancel_run(started["id"])
    finished(service, started)


def test_the_service_checks_read_access_before_anything_about_the_function(
    storage, tmp_path, runner
):
    class NoReads:
        def evaluate(self, context):
            return GraphAuthorizationDecision(allowed=False, mode="c", source="t")

    harness = Harness(storage, tmp_path, runner, hook=NoReads())
    harness.add("bad", "return {}", triggers=[])
    run = invoke(harness.service, "bad")
    _rejected(run, "access_denied")
    assert "read" in run["error"]["message"]


def test_an_invocation_through_the_api_acts_as_the_caller(harness):
    harness.add("count", "return {}")
    client, service = _client(harness)
    started = client.post(
        "/functions/count/invoke", json={}, headers={ACTOR_ID_HEADER: "caller-9"}
    ).json()
    run = finished(service, started)
    assert run["actor"]["actor"]["actor_id"] == "caller-9"


# --- admission (G2) -------------------------------------------------------------------


def test_a_runtime_the_host_lacks_is_an_invalid_manifest(harness):
    harness.add("js-fn", "return {}", runtime="node", entry_point="main.js")
    (harness.root / "js-fn" / "main.js").write_text("")
    run = invoke(harness.service, "js-fn")
    _rejected(run, "invalid_manifest")
    assert "node" in run["error"]["message"]


def test_a_function_without_grants_still_needs_enabling(harness):
    harness.add("pure", "return {}", grants=[], enable=False)
    _rejected(invoke(harness.service, "pure"), "not_enabled")


def test_every_declared_grant_must_be_enabled(harness):
    harness.add("writer", "return {}", grants=["graph:read", "graph:write"])
    harness.enabled["writer"] = ["graph:write"]
    _rejected(invoke(harness.service, "writer"), "not_enabled")


@pytest.mark.parametrize("value", [[], "text", 3])
def test_input_must_be_an_object_even_without_a_schema(harness, value):
    harness.add("no-input", "return {}")
    _rejected(invoke(harness.service, "no-input", value), "invalid_input")


def test_input_is_accepted_up_to_its_size_limit(harness):
    harness.add("big", "return {}", input_schema={"type": "object"})
    overhead = len(json.dumps({"s": ""}).encode())
    exact = {"s": "x" * (MAX_INPUT_BYTES - overhead)}
    assert invoke(harness.service, "big", exact)["status"] == "running"


def test_invalid_input_is_reported_before_the_isolation_floor(storage, tmp_path):
    from backend.functions.runner import HardenedSubprocessRunner

    degraded = HardenedSubprocessRunner(use_landlock=False, use_seccomp=False)
    harness = Harness(storage, tmp_path, degraded, isolation_minimum="full")
    harness.add(
        "typed", "return {}", input_schema={"type": "object", "required": ["n"]}
    )
    _rejected(invoke(harness.service, "typed", {}), "invalid_input")


def test_a_degraded_runner_runs_under_the_default_floor(storage, tmp_path):
    from backend.functions.runner import HardenedSubprocessRunner

    degraded = HardenedSubprocessRunner(use_landlock=False, use_seccomp=False)
    harness = Harness(storage, tmp_path, degraded)
    harness.add("count", "return {}")
    service = harness.service
    run = finished(service, invoke(service, "count"))
    assert run["status"] == "succeeded"
    assert run["isolation"]["level"] == "degraded"


# --- one run per function, keys per function (G5, G6) ----------------------------------


def test_different_functions_run_at_the_same_time(harness):
    harness.add("one", "time.sleep(60)")
    harness.add("two", "return {}")
    service = harness.service
    first = invoke(service, "one")
    assert invoke(service, "two")["status"] == "running"
    service.cancel_run(first["id"])
    finished(service, first)


def test_the_same_key_on_two_functions_starts_two_runs(harness):
    harness.add("one", "return {}")
    harness.add("two", "return {}")
    service = harness.service
    a = invoke(service, "one", idempotency_key="shared")
    b = invoke(service, "two", idempotency_key="shared")
    assert a["id"] != b["id"]


def test_a_failing_store_releases_the_function(harness, monkeypatch):
    harness.add("count", "return {}")
    service = harness.service
    real_enqueue = harness.store.enqueue
    calls = []

    def enqueue_once_failing(job):
        calls.append(job)
        if len(calls) == 1:
            raise OSError("disk full")
        return real_enqueue(job)

    monkeypatch.setattr(harness.store, "enqueue", enqueue_once_failing)
    with pytest.raises(OSError):
        invoke(service, "count")
    assert invoke(service, "count")["status"] == "running"


@pytest.mark.parametrize("length, ok", [(200, True), (201, False)])
def test_text_fields_are_bounded_at_200_characters(harness, length, ok):
    from backend.functions.service import InvocationError

    harness.add("count", "return {}")
    service = harness.service
    for field in ("idempotency_key", "correlation_id"):
        if ok:
            invoke(service, "count", **{field: "k" * length})
        else:
            with pytest.raises(InvocationError):
                invoke(service, "count", **{field: "k" * length})


# --- limits (G8) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "manifest_limits, instance, expected",
    [
        ({"timeout_seconds": 5, "max_tool_calls": 1}, {}, (5, 1)),
        ({}, {"timeout_seconds": 6, "max_tool_calls": 2}, (6, 2)),
        (
            {"timeout_seconds": 50, "max_tool_calls": 9},
            {"timeout_seconds": 7, "max_tool_calls": 3},
            (7, 3),
        ),
        (
            {"timeout_seconds": 8, "max_tool_calls": 4},
            {"timeout_seconds": 60, "max_tool_calls": 50},
            (8, 4),
        ),
    ],
)
def test_each_limit_is_the_smaller_of_function_and_instance(
    storage, tmp_path, runner, manifest_limits, instance, expected
):
    harness = Harness(storage, tmp_path, runner, **instance)
    harness.add(
        "limits",
        """
        calls = 0
        while True:
            try:
                tools.graph.search(query="x", limit=1)
                calls += 1
            except ToolError:
                break
        return {"deadline": envelope["invocation"]["deadline"],
                "occurred": envelope["occurred_at"], "calls": calls}
        """,
        limits=manifest_limits,
    )
    service = harness.service
    run = finished(service, invoke(service, "limits"))
    output = run["output"]

    def parse(stamp):
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))

    seconds = (parse(output["deadline"]) - parse(output["occurred"])).total_seconds()
    assert (round(seconds), output["calls"]) == expected


def test_the_envelope_names_its_versions_and_attribution(harness):
    harness.add("echo", "return {'envelope': envelope}")
    service = harness.service
    envelope = finished(service, invoke(service, "echo", actor="u-5"))["output"][
        "envelope"
    ]
    assert envelope["schema_version"] == "1.0"
    assert envelope["event_id"].startswith("evt-")
    assert envelope["invocation"]["contract_version"] == "1"
    assert envelope["invocation"]["function_version"] == "1.2.0"
    assert envelope["origin"]["attribution"]["actor"]["actor_id"] == "u-5"


# --- records (G7) ------------------------------------------------------------------------


def _function_job(**fields):
    return ExecutionJob(
        agent_id="function:count",
        kind=ExecutionKind.FUNCTION,
        idempotency_key=fields.pop("key", "k"),
        state=fields.pop("state", ExecutionState.RUNNING),
        attempts=1,
        **fields,
    )


def test_output_is_shown_only_for_a_succeeded_run(harness):
    store = harness.store
    job = store.enqueue(_function_job())
    store.fail(job.id, error="function_error: no", result={"output": {"leak": 1}})
    run = harness.service.get_run(job.id)
    assert run["output"] is None
    assert run["error"] == {"code": "function_error", "message": "no"}


def test_a_function_error_message_is_kept_bounded(harness):
    harness.add("loud", "raise ToolError('custom', 'x' * 5000)")
    service = harness.service
    run = finished(service, invoke(service, "loud"))
    assert run["function_error_code"] == "custom"
    assert len(run["error"]["message"]) <= 1100


def test_a_runner_that_raises_closes_the_run_as_lost(harness, monkeypatch):
    import backend.functions.service as service_module

    harness.add("count", "return {}")
    service = harness.service

    def explode(*args, **kwargs):
        raise RuntimeError("runner broke")

    monkeypatch.setattr(service_module, "run_function", explode)
    run = finished(service, invoke(service, "count"))
    assert (run["status"], run["error"]["code"]) == ("failed", "runner_lost")


def test_the_runner_status_route_reports_an_unavailable_runner(harness, monkeypatch):
    harness.add("count", "return {}")
    client, service = _client(harness)

    def unavailable():
        raise RunnerUnavailable("no runner")

    monkeypatch.setattr(service._runner, "probe", unavailable)
    assert client.get("/functions/runner").status_code == 503
    assert client.post("/functions/count/invoke", json={}).status_code == 503


def test_an_invocation_without_a_body_sends_empty_input(harness):
    harness.add("count", "return {}")
    client, service = _client(harness)
    response = client.post("/functions/count/invoke")
    assert response.status_code == 202
    finished(service, response.json())


@pytest.mark.parametrize("limit", [0, 501])
def test_the_run_listing_limit_is_bounded(harness, limit):
    client, _ = _client(harness)
    assert client.get(f"/functions/runs?limit={limit}").status_code == 422


def test_a_status_filter_looks_past_the_newest_runs(harness):
    harness.add("count", "return {}")
    harness.add("off", "return {}", enable=False)
    service = harness.service
    for _ in range(3):
        invoke(service, "off")
    done = finished(service, invoke(service, "count"))
    for _ in range(3):
        invoke(service, "off")
    listed = service.list_runs(status="succeeded", limit=1)
    assert [r["id"] for r in listed] == [done["id"]]
    assert len(service.list_runs(status="rejected", limit=2)) == 2


# --- the store and the AgentRun view (G10) -------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def any_store(request, tmp_path):
    if request.param == "memory":
        return InMemoryExecutionStore(retry_policy=RetryPolicy(max_attempts=1))
    return SqliteExecutionStore(
        str(tmp_path / "runs.db"), retry_policy=RetryPolicy(max_attempts=1)
    )


def test_a_failure_without_a_result_keeps_an_earlier_result(any_store):
    job = any_store.enqueue(_function_job(result={"kept": True}))
    any_store.fail(job.id, error="boom")
    assert any_store.get(job.id).result == {"kept": True}


def test_a_stored_failure_result_is_a_copy(any_store):
    job = any_store.enqueue(_function_job())
    result = {"log": "a"}
    any_store.fail(job.id, error="boom", result=result)
    result["log"] = "changed"
    assert any_store.get(job.id).result == {"log": "a"}


def test_agent_runs_of_both_kinds_are_merged_newest_first_and_limited():
    store = InMemoryExecutionStore()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    kinds = [ExecutionKind.SCHEDULED, ExecutionKind.EVENT] * 3
    ids = []
    for i, kind in enumerate(kinds):
        job = store.enqueue(
            ExecutionJob(
                agent_id="agent-1",
                kind=kind,
                idempotency_key=f"k{i}",
                created_at=base.replace(minute=i),
            )
        )
        ids.append(job.id)
    listed = AgentRunRecorder(store).list_runs(limit=4)
    assert [r["id"] for r in listed] == list(reversed(ids))[:4]


# --- packages and manifests (G3) -----------------------------------------------------------


def _package(root, files):
    directory = root / "count-nodes"
    directory.mkdir()
    for name, content in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return directory


def _valid_files(**extra):
    return {
        "function.json": json.dumps(manifest("count-nodes")).encode(),
        "main.py": b"",
        **extra,
    }


def test_a_package_of_exactly_the_file_limit_is_read(tmp_path):
    extra = {f"f{i}.py": b"" for i in range(MAX_FILES - 2)}
    _package(tmp_path, _valid_files(**extra))
    assert load_function(str(tmp_path), "count-nodes").valid


def test_a_package_over_the_size_limit_is_invalid(tmp_path):
    directory = _package(tmp_path, _valid_files())
    with open(directory / "big.bin", "wb") as handle:
        handle.truncate(MAX_PACKAGE_BYTES)
    loaded = load_function(str(tmp_path), "count-nodes")
    assert not loaded.valid
    assert "bytes" in loaded.errors[0]


def test_a_manifest_over_its_size_limit_is_invalid(tmp_path):
    document = dict(manifest("count-nodes"), pad="x" * MAX_MANIFEST_BYTES)
    _package(tmp_path, {"function.json": json.dumps(document).encode(), "main.py": b""})
    assert not load_function(str(tmp_path), "count-nodes").valid


def test_a_linked_subdirectory_makes_a_package_invalid(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("SECRET = 1")
    root = tmp_path / "root"
    root.mkdir()
    directory = _package(root, _valid_files())
    os.symlink(outside, directory / "lib")
    loaded = load_function(str(root), "count-nodes")
    assert not loaded.valid
    assert all("SECRET" not in content.decode() for content in loaded.files.values())


def test_an_unreadable_file_is_reported_without_its_host_path(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads files regardless of their mode")
    directory = _package(tmp_path, _valid_files())
    (directory / "main.py").chmod(0)
    loaded = load_function(str(tmp_path), "count-nodes")
    assert not loaded.valid
    assert str(tmp_path) not in loaded.errors[0]


def test_a_missing_manifest_is_named(tmp_path):
    _package(tmp_path, {"main.py": b""})
    loaded = load_function(str(tmp_path), "count-nodes")
    assert loaded.errors == ["function.json is missing"]


@pytest.mark.parametrize(
    "function_id, valid",
    [
        ("ab", False),
        ("abc", True),
        ("a" * 64, True),
        ("a" * 65, False),
        ("has_underscore", False),
        ("trailing\n", False),
    ],
)
def test_the_id_pattern_bounds(function_id, valid):
    _, errors = validate_manifest(
        manifest(function_id),
        function_id,
        {"main.py": b"", "function.json": b"{}"},
    )
    assert (errors == []) is valid, errors


@pytest.mark.parametrize(
    "changes, valid",
    [
        ({"runtime": "node", "entry_point": "main.mjs"}, True),
        ({"input_schema": True}, False),
        ({"limits": {"timeout_seconds": 120, "memory_mib": 256}}, True),
        ({"description": None}, False),
    ],
)
def test_more_manifest_rules(changes, valid):
    files = {"main.py": b"", "main.mjs": b"", "function.json": b"{}"}
    _, errors = validate_manifest(
        dict(manifest("count-nodes"), **changes), "count-nodes", files
    )
    assert (errors == []) is valid, errors


# --- settings ------------------------------------------------------------------------------


def test_every_setting_is_read_from_its_variable():
    settings = FunctionsSettings.from_env(
        {
            "FUNCTIONS_ROOT": "/srv/functions",
            "FUNCTIONS_ENABLED": '{"a-fn": ["graph:read"]}',
            "FUNCTIONS_ISOLATION_MINIMUM": " FULL ",
            "FUNCTIONS_TIMEOUT_SECONDS": "30",
            "FUNCTIONS_MEMORY_MIB": "128",
            "FUNCTIONS_MAX_TOOL_CALLS": "20",
        }
    )
    assert settings.root == "/srv/functions"
    assert settings.isolation_minimum == "full"
    assert (settings.timeout_seconds, settings.memory_mib, settings.max_tool_calls) == (
        30,
        128,
        20,
    )


@pytest.mark.parametrize(
    "environ",
    [{"FUNCTIONS_ENABLED": "   "}, {"FUNCTIONS_TIMEOUT_SECONDS": "  "}],
)
def test_blank_settings_mean_unset(environ):
    settings = FunctionsSettings.from_env(environ)
    assert settings.enabled == {} and settings.timeout_seconds == 120


def test_grants_must_be_strings():
    from backend.functions.settings import FunctionsSettingsError

    with pytest.raises(FunctionsSettingsError):
        FunctionsSettings.from_env({"FUNCTIONS_ENABLED": '{"a-fn": [1]}'})


def test_the_read_window_hides_a_run_created_exactly_at_the_cutoff(harness):
    store = harness.store
    job = store.enqueue(_function_job(key="edge"))
    harness.cutoff = job.created_at
    assert harness.service.get_run(job.id) is None


def test_a_redacted_flag_is_reported(harness):
    from backend.agents.execution.models import redact_job

    job = harness.store.enqueue(_function_job(key="r"))
    harness.store.complete(job.id, result={"outcome": "succeeded"})
    redact_job(harness.store._jobs[job.id], datetime.now(timezone.utc))
    assert harness.service.get_run(job.id)["redacted"] is True
