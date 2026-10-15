"""Run-record invariants pinned after the third review round
(USER_FUNCTION_CONTRACT §5.3, §11; DURABLE_EXECUTION_CONTRACT)."""

import threading
from datetime import datetime, timezone

import pytest

from backend.agents.execution import (
    ExecutionJob,
    ExecutionKind,
    ExecutionState,
    InMemoryExecutionStore,
)
from backend.agents.execution.models import redact_job
from backend.functions.manifest import is_valid_id
from backend.functions.service import FunctionNotFound, RunViewer, view_run
from backend.functions.settings import FunctionsSettings
from backend.functions.tests.test_service import (
    Harness,
    _rejected,
    finished,
    invoke,
)

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def runner():
    from backend.functions.runner import HardenedSubprocessRunner

    return HardenedSubprocessRunner()


@pytest.fixture
def harness(storage, tmp_path, runner):
    return Harness(storage, tmp_path, runner)


def _job(kind=ExecutionKind.FUNCTION, state=ExecutionState.RUNNING, **fields):
    return ExecutionJob(
        agent_id=fields.pop("agent_id", "function:count"),
        kind=kind,
        idempotency_key=fields.pop("key", "k"),
        state=state,
        attempts=1,
        **fields,
    )


# --- redaction --------------------------------------------------------------------


def test_redaction_keeps_exactly_a_function_runs_identity_and_outcome():
    job = _job(
        state=ExecutionState.DEAD_LETTER,
        payload={
            "function_id": "count",
            "function_version": "1.0.0",
            "trigger": "manual",
            "actor": {"actor": {"actor_id": "u"}},
            "isolation": {"level": "full"},
            "boot_id": "b",
        },
        result={
            "outcome": "failed",
            "code": "function_error",
            "message": "Node Confidential X",
            "log": "secret",
            "output": {"secret": 1},
            "function_error_code": "custom",
        },
    )
    redact_job(job, NOW)
    assert job.payload == {
        "function_id": "count",
        "function_version": "1.0.0",
        "trigger": "manual",
    }
    assert job.result["outcome"] == "failed"
    assert job.result["code"] == "function_error"
    for key in ("message", "log", "output", "function_error_code"):
        assert job.result[key] is None


def test_redaction_drops_an_identity_value_too_long_to_keep():
    job = _job(payload={"function_id": "x" * 500, "trigger": "manual"})
    redact_job(job, NOW)
    assert job.payload["function_id"] is None


def test_redaction_keeps_no_outcome_text_for_other_kinds():
    job = _job(
        kind=ExecutionKind.EVENT,
        agent_id="agent-1",
        payload={"function_id": "count"},
        result={"outcome": "secret text", "code": "secret code"},
    )
    redact_job(job, NOW)
    assert "function_id" not in job.payload
    assert job.result == {"outcome": None, "code": None}


# --- closing interrupted runs ----------------------------------------------------------


def test_only_running_function_runs_of_other_boots_are_closed(harness):
    store = harness.store
    interrupted = store.enqueue(_job(key="a", payload={"boot_id": "old"}))
    done = store.enqueue(
        _job(key="b", state=ExecutionState.SUCCEEDED, payload={"boot_id": "old"})
    )
    agent_run = store.enqueue(
        _job(kind=ExecutionKind.EVENT, agent_id="agent-1", key="c")
    )
    service = harness.service
    assert service.close_interrupted_runs() == 1
    closed = service.get_run(interrupted.id)
    assert closed["status"] == "failed"
    assert closed["error"] == {
        "code": "runner_lost",
        "message": "The host restarted during the run.",
    }
    assert store.get(done.id).state == ExecutionState.SUCCEEDED
    assert store.get(agent_run.id).state == ExecutionState.RUNNING


def test_one_run_that_cannot_be_closed_does_not_stop_the_others(harness, monkeypatch):
    store = harness.store
    first = store.enqueue(_job(key="a", payload={"boot_id": "old"}))
    second = store.enqueue(_job(key="b", payload={"boot_id": "old"}))
    real_fail = store.fail

    def fail_first(job_id, **kwargs):
        if job_id == first.id:
            raise OSError("locked")
        return real_fail(job_id, **kwargs)

    monkeypatch.setattr(store, "fail", fail_first)
    assert harness.service.close_interrupted_runs() == 1
    assert store.get(second.id).state == ExecutionState.DEAD_LETTER


