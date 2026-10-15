"""Invariants of function invocation and run records (USER_FUNCTION_CONTRACT §4.4,
§5, §8, §11), against the real runner."""

import json
import textwrap
import time
from datetime import datetime, timedelta, timezone

import pytest

from backend.agents.execution import (
    ExecutionJob,
    ExecutionKind,
    InMemoryExecutionStore,
    RetryPolicy,
)
from backend.agents.run_history import AgentRunRecorder
from backend.core import Node, NodeType
from backend.functions.runner import HardenedSubprocessRunner
from backend.functions.service import FunctionNotFound, FunctionService, InvocationError
from backend.functions.settings import FunctionsSettings
from backend.functions.tests.test_broker import RecordingHook
from backend.runtime.authorization import use_request_authorization
from backend.service import GraphService

HELPER = "from communityoverview_function import run, ToolError\n"


@pytest.fixture(scope="module")
def runner():
    return HardenedSubprocessRunner()


def manifest(function_id, **fields):
    document = {
        "contract_version": "1",
        "id": function_id,
        "version": "1.2.0",
        "name": function_id,
        "runtime": "python",
        "entry_point": "main.py",
        "triggers": [{"kind": "manual"}],
        "grants": ["graph:read"],
    }
    document.update(fields)
    return document


class Harness:
    def __init__(self, storage, root, runner, *, hook=None, **settings):
        self.root = root
        self.store = InMemoryExecutionStore(retry_policy=RetryPolicy(max_attempts=1))
        self.enabled = {}
        self._settings = settings
        self._hook = hook
        self._storage = storage
        self._runner = runner
        self.cutoff = None

    def add(self, function_id, handler_body, *, enable=True, **fields):
        document = manifest(function_id, **fields)
        directory = self.root / function_id
        directory.mkdir()
        (directory / "function.json").write_text(json.dumps(document))
        (directory / "main.py").write_text(
            HELPER
            + "import os, sys, time\n"
            + "def handle(envelope, tools):\n"
            + textwrap.indent(textwrap.dedent(handler_body), "    ")
            + "\nrun(handle)\n"
        )
        if enable:
            self.enabled[function_id] = list(document["grants"])

    @property
    def service(self):
        settings = FunctionsSettings(
            root=str(self.root),
            enabled={k: frozenset(v) for k, v in self.enabled.items()},
            **self._settings,
        )
        return FunctionService(
            GraphService(self._storage, authorization_hook=self._hook),
            self.store,
            settings=settings,
            runner=self._runner,
            read_cutoff=lambda: self.cutoff,
        )


@pytest.fixture
def harness(storage, tmp_path, runner):
    return Harness(storage, tmp_path, runner)


def invoke(service, function_id, input_value=None, actor="user-1", **kwargs):
    with use_request_authorization(actor_id=actor, actor_type="human"):
        return service.invoke(
            function_id, {} if input_value is None else input_value, **kwargs
        )


