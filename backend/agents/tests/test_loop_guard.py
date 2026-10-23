"""Agent chains must terminate even without subscription self-exclusion."""

from collections import deque
from unittest.mock import MagicMock

import pytest

from backend.agents.config import AgentConfig, AgentsSettings
from backend.agents.execution import InMemoryExecutionStore, RetryPolicy
from backend.agents.mcp_loader import MCPLoader, NamespacedTool
from backend.agents.run_history import AgentRunRecorder
from backend.agents.worker import AgentWorker
from backend.core import Node
from backend.core.events import (
    Event,
    EventContext,
    EventDispatcher,
    EventType,
    EntityKind,
)
from backend.core.events.causation import (
    AGENT_EVENT_RATE_LIMIT,
    AGENT_EVENT_RATE_WINDOW_SECONDS,
    MAX_AGENT_CAUSATION_DEPTH,
    agent_mutation_context,
)
from backend.core.events.models import EntityData
from backend.service import GraphService
from backend.service.access import build_event_context


class LoopStorage:
    """Emit updates through the real service, dispatcher, and agent executor."""

    def __init__(self, subscriptions):
        target = Node(id="target", name="Target", type="Initiative")
        self.nodes = {node.id: node for node in [target, *subscriptions]}
        self.contexts = []
        self.vector_store = None
        self.dispatcher = EventDispatcher(self)

    def get_node(self, node_id):
        return self.nodes.get(node_id)

    def update_node(self, node_id, updates, *, event_context, **kwargs):
        self.nodes[node_id] = self.nodes[node_id].model_copy(update=updates)
        self.contexts.append(event_context)
        self.dispatcher.dispatch(
            Event(
                event_type=EventType.NODE_UPDATE,
                origin=event_context,
                entity=EntityData(
                    kind=EntityKind.NODE,
                    id=node_id,
                    type="Initiative",
                    after=self.nodes[node_id].to_dict(),
                ),
            )
        )
        return self.nodes[node_id]


@pytest.mark.parametrize(
    "agent_ids", [("self",), ("a", "b")], ids=["self-retrigger", "ping-pong"]
)
def test_agent_mutation_chains_terminate_at_default_hop_limit(agent_ids):
    subscriptions = [
        Node(
            id=f"sub-{agent_id}",
            name=agent_id,
            type="EventSubscription",
            metadata={
                "filters": {
                    "target": {"entity_kind": "node", "node_types": ["Initiative"]},
                    "operations": ["update"],
                },
                "delivery": {
                    "webhook_url": f"internal://agent/{agent_id}",
                    # Ping-pong excludes self; self-retrigger deliberately does not.
                    "ignore_origins": (
                        [f"agent:{agent_id}"] if len(agent_ids) > 1 else []
                    ),
                },
            },
        )
        for agent_id in agent_ids
    ]
    storage = LoopStorage(subscriptions)
    service = GraphService(storage)
    loader = MCPLoader([])
    loader._tools_cache = {
        "GRAPH__update_node": NamespacedTool(
            "GRAPH", "update_node", "GRAPH__update_node", "Update", {}
        )
    }
    history = AgentRunRecorder(
        InMemoryExecutionStore(retry_policy=RetryPolicy(max_attempts=1))
    )
    workers = {}
    pending = deque()

    for agent_id in agent_ids:
        worker = AgentWorker(
            config=AgentConfig(
                agent_id=agent_id,
                name=agent_id,
                enabled=True,
                mcp_integration_ids=["GRAPH"],
            ),
            settings=AgentsSettings(),
            mcp_loader=loader,
            graph_service=service,
            run_recorder=history,
        )
        worker._cached_skills = []
        worker._llm_client = MagicMock()

        def write_once(*, tool_executor, **kwargs):
            result = tool_executor(
                "GRAPH__update_node",
                {"node_id": "target", "updates": {"description": "changed"}},
            )
            assert result.get("success"), result
            return {
                "success": True,
                "response": '{"handled": true, "summary": "updated"}',
            }

        worker._llm_client.execute_with_tools.side_effect = write_once
        workers[f"sub-{agent_id}"] = worker

    def deliver(event, subscription_id):
        worker = workers[subscription_id]
        if not worker.enqueue(event.to_webhook_payload()):
            return False
        pending.append(worker)
        return True

    storage.dispatcher.set_agent_delivery_callback(deliver)
    webhook_delivery = MagicMock()
    storage.dispatcher.set_delivery_callback(webhook_delivery)
    # Seed only one agent, even when the initiating user event matches both.
    first = next(iter(workers.values()))
    assert first.enqueue(
        {"event_id": "seed", "event_type": "node.update", "origin": {}}
    )
    pending.append(first)
    iterations = 0
    while pending and iterations <= MAX_AGENT_CAUSATION_DEPTH:
        worker = pending.popleft()
        worker._process_event(worker._queue.get_nowait())
        iterations += 1

    assert not pending
    webhook_delivery.assert_not_called()
    assert iterations == MAX_AGENT_CAUSATION_DEPTH
    assert [ctx.event_causation_depth for ctx in storage.contexts] == list(
        range(1, MAX_AGENT_CAUSATION_DEPTH + 1)
    )
    assert {ctx.event_correlation_id for ctx in storage.contexts} == {"seed"}
    assert not any(worker.get_status()["paused"] for worker in workers.values())
    failed_runs = [run for run in history.list_runs() if run["status"] == "failed"]
    assert len(failed_runs) == 1
    assert "causation depth limit" in failed_runs[0]["error"]