# --- cancel while the outcome is recorded ------------------------------------------------


def test_a_cancel_while_the_outcome_is_recorded_does_nothing(harness, monkeypatch):
    harness.add("count", "return {'n': 1}")
    service = harness.service
    entered, release = threading.Event(), threading.Event()
    real_complete = harness.store.complete

    def slow_complete(*args, **kwargs):
        entered.set()
        release.wait(10)
        return real_complete(*args, **kwargs)

    monkeypatch.setattr(harness.store, "complete", slow_complete)
    started = invoke(service, "count")
    assert entered.wait(30)
    assert service.cancel_run(started["id"]) is False
    release.set()
    run = finished(service, started)
    assert run["status"] == "succeeded"
    assert service._active == {} and service._busy == set()


# --- listing --------------------------------------------------------------------------------


def test_the_listing_honours_its_limit_without_a_status_filter(harness):
    harness.add("count", "return {}")
    service = harness.service
    for _ in range(3):
        finished(service, invoke(service, "count"))
    assert len(service.list_runs(limit=2)) == 2


def test_each_status_filter_returns_only_its_status(harness):
    harness.add("count", "return {}")
    harness.add("off", "return {}", enable=False)
    harness.add("sleeper", "time.sleep(60)")
    harness.add("broken", "raise ValueError('x')")
    harness.add("spin", "while True:\n    pass", limits={"timeout_seconds": 1})
    service = harness.service
    succeeded = finished(service, invoke(service, "count"))
    rejected = invoke(service, "off")
    failed = finished(service, invoke(service, "broken"))
    timed_out = finished(service, invoke(service, "spin"))
    failed_again = finished(service, invoke(service, "broken"))
    cancelled = invoke(service, "sleeper")
    service.cancel_run(cancelled["id"])
    finished(service, cancelled)
    running = invoke(service, "sleeper")

    def ids(status, limit=50):
        return [r["id"] for r in service.list_runs(status=status, limit=limit)]

    assert ids("succeeded") == [succeeded["id"]]
    assert ids("rejected") == [rejected["id"]]
    assert ids("cancelled") == [cancelled["id"]]
    assert ids("running") == [running["id"]]
    assert ids("timed_out") == [timed_out["id"]]
    assert ids("failed") == [failed_again["id"], failed["id"]]
    assert ids("failed", limit=1) == [failed_again["id"]]
    service.cancel_run(running["id"])
    finished(service, running)


def test_agent_runs_in_the_shared_store_are_not_function_runs(harness):
    harness.store.enqueue(_job(kind=ExecutionKind.EVENT, agent_id="agent-1"))
    assert harness.service.list_runs() == []


# --- identity and defaults --------------------------------------------------------------------


@pytest.mark.parametrize("reserved", ["runs", "runner"])
def test_reserved_ids_are_never_functions(harness, reserved):
    (harness.root / reserved).mkdir()
    assert not is_valid_id(reserved)
    with pytest.raises(FunctionNotFound):
        harness.service.get_function(reserved)
    with pytest.raises(FunctionNotFound):
        invoke(harness.service, reserved)


@pytest.mark.parametrize("function_id", ["abc-", "a-b-c", "x" * 64])
def test_ids_of_the_pattern_are_valid(function_id):
    assert is_valid_id(function_id)


@pytest.mark.parametrize("function_id", ["abc\n", "ab", "ABC", "a:b"])
def test_ids_outside_the_pattern_are_not(function_id):
    assert not is_valid_id(function_id)


def test_a_function_that_is_not_enabled_is_listed_as_disabled(harness):
    harness.add("off", "return {}", enable=False)
    (listed,) = harness.service.list_functions()
    assert listed["enabled"] is False


def test_a_run_without_a_correlation_id_gets_a_fresh_one(harness):
    harness.add("count", "return {}")
    run = invoke(harness.service, "count")
    assert run["correlation_id"].startswith("corr-")


