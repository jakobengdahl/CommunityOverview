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