def test_burst_preserves_queue_and_throttles_temporarily(monkeypatch):
    worker = AgentWorker(AgentConfig(agent_id="a", name="A"), AgentsSettings(), None)
    clock = [100.0]
    monkeypatch.setattr("backend.agents.worker.time.monotonic", lambda: clock[0])
    for i in range(AGENT_EVENT_RATE_LIMIT + 5):
        assert worker.enqueue({"event_id": str(i)})
    for _ in range(AGENT_EVENT_RATE_LIMIT):
        assert worker._processing_delay() == 0
    assert worker._processing_delay() == AGENT_EVENT_RATE_WINDOW_SECONDS
    assert worker.get_status()["paused"]
    assert worker.queue_size == AGENT_EVENT_RATE_LIMIT + 5
    clock[0] += AGENT_EVENT_RATE_WINDOW_SECONDS
    assert worker._processing_delay() == 0
    assert not worker.get_status()["paused"]
    assert [worker._queue.get_nowait().event_id for _ in range(worker.queue_size)] == [
        str(i) for i in range(AGENT_EVENT_RATE_LIMIT + 5)
    ]


def test_event_rate_window_expires_without_pausing(monkeypatch):
    worker = AgentWorker(AgentConfig(agent_id="a", name="A"), AgentsSettings(), None)
    monkeypatch.setattr("backend.agents.worker.time.monotonic", lambda: 100)
    for i in range(AGENT_EVENT_RATE_LIMIT):
        assert worker.enqueue({"event_id": f"e-{i}"})
    monkeypatch.setattr(
        "backend.agents.worker.time.monotonic",
        lambda: 100 + AGENT_EVENT_RATE_WINDOW_SECONDS,
    )
    assert worker.enqueue({"event_id": "next-window"})
    assert not worker.get_status()["paused"]


@pytest.mark.parametrize("depth", [-1, True, "0", 1.5, MAX_AGENT_CAUSATION_DEPTH])
def test_invalid_or_exhausted_depth_never_reaches_llm(depth):
    worker = AgentWorker(AgentConfig(agent_id="a", name="A"), AgentsSettings(), None)
    assert not worker.enqueue({"origin": {"event_causation_depth": depth}})
    assert worker.queue_size == 0
    assert not worker.get_status()["paused"]
    assert worker.enqueue({"event_id": "unrelated"})


def test_agent_context_cannot_be_reset_by_tool_arguments_and_does_not_leak():
    with pytest.raises(RuntimeError):
        with agent_mutation_context(
            "a", {"event_id": "source", "origin": {"event_causation_depth": 2}}
        ):
            context = build_event_context(
                target="update_node",
                event_origin="web-ui",
                event_correlation_id="spoofed",
            )
            assert context.event_origin == "agent:a"
            assert context.event_causation_depth == 3
            assert context.event_correlation_id == "source"
            assert context.to_dict()["event_causation_depth"] == 3
            raise RuntimeError("tool failed")
    assert (
        build_event_context(
            target="update_node", event_origin="web-ui"
        ).event_causation_depth
        == 0
    )
    assert EventContext().event_causation_depth == 0