def test_a_rejection_is_finished_when_recorded(harness):
    harness.add("off", "return {}", enable=False)
    run = invoke(harness.service, "off")
    _rejected(run, "not_enabled")
    assert run["finished_at"] is not None


def test_a_cancelled_run_carries_no_error(harness):
    harness.add("sleeper", "time.sleep(60)")
    service = harness.service
    started = invoke(service, "sleeper")
    service.cancel_run(started["id"])
    run = finished(service, started)
    assert (run["status"], run["error"]) == ("cancelled", None)


def test_a_truncated_log_is_reported(harness, monkeypatch):
    import backend.functions.service as service_module

    real = service_module.RunSpec

    def small_log(**fields):
        return real(**dict(fields, max_log_bytes=10))

    monkeypatch.setattr(service_module, "RunSpec", small_log)
    harness.add("loud", "print('x' * 1000); return {}")
    service = harness.service
    run = finished(service, invoke(service, "loud"))
    assert run["log_truncated"] is True and len(run["log"]) == 10


def test_a_viewer_by_default_is_not_narrowed():
    run = {"output": {"a": 1}, "log": "l", "actor": None, "error": None}
    assert view_run(run, RunViewer()) == run


def test_a_narrowed_viewer_without_an_actor_id_sees_no_content():
    run = {
        "output": {"a": 1},
        "log": "l",
        "actor": {"actor": {"actor_id": ""}},
        "error": None,
    }
    assert view_run(run, RunViewer(actor_id="", narrowed=True))["output"] is None


def test_a_blank_functions_root_means_the_default():
    from backend.functions.settings import DEFAULT_ROOT

    assert FunctionsSettings.from_env({"FUNCTIONS_ROOT": ""}).root == str(DEFAULT_ROOT)


def test_a_job_read_by_its_key_is_a_copy():
    store = InMemoryExecutionStore()
    store.enqueue(_job(key="copy"))
    store.get_by_key("copy").payload["changed"] = True
    assert "changed" not in store.get_by_key("copy").payload


# --- review round 4 --------------------------------------------------------------------


def test_a_schema_check_never_fetches_a_remote_reference():
    import http.server

    from backend.functions.schema_check import SchemaCheckFailed, schema_problem

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"enum": ["secret"]}')

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/leak"
        with pytest.raises(SchemaCheckFailed):
            schema_problem({"$ref": url}, "x")
    finally:
        server.shutdown()
    assert hits == []


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "http://example.invalid/s.json"},
        {"properties": {"a": {"$ref": "other.json"}}},
        {"$dynamicRef": "https://example.invalid/x"},
        {"$ref": 5},
    ],
)
def test_a_manifest_schema_may_refer_only_within_itself(schema):
    from backend.functions.manifest import validate_manifest
    from backend.functions.tests.test_service import manifest

    _, errors = validate_manifest(
        dict(manifest("count-nodes"), input_schema=schema),
        "count-nodes",
        {"main.py": b"", "function.json": b"{}"},
    )
    assert errors


def test_a_local_reference_is_allowed():
    from backend.functions.manifest import validate_manifest
    from backend.functions.tests.test_service import manifest

    schema = {
        "$defs": {"n": {"type": "integer"}},
        "properties": {"n": {"$ref": "#/$defs/n"}},
    }
    _, errors = validate_manifest(
        dict(manifest("count-nodes"), input_schema=schema),
        "count-nodes",
        {"main.py": b"", "function.json": b"{}"},
    )
    assert errors == []


def test_a_busy_function_rejects_before_checking_input(harness, monkeypatch):
    import backend.functions.service as service_module

    harness.add("sleeper", "time.sleep(60)", input_schema={"type": "object"})
    service = harness.service
    first = invoke(service, "sleeper")
    checks = []
    monkeypatch.setattr(
        service_module, "schema_problem", lambda *a, **k: checks.append(1)
    )
    _rejected(invoke(service, "sleeper"), "busy")
    assert checks == []
    service.cancel_run(first["id"])
    finished(service, first)