def finished(service, run, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = service.get_run(run["id"])
        if current["status"] != "running":
            return current
        time.sleep(0.05)
    raise AssertionError(f"run {run['id']} did not finish")


# --- a run, end to end ------------------------------------------------------------


def test_a_run_reads_the_graph_and_records_its_outcome(harness, storage):
    storage.add_nodes([Node(name="Alpha", type=NodeType.ACTOR)], [])
    harness.add(
        "count-nodes",
        """
        print("counting")
        found = tools.graph.search(query="Alpha", limit=5)
        return {"count": len(found["nodes"])}
        """,
    )
    service = harness.service
    started = invoke(service, "count-nodes", correlation_id="corr-7")
    assert started["status"] == "running"
    run = finished(service, started)
    assert run["status"] == "succeeded", run
    assert run["output"] == {"count": 1}
    assert run["error"] is None
    assert (run["function_id"], run["function_version"], run["trigger"]) == (
        "count-nodes",
        "1.2.0",
        "manual",
    )
    assert run["correlation_id"] == "corr-7"
    assert run["tool_calls"]["count"] == 1
    assert run["tool_calls"]["calls"][0]["tool"] == "graph.search"
    assert "counting" in run["log"]
    assert run["usage"]["wall_ms"] > 0
    assert run["isolation"]["level"] in ("full", "degraded")
    assert run["actor"]["actor"]["actor_id"] == "user-1"
    assert run["started_at"] and run["finished_at"]


def test_the_envelope_carries_the_invocation_as_the_contract_describes(harness):
    harness.add(
        "echo",
        "return {'envelope': envelope}",
        input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
    )
    service = harness.service
    run = finished(
        service,
        invoke(service, "echo", {"n": 3}, idempotency_key="k-1", event_origin="web-ui"),
    )
    envelope = run["output"]["envelope"]
    invocation = envelope["invocation"]
    assert envelope["event_type"] == "function.invoke"
    assert envelope["entity"] is None
    assert envelope["origin"]["event_origin"] == "web-ui"
    assert envelope["origin"]["event_correlation_id"] == run["correlation_id"]
    assert envelope["origin"]["event_session_id"] is None
    assert invocation["run_id"] == run["id"]
    assert invocation["function_id"] == "echo"
    assert invocation["input"] == {"n": 3}
    assert invocation["idempotency_key"] == "k-1"
    assert (invocation["trigger"], invocation["chain_depth"]) == ("manual", 0)
    assert invocation["deadline"].endswith("Z")


def test_a_function_error_is_a_failed_run_that_keeps_its_log(harness):
    harness.add("broken", "raise ValueError('boom')")
    service = harness.service
    run = finished(service, invoke(service, "broken"))
    assert run["status"] == "failed"
    assert run["error"]["code"] == "function_error"
    assert run["function_error_code"] == "unhandled_exception"
    assert "Traceback" in run["log"]
    assert run["output"] is None


def test_a_run_over_its_time_limit_is_timed_out(harness):
    harness.add("spin", "while True:\n    pass", limits={"timeout_seconds": 1})
    service = harness.service
    run = finished(service, invoke(service, "spin"))
    assert (run["status"], run["error"]["code"]) == ("timed_out", "timeout")


def test_output_that_fails_its_schema_fails_the_run(harness):
    harness.add(
        "wrong-shape",
        "return {'count': 'many'}",
        output_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
    )
    service = harness.service
    run = finished(service, invoke(service, "wrong-shape"))
    assert (run["status"], run["error"]["code"]) == ("failed", "invalid_output")
    assert run["output"] is None


def test_the_run_gets_the_smaller_of_the_function_and_instance_limits(
    storage, tmp_path, runner
):
    harness = Harness(storage, tmp_path, runner, memory_mib=128, max_tool_calls=1)
    harness.add(
        "limits",
        """
        import resource
        calls = []
        for _ in range(2):
            try:
                tools.graph.search(query="x", limit=1)
                calls.append("ok")
            except ToolError as exc:
                calls.append(exc.code)
        return {"as": resource.getrlimit(resource.RLIMIT_AS)[0], "calls": calls}
        """,
        limits={"memory_mib": 200, "max_tool_calls": 5, "timeout_seconds": 30},
    )
    service = harness.service
    run = finished(service, invoke(service, "limits"))
    assert run["output"] == {"as": 128 * 1024 * 1024, "calls": ["ok", "limit_exceeded"]}


def test_an_operator_can_cancel_a_run_in_progress(harness):
    harness.add("sleeper", "time.sleep(60)")
    service = harness.service
    started = invoke(service, "sleeper")
    time.sleep(0.5)
    assert service.cancel_run(started["id"]) is True
    run = finished(service, started)
    assert run["status"] == "cancelled"
    assert service.cancel_run(started["id"]) is False
    assert service.cancel_run("job-unknown") is False


# --- rejected invocations (§11) ------------------------------------------------------


def _rejected(run, code):
    assert run["status"] == "rejected", run
    assert run["error"]["code"] == code
    assert run["started_at"] is None


def test_an_invalid_manifest_is_rejected(harness):
    harness.add("bad-limits", "return {}", limits={"timeout_seconds": 999})
    _rejected(invoke(harness.service, "bad-limits"), "invalid_manifest")


def test_a_function_that_is_not_enabled_is_rejected(harness):
    harness.add("off", "return {}", enable=False)
    _rejected(invoke(harness.service, "off"), "not_enabled")


def test_a_function_with_a_grant_not_enabled_is_rejected(harness):
    harness.add("writer", "return {}", grants=["graph:read", "graph:write"])
    harness.enabled["writer"] = ["graph:read"]
    _rejected(invoke(harness.service, "writer"), "not_enabled")


def test_a_caller_who_cannot_write_cannot_start_a_writing_function(
    storage, tmp_path, runner
):
    harness = Harness(storage, tmp_path, runner, hook=RecordingHook(deny_mutate=True))
    harness.add("writer", "return {}", grants=["graph:read", "graph:write"])
    harness.add("reader", "return {}")
    _rejected(invoke(harness.service, "writer"), "access_denied")
    service = harness.service
    assert finished(service, invoke(service, "reader"))["status"] == "succeeded"


@pytest.mark.parametrize(
    "input_value", [{"n": "three"}, {}, [], "text", {"n": 1, "big": "x" * 300000}]
)
def test_input_that_fails_its_schema_is_rejected(harness, input_value):
    harness.add(
        "typed",
        "return {}",
        input_schema={
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        },
    )
    _rejected(invoke(harness.service, "typed", input_value), "invalid_input")


def test_input_to_a_function_without_an_input_schema_must_be_empty(harness):
    harness.add("no-input", "return {}")
    _rejected(invoke(harness.service, "no-input", {"x": 1}), "invalid_input")


def test_a_second_run_while_one_is_in_progress_is_busy(harness):
    harness.add("sleeper", "time.sleep(60)")
    service = harness.service
    first = invoke(service, "sleeper")
    _rejected(invoke(service, "sleeper"), "busy")
    service.cancel_run(first["id"])
    finished(service, first)
    assert invoke(service, "sleeper")["status"] == "running"


def test_a_runner_below_the_instance_minimum_is_rejected(storage, tmp_path):
    degraded = HardenedSubprocessRunner(use_landlock=False, use_seccomp=False)
    harness = Harness(storage, tmp_path, degraded, isolation_minimum="full")
    harness.add("count", "return {}")
    _rejected(invoke(harness.service, "count"), "isolation_below_minimum")


def test_an_unknown_function_is_not_found_and_records_nothing(harness):
    with pytest.raises(FunctionNotFound):
        invoke(harness.service, "no-such-function")
    assert harness.store.list_jobs() == []


@pytest.mark.parametrize("key", ["", "k" * 201, 7])
def test_a_malformed_idempotency_key_is_refused(harness, key):
    harness.add("count", "return {}")
    with pytest.raises(InvocationError):
        invoke(harness.service, "count", idempotency_key=key)


# --- idempotency (§5.3) ----------------------------------------------------------------


def test_a_repeated_key_returns_the_existing_run_in_any_state(harness):
    harness.add("count", "return {'n': 1}")
    service = harness.service
    first = invoke(service, "count", idempotency_key="same")
    again = invoke(service, "count", idempotency_key="same")
    assert again["id"] == first["id"]
    finished(service, first)
    later = invoke(service, "count", idempotency_key="same")
    assert later["id"] == first["id"]
    assert later["status"] == "succeeded"
    assert len(harness.store.list_jobs()) == 1


def test_a_rejected_run_holds_its_key(harness):
    harness.add("off", "return {}", enable=False)
    rejected = invoke(harness.service, "off", idempotency_key="fix-me")
    harness.enabled["off"] = ["graph:read"]
    service = harness.service
    again = invoke(service, "off", idempotency_key="fix-me")
    assert again["id"] == rejected["id"] and again["status"] == "rejected"
    assert invoke(service, "off", idempotency_key="new-key")["status"] == "running"


# --- reading records -------------------------------------------------------------------


def test_runs_are_listed_by_function_and_status(harness):
    harness.add("count", "return {}")
    harness.add("off", "return {}", enable=False)
    service = harness.service
    done = finished(service, invoke(service, "count"))
    invoke(service, "off")
    assert [r["id"] for r in service.list_runs(function_id="count")] == [done["id"]]
    assert [r["status"] for r in service.list_runs(status="rejected")] == ["rejected"]
    assert service.list_runs(status="not-a-status") == []
    assert len(service.list_runs()) == 2


def test_runs_outside_the_read_window_are_hidden(harness):
    harness.add("count", "return {}")
    service = harness.service
    run = finished(service, invoke(service, "count"))
    harness.cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert service.get_run(run["id"]) is None
    assert service.list_runs() == []


def test_function_and_agent_runs_share_the_store_but_not_their_views(harness):
    harness.add("count", "return {}")
    service = harness.service
    function_run = finished(service, invoke(service, "count"))
    agent_job = harness.store.enqueue(
        ExecutionJob(agent_id="agent-1", kind=ExecutionKind.EVENT, idempotency_key="a")
    )
    recorder = AgentRunRecorder(harness.store)
    assert [r["id"] for r in recorder.list_runs()] == [agent_job.id]
    assert recorder.get_run(function_run["id"]) is None
    assert recorder.list_runs(kind=ExecutionKind.FUNCTION) == []
    assert service.get_run(agent_job.id) is None


def test_functions_are_listed_with_their_validation_and_enablement(harness):
    harness.add("count", "return {}")
    harness.add("writer", "return {}", grants=["graph:read", "graph:write"])
    harness.enabled["writer"] = ["graph:read"]
    harness.add("bad", "return {}", triggers=[{"kind": "schedule"}], enable=False)
    listed = {f["id"]: f for f in harness.service.list_functions()}
    assert listed["count"]["valid"] and listed["count"]["enabled"]
    assert listed["writer"]["missing_grants"] == ["graph:write"]
    assert not listed["bad"]["valid"] and listed["bad"]["errors"]
    with pytest.raises(FunctionNotFound):
        harness.service.get_function("absent")
