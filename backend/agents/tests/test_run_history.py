"""
Tests for durable AgentRun history: the recorder, the ExecutionJob→AgentRun
mapping, worker integration, and the registry read surface.
"""

from unittest.mock import MagicMock

import pytest

from backend.agents.config import AgentConfig, AgentPrompts, AgentsSettings
from backend.agents.execution import (
    InMemoryExecutionStore,
    RetryPolicy,
    SqliteExecutionStore,
)
from backend.agents.run_history import AgentRunRecorder
from backend.agents.worker import AgentWorker


def _store():
    # max_attempts=1 mirrors the registry: a failed run is terminal (failed),
    # not rescheduled.
    return InMemoryExecutionStore(retry_policy=RetryPolicy(max_attempts=1))


def _event_payload(event_id="evt-1", event_type="node.create"):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "origin": {
            "event_origin": "graph",
            "event_session_id": "sess-1",
            "event_correlation_id": "corr-1",
        },
    }


class TestRecorder:
    def test_record_start_creates_running_run(self):
        rec = AgentRunRecorder(_store())
        run_id = rec.record_start("agent-1", "Agent One", _event_payload())
        assert run_id is not None

        runs = rec.list_runs()
        assert len(runs) == 1
        run = runs[0]
        assert run["id"] == run_id
        assert run["agent_id"] == "agent-1"
        assert run["agent_name"] == "Agent One"
        assert run["status"] == "running"
        assert run["trigger"] == "event"
        assert run["event_type"] == "node.create"
        assert run["correlation_id"] == "corr-1"
        assert run["session_id"] == "sess-1"
        assert run["started_at"] is not None
        assert run["finished_at"] is None

    def test_scheduled_trigger_maps_to_scheduled_kind(self):
        rec = AgentRunRecorder(_store())
        rec.record_start("agent-1", "A", _event_payload(event_type="scheduled_trigger"))
        assert rec.list_runs()[0]["trigger"] == "scheduled"

    def test_record_success_marks_succeeded(self):
        rec = AgentRunRecorder(_store())
        run_id = rec.record_start("agent-1", "A", _event_payload())
        rec.record_success(run_id, result={"handled": True, "turns": 2})

        run = rec.get_run(run_id)
        assert run["status"] == "succeeded"
        assert run["result"] == {"handled": True, "turns": 2}
        assert run["finished_at"] is not None
        assert run["error"] is None

    def test_record_failure_marks_failed(self):
        rec = AgentRunRecorder(_store())
        run_id = rec.record_start("agent-1", "A", _event_payload())
        rec.record_failure(run_id, "boom")

        run = rec.get_run(run_id)
        assert run["status"] == "failed"
        assert run["error"] == "boom"
        assert run["finished_at"] is not None

    def test_reenqueue_same_event_is_deduplicated(self):
        rec = AgentRunRecorder(_store())
        first = rec.record_start("agent-1", "A", _event_payload(event_id="dup"))
        second = rec.record_start("agent-1", "A", _event_payload(event_id="dup"))
        assert first == second
        assert len(rec.list_runs()) == 1

    def test_durable_sqlite_store_records_terminal_outcomes(self, tmp_path):
        # The durable path is what this slice exists for: verify the recorder's
        # start→terminal flow against SqliteExecutionStore, not only in-memory.
        store = SqliteExecutionStore(
            tmp_path / "runs.db", retry_policy=RetryPolicy(max_attempts=1)
        )
        try:
            rec = AgentRunRecorder(store)
            ok = rec.record_start("agent-1", "A", _event_payload(event_id="ok"))
            rec.record_success(ok, {"handled": True})
            bad = rec.record_start("agent-1", "A", _event_payload(event_id="bad"))
            rec.record_failure(bad, "boom")

            assert rec.get_run(ok)["status"] == "succeeded"
            failed = rec.get_run(bad)
            assert failed["status"] == "failed"  # terminal, not rescheduled
            assert failed["attempts"] == 1
        finally:
            store.close()

    def test_none_store_is_noop(self):
        rec = AgentRunRecorder(None)
        assert rec.record_start("agent-1", "A", _event_payload()) is None
        # Finalizers on a missing run must not raise.
        rec.record_success(None, {"ok": True})
        rec.record_failure(None, "boom")
        assert rec.list_runs() == []
        assert rec.get_run("whatever") is None