def test_invocations_beyond_the_check_slots_are_turned_away(harness):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import backend.functions.schema_check as schema_check
    from backend.api_host.function_routes import register_function_routes

    harness.add("typed", "return {}", input_schema={"type": "object"})
    service = harness.service
    app = FastAPI()
    register_function_routes(app, service, service._graph_service)
    client = TestClient(app)
    taken = 0
    while schema_check._input_slots.acquire(blocking=False):
        taken += 1
    try:
        response = client.post("/functions/typed/invoke", json={"input": {}})
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
    finally:
        for _ in range(taken):
            schema_check._input_slots.release()
    assert harness.store.list_jobs() == []


def test_deeply_nested_input_is_invalid_input(harness):
    harness.add(
        "typed",
        "return {}",
        input_schema={"type": "object", "properties": {"a": {"uniqueItems": True}}},
    )
    deep = []
    for _ in range(600):
        deep = [deep]
    _rejected(invoke(harness.service, "typed", {"a": [deep, deep]}), "invalid_input")


# --- review round 5 --------------------------------------------------------------------


def test_a_schema_that_refers_to_itself_without_end_is_the_manifests_fault(harness):
    harness.add("loop", "return {}", input_schema={"$ref": "#"})
    _rejected(invoke(harness.service, "loop", {}), "invalid_manifest")


@pytest.mark.parametrize(
    "schema",
    [
        {"properties": {"$ref": {"type": "string"}}},
        {"const": {"$ref": "http://x"}},
        {"enum": [{"$ref": "x"}]},
        {"default": {"$ref": "y"}},
        {"$defs": {"$ref": {"type": "integer"}}},
    ],
)
def test_data_and_names_that_look_like_references_are_not_references(schema):
    from backend.functions.manifest import validate_manifest
    from backend.functions.tests.test_service import manifest

    _, errors = validate_manifest(
        dict(manifest("count-nodes"), input_schema=schema),
        "count-nodes",
        {"main.py": b"", "function.json": b"{}"},
    )
    assert errors == []


def test_a_package_is_read_again_only_when_it_changes(harness, monkeypatch):
    import json as json_module

    import backend.functions.service as service_module

    harness.add("count", "return {}")
    service = harness.service
    reads = []
    real = service_module.load_function

    def counting(root, name):
        reads.append(name)
        return real(root, name)

    monkeypatch.setattr(service_module, "load_function", counting)
    service.list_functions()
    service.get_function("count")
    service.list_functions()
    assert reads == ["count"]
    path = harness.root / "count" / "function.json"
    document = json_module.loads(path.read_text())
    document["name"] = "Renamed"
    path.write_text(json_module.dumps(document))
    assert service.get_function("count")["name"] == "Renamed"
    assert reads == ["count", "count"]


def test_output_checks_do_not_wait_on_callers_input_checks(harness):
    import backend.functions.schema_check as schema_check

    harness.add("typed-out", "return {'n': 1}", output_schema={"type": "object"})
    service = harness.service
    started = invoke(service, "typed-out")
    taken = 0
    while schema_check._input_slots.acquire(blocking=False):
        taken += 1
    try:
        run = finished(service, started, timeout=20)
    finally:
        for _ in range(taken):
            schema_check._input_slots.release()
    assert run["status"] == "succeeded"


# --- review round 5 mutation residue ----------------------------------------------------


def test_a_conforming_output_keeps_its_output(harness):
    harness.add(
        "typed-out",
        "return {'n': 1}",
        output_schema={"type": "object", "required": ["n"]},
    )
    service = harness.service
    run = finished(service, invoke(service, "typed-out"))
    assert (run["status"], run["output"]) == ("succeeded", {"n": 1})


def test_a_failed_run_keeps_its_own_code_under_an_output_schema(harness):
    harness.add("broken", "raise ValueError('x')", output_schema={"type": "object"})
    service = harness.service
    run = finished(service, invoke(service, "broken"))
    assert run["error"]["code"] == "function_error"


def test_an_invalid_output_says_what_failed(harness):
    harness.add(
        "wrong",
        "return {'n': 'x'}",
        output_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
    )
    service = harness.service
    run = finished(service, invoke(service, "wrong"))
    assert run["error"]["message"].startswith("result.output failed output_schema")


def test_a_schema_problem_is_cut_to_its_limit():
    from backend.functions.schema_check import MAX_PROBLEM_CHARS, schema_problem

    problem = schema_problem({"const": "x" * 10000}, "y")
    assert problem and len(problem) <= MAX_PROBLEM_CHARS


