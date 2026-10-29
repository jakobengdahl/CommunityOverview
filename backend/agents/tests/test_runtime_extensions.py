"""Admission cannot bypass guards, and diagnostics cannot block them."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.agents.config import AgentConfig, AgentsSettings
from backend.agents.mcp_loader import MCPLoader
from backend.agents.llm_client import LLMClient
from backend.agents.worker import AgentWorker
from backend.core.events.causation import (
    AGENT_EVENT_RATE_LIMIT,
    MAX_AGENT_CAUSATION_DEPTH,
)
from backend.llm.llm_providers import ClaudeProvider, OpenAIProvider
from backend.runtime.authorization import use_request_authorization
from backend.runtime.extensions import (
    AdmissionDecision,
    AdmissionRefused,
    RuntimeExtensions,
    bind_runtime_extensions,
)


def worker(extensions):
    instance = AgentWorker(
        AgentConfig(agent_id="agent", name="Agent", enabled=True),
        AgentsSettings(),
        MCPLoader([]),
        runtime_extensions=extensions,
    )
    instance._cached_skills = []
    instance._llm_client = Mock()
    instance._llm_client.execute_with_tools.return_value = {
        "success": True,
        "response": '{"handled": true, "summary": "Reviewed"}',
    }
    return instance


def test_deny_runs_core_guard_but_never_starts_agent_work():
    hook = Mock()
    hook.admit.return_value = AdmissionDecision("deny", "work_blocked")
    w = worker(RuntimeExtensions(execution_admission=hook))
    w.enqueue({"event_id": "event"})
    w._process_event(w._queue.get_nowait())
    assert len(w._event_admissions) == 1
    w._llm_client.execute_with_tools.assert_not_called()
    assert w.events_failed == 1
    assert hook.admit.call_args.args[0].run_id


def test_deferral_retries_with_snapshot_scope_correlation_and_server_attempt():
    hook = Mock()
    hook.admit.side_effect = [
        AdmissionDecision("defer", "wait", time.time()),
        AdmissionDecision(),
        AdmissionDecision(),
    ]
    extensions = RuntimeExtensions(execution_admission=hook)
    w = worker(extensions)
    payload = {
        "event_id": "event",
        "origin": {"event_causation_depth": 2, "event_correlation_id": "corr"},
        "scope": {"graph_id": "untrusted"},
        "attempt": 999,
    }
    with use_request_authorization(actor_id="caller", graph_id="trusted"):
        assert w.enqueue(payload)
    payload["origin"]["event_causation_depth"] = MAX_AGENT_CAUSATION_DEPTH
    payload["origin"]["event_correlation_id"] = "changed"
    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider.model = "model"
    provider.client = Mock()
    provider.client.messages.create.return_value = Mock(
        content=[], stop_reason="end_turn"
    )

    def execute(**kwargs):
        provider.create_completion([], "", [])
        return {"success": True, "response": '{"handled": true, "summary": "Reviewed"}'}

    w._llm_client.execute_with_tools.side_effect = execute
    w._process_event(w._queue.get_nowait())
    contexts = [call.args[0] for call in hook.admit.call_args_list]
    assert [c.attempt for c in contexts] == [1, 2, 2]
    assert [c.operation for c in contexts] == [
        "agent_work",
        "agent_work",
        "model_invocation",
    ]
    assert len({c.run_id for c in contexts}) == 1
    assert all(c.agent_id == "agent" and c.event_id == "event" for c in contexts)
    assert all(c.correlation_id == "corr" and c.causation_depth == 3 for c in contexts)
    assert all(c.scope["graph_id"] == "trusted" for c in contexts)
    assert len(w._event_admissions) == 2
    assert w.events_processed == 1


@pytest.mark.parametrize("provider_class", [ClaudeProvider, OpenAIProvider])
@pytest.mark.parametrize("outcome", ["deny", "defer"])
def test_provider_invocation_is_admitted_before_network(provider_class, outcome):
    hook = Mock()
    hook.admit.return_value = AdmissionDecision(
        outcome, "model_blocked", time.time() + 10000
    )
    provider = provider_class.__new__(provider_class)
    provider.model = "model"
    provider.client = Mock()
    provider._tool_calling = True
    with bind_runtime_extensions(RuntimeExtensions(execution_admission=hook)):
        with pytest.raises(AdmissionRefused) as error:
            provider.create_completion([], "", [])
    assert error.value.code == "model_blocked"
    if outcome == "defer":
        assert time.time() < error.value.retry_at <= time.time() + 60
    provider.client.messages.create.assert_not_called()
    provider.client.chat.completions.create.assert_not_called()


def test_permissive_admission_cannot_raise_depth_or_rate_limits():
    hook = Mock()
    hook.admit.return_value = AdmissionDecision()
    w = worker(RuntimeExtensions(execution_admission=hook))
    assert not w.enqueue(
        {"origin": {"event_causation_depth": MAX_AGENT_CAUSATION_DEPTH}}
    )
    assert w.queue_size == 0
    for _ in range(AGENT_EVENT_RATE_LIMIT):
        assert w._processing_delay() == 0
    assert w._processing_delay() > 0


def test_model_deferral_retries_current_invocation_without_replaying_tools():
    hook = Mock()
    hook.admit.side_effect = [
        AdmissionDecision(),
        AdmissionDecision(),
        AdmissionDecision("defer", "wait", time.time()),
        AdmissionDecision(),
    ]
    w = worker(RuntimeExtensions(execution_admission=hook))
    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider.model, provider.client = "model", Mock()
    provider.client.messages.create.side_effect = [
        SimpleNamespace(
            content=[
                SimpleNamespace(type="tool_use", id="call", name="read", input={})
            ],
            stop_reason="tool_use",
        ),
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="text", text='{"handled": true, "summary": "Reviewed"}'
                )
            ],
            stop_reason="end_turn",
        ),
    ]
    w._llm_client = LLMClient(llm_provider=provider)
    execute = Mock(return_value={"ok": True})
    w.mcp_loader = Mock()
    w.mcp_loader.get_tool_definitions.return_value = []
    w.mcp_loader.create_tool_executor.return_value = execute
    w.enqueue(
        {
            "event_id": "event",
            "origin": {"event_causation_depth": 2, "event_correlation_id": "corr"},
        }
    )
    w._process_event(w._queue.get_nowait())
    execute.assert_called_once_with("read", {})
    assert provider.client.messages.create.call_count == 2
    contexts = [call.args[0] for call in hook.admit.call_args_list]
    assert [c.attempt for c in contexts] == [1, 1, 1, 2]
    assert all(c.correlation_id == "corr" and c.causation_depth == 3 for c in contexts)
    assert len({c.run_id for c in contexts}) == 1
    assert w.events_processed == 1


def test_slow_observer_does_not_block_guard_and_queue_is_bounded():
    entered, release = threading.Event(), threading.Event()

    class SlowSink:
        def emit(self, event):
            entered.set()
            release.wait(2)

    w = worker(RuntimeExtensions(event_sink=SlowSink()))
    invalid = {"origin": {"event_causation_depth": MAX_AGENT_CAUSATION_DEPTH}}
    try:
        assert not w.enqueue(invalid)
        assert entered.wait(1)
        finished = threading.Event()

        def reject_many():
            for _ in range(150):
                assert not w.enqueue(invalid)
            finished.set()

        thread = threading.Thread(target=reject_many, daemon=True)
        thread.start()
        assert finished.wait(1)
        assert w._extensions.event_sink._queue.qsize() <= 128
    finally:
        release.set()


def test_observer_exceptions_are_isolated_and_guard_transitions_have_context(
    monkeypatch,
):
    received = []
    recovered = threading.Event()

    class BrokenSink:
        def emit(self, event):
            received.append(event)
            if event.event_type == "loop_guard.recovered":
                recovered.set()
            raise RuntimeError("observer unavailable")

    w = worker(RuntimeExtensions(event_sink=BrokenSink()))
    assert not w.enqueue(
        {
            "event_id": "source",
            "origin": {"event_causation_depth": 8, "event_correlation_id": "corr"},
        }
    )
    clock = [100.0]
    monkeypatch.setattr("backend.agents.worker.time.monotonic", lambda: clock[0])
    for _ in range(AGENT_EVENT_RATE_LIMIT):
        w._processing_delay()
    assert w._processing_delay() > 0
    assert w._processing_delay() > 0
    clock[0] += 61
    assert w._processing_delay() == 0
    assert recovered.wait(1)
    assert [e.event_type for e in received] == [
        "loop_guard.rejected",
        "loop_guard.throttled",
        "loop_guard.recovered",
    ]
    assert received[0].context.correlation_id == "corr"
    assert all(
        e.version == 1 and e.timestamp and e.resource_ids == ("agent",)
        for e in received
    )


@pytest.mark.parametrize("decision", [AdmissionDecision("defer", "wait"), None])
def test_invalid_admission_fails_closed(decision):
    hook = Mock()
    hook.admit.return_value = decision
    assert (
        RuntimeExtensions(execution_admission=hook).admit(Mock()).code
        == "execution_admission_error"
    )