class TestWorkerRecordsRuns:
    @pytest.fixture
    def config(self):
        return AgentConfig(
            agent_id="agent-001",
            name="Test Agent",
            enabled=True,
            prompts=AgentPrompts(task_prompt="Summarise events."),
            mcp_integration_ids=[],
        )

    @pytest.fixture
    def settings(self):
        return AgentsSettings(enabled=True, openai_api_key="test-key")

    def _worker(self, config, settings, recorder):
        worker = AgentWorker(
            config=config,
            settings=settings,
            mcp_loader=MagicMock(),
            graph_service=MagicMock(),
            run_recorder=recorder,
        )
        worker.mcp_loader.get_tool_definitions.return_value = []
        # Skip skill loading (no network) and inject a fake LLM client.
        worker._cached_skills = []
        worker._llm_client = MagicMock()
        return worker

    def _item(self, worker):
        from backend.agents.worker import EventItem

        return EventItem(event_id="evt-1", payload=_event_payload())

    def test_successful_processing_records_succeeded(self, config, settings):
        rec = AgentRunRecorder(_store())
        worker = self._worker(config, settings, rec)
        worker._llm_client.execute_with_tools.return_value = {
            "success": True,
            "final_response": "done",
            "trace": [],
            "turns": 1,
        }

        worker._process_event(self._item(worker))

        runs = rec.list_runs()
        assert len(runs) == 1
        assert runs[0]["status"] == "succeeded"
        assert runs[0]["agent_id"] == "agent-001"

    def test_failed_processing_records_failed(self, config, settings):
        rec = AgentRunRecorder(_store())
        worker = self._worker(config, settings, rec)
        worker._llm_client.execute_with_tools.side_effect = RuntimeError("boom")

        worker._process_event(self._item(worker))

        runs = rec.list_runs()
        assert len(runs) == 1
        assert runs[0]["status"] == "failed"
        assert "boom" in (runs[0]["error"] or "")

    def test_no_recorder_processes_without_error(self, config, settings):
        worker = self._worker(config, settings, None)
        worker._llm_client.execute_with_tools.return_value = {
            "success": True,
            "final_response": "done",
            "trace": [],
            "turns": 1,
        }
        # Must not raise when there is no recorder.
        worker._process_event(self._item(worker))


class TestRegistryReadSurface:
    def _registry(self):
        from backend.agents.registry import AgentRegistry

        registry = AgentRegistry(
            settings=AgentsSettings(enabled=True),
            graph_storage=MagicMock(),
            graph_service=MagicMock(),
            execution_store=_store(),
        )
        return registry

    def test_list_and_get_runs_with_filters(self):
        registry = self._registry()
        rec = registry._run_recorder

        sched = rec.record_start("a1", "One", _event_payload("e1", "scheduled_trigger"))
        evt = rec.record_start("a2", "Two", _event_payload("e2", "node.update"))
        rec.record_success(evt, {"ok": True})

        assert len(registry.list_runs()) == 2
        assert len(registry.list_runs(agent_id="a1")) == 1
        assert len(registry.list_runs(kind="scheduled")) == 1
        assert len(registry.list_runs(status="succeeded")) == 1
        assert len(registry.list_runs(status="running")) == 1

        one = registry.get_run(sched)
        assert one["agent_id"] == "a1"
        assert registry.get_run("missing") is None

    def test_unknown_filter_values_return_empty(self):
        registry = self._registry()
        registry._run_recorder.record_start("a1", "One", _event_payload())
        assert registry.list_runs(kind="bogus") == []
        assert registry.list_runs(status="bogus") == []