def test_the_number_of_concurrent_input_checks_is_bounded():
    import backend.functions.schema_check as schema_check

    taken = 0
    while schema_check._input_slots.acquire(blocking=False):
        taken += 1
    try:
        assert taken == schema_check.MAX_INPUT_CHECKS == 3
        with pytest.raises(schema_check.SchemaCheckBusy):
            schema_check.schema_problem({"type": "object"}, {})
    finally:
        for _ in range(taken):
            schema_check._input_slots.release()


def test_a_check_that_needs_too_much_memory_fails():
    from backend.functions.schema_check import SchemaCheckFailed, schema_problem

    # Building this list inside the check needs well over the memory limit.
    huge = {"type": "array", "items": {"type": "integer"}, "minItems": 1}
    with pytest.raises(SchemaCheckFailed):
        schema_problem(huge, [0] * 80_000_000)


def test_the_check_ignores_the_hosts_environment(monkeypatch, tmp_path):
    from backend.functions.schema_check import schema_problem

    (tmp_path / "jsonschema.py").write_text("raise SystemExit('poisoned')\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert schema_problem({"type": "object"}, {}) is None


@pytest.mark.parametrize("combiner", ["allOf", "anyOf", "oneOf"])
def test_a_remote_reference_inside_a_combiner_is_refused(combiner):
    from backend.functions.manifest import validate_manifest
    from backend.functions.tests.test_service import manifest

    schema = {combiner: [{"type": "object"}, {"$ref": "http://example.invalid/s"}]}
    _, errors = validate_manifest(
        dict(manifest("count-nodes"), input_schema=schema),
        "count-nodes",
        {"main.py": b"", "function.json": b"{}"},
    )
    assert errors


def test_a_manifest_whose_schema_recurses_in_validation_is_invalid(tmp_path):
    import json as json_module

    from backend.functions.manifest import load_function
    from backend.functions.tests.test_service import manifest

    # Shallow enough for the JSON parser, deep enough for schema validation,
    # which recurses several frames per level.
    depth = 300
    schema = '{"allOf": [' * depth + '{"type": "object"}' + "]}" * depth
    document = json_module.dumps(manifest("count-nodes"))[:-1]
    directory = tmp_path / "count-nodes"
    directory.mkdir()
    (directory / "main.py").write_text("")
    (directory / "function.json").write_text(
        document + ', "input_schema": ' + schema + "}"
    )
    loaded = load_function(str(tmp_path), "count-nodes")
    assert not loaded.valid


def test_a_shared_state_filter_looks_past_newer_runs_of_the_other_status(harness):
    harness.add("sleeper", "time.sleep(60)")
    harness.add("off", "return {}", enable=False)
    service = harness.service
    cancelled = invoke(service, "sleeper")
    service.cancel_run(cancelled["id"])
    finished(service, cancelled)
    for _ in range(5):
        invoke(service, "off")
    assert [r["id"] for r in service.list_runs(status="cancelled", limit=2)] == [
        cancelled["id"]
    ]


def test_a_closed_interrupted_run_keeps_its_code_through_redaction(harness):
    job = harness.store.enqueue(_job(key="r", payload={"boot_id": "old"}))
    service = harness.service
    service.close_interrupted_runs()
    redact_job(harness.store._jobs[job.id], NOW)
    assert service.get_run(job.id)["error"]["code"] == "runner_lost"


def test_redaction_drops_an_outcome_code_too_long_to_keep():
    job = _job(
        state=ExecutionState.DEAD_LETTER,
        result={"outcome": "failed", "code": "c" * 500},
    )
    redact_job(job, NOW)
    assert job.result["code"] is None


def test_a_narrowed_reader_does_not_see_where_someone_elses_run_came_from():
    run = {
        "output": None,
        "log": None,
        "actor": {"actor": {"actor_id": "someone"}},
        "error": None,
        "origin": "private-tool",
        "correlation_id": "c",
        "function_error_code": None,
    }
    seen = view_run(run, RunViewer(actor_id="narrowed", narrowed=True))
    assert seen["origin"] is None
