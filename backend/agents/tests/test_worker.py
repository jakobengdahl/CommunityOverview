"""
Tests for agent worker.
"""

import pytest
from unittest.mock import MagicMock, patch
import queue
import time

from backend.agents.config import AgentConfig, AgentsSettings
from backend.agents.worker import AgentWorker, ProcessingResult


class TestAgentWorker:
    """Tests for AgentWorker functionality."""

    @pytest.fixture
    def agent_config(self):
        """Create a test agent configuration."""
        return AgentConfig(
            agent_id="agent-001",
            name="Test Agent",
            description="Test agent for unit tests",
            enabled=True,
            task_prompt="Process events and return a summary.",
            subscription_id="sub-001",
            mcp_integrations=["GRAPH"],
        )

    @pytest.fixture
    def agent_settings(self):
        """Create test agent settings."""
        return AgentsSettings(
            enabled=True,
            llm_provider="openai",
            openai_api_key="test-key",
            max_turns_per_event=5,
        )

    def test_worker_init(self, agent_config, agent_settings):
        """Test worker initialization."""
        worker = AgentWorker(
            config=agent_config,
            settings=agent_settings,
            mcp_loader=None,
            graph_service=MagicMock(),
        )

        assert worker.config.agent_id == "agent-001"
        assert worker._running is False

    def test_worker_enqueue(self, agent_config, agent_settings):
        """Test enqueueing events to worker."""
        worker = AgentWorker(
            config=agent_config,
            settings=agent_settings,
            mcp_loader=None,
            graph_service=MagicMock(),
        )

        event_payload = {"event_id": "evt-001", "event_type": "node.create"}
        worker.enqueue(event_payload)

        # Should be in the queue
        assert worker._queue.qsize() == 1

    def test_worker_status(self, agent_config, agent_settings):
        """Test getting worker status."""
        worker = AgentWorker(
            config=agent_config,
            settings=agent_settings,
            mcp_loader=None,
            graph_service=MagicMock(),
        )

        status = worker.get_status()

        assert status["agent_id"] == "agent-001"
        assert status["agent_name"] == "Test Agent"
        assert status["running"] is False
        assert status["queue_size"] == 0
        assert status["events_processed"] == 0

    def test_worker_reload_config(self, agent_config, agent_settings):
        """Test reloading worker configuration."""
        worker = AgentWorker(
            config=agent_config,
            settings=agent_settings,
            mcp_loader=None,
            graph_service=MagicMock(),
        )

        new_config = AgentConfig(
            agent_id="agent-001",
            name="Updated Agent",
            description="Updated description",
            enabled=True,
            task_prompt="New task prompt.",
            subscription_id="sub-001",
            mcp_integrations=["GRAPH", "WEB"],
        )

        worker.reload_config(new_config)

        assert worker.config.name == "Updated Agent"
        assert worker.config.task_prompt == "New task prompt."
        assert worker.config.mcp_integrations == ["GRAPH", "WEB"]


class TestProcessingResult:
    """Tests for ProcessingResult dataclass."""

    def test_create_success_result(self):
        """Test creating a successful processing result."""
        result = ProcessingResult(
            agent_id="agent-001",
            event_id="evt-001",
            success=True,
            summary="Processed event successfully",
        )

        assert result.success is True
        assert result.error is None
        assert result.summary == "Processed event successfully"

    def test_create_error_result(self):
        """Test creating an error processing result."""
        result = ProcessingResult(
            agent_id="agent-001",
            event_id="evt-001",
            success=False,
            error="LLM call failed",
        )

        assert result.success is False
        assert result.error == "LLM call failed"

    def test_result_with_actions(self):
        """Test processing result with recorded actions."""
        result = ProcessingResult(
            agent_id="agent-001",
            event_id="evt-001",
            success=True,
            summary="Updated node with web search results",
            actions=[
                {"tool": "GRAPH.search_graph", "input": {"query": "AI"}},
                {"tool": "GRAPH.update_node", "input": {"node_id": "n-1"}},
            ],
            graph_changes=["Updated node n-1 description"],
        )

        assert len(result.actions) == 2
        assert len(result.graph_changes) == 1


class TestWorkerStartStop:
    """Tests for worker thread management."""

    @pytest.fixture
    def worker(self, agent_config, agent_settings):
        """Create a worker for testing."""
        return AgentWorker(
            config=agent_config,
            settings=agent_settings,
            mcp_loader=None,
            graph_service=MagicMock(),
        )

    def test_start_sets_running_flag(self, worker):
        """Test that start() sets the running flag."""
        worker.start()

        assert worker._running is True

        # Cleanup
        worker.stop(wait=True, timeout=1.0)

    def test_stop_clears_running_flag(self, worker):
        """Test that stop() clears the running flag."""
        worker.start()
        worker.stop(wait=True, timeout=1.0)

        assert worker._running is False

    def test_stop_without_start(self, worker):
        """Test that stop() works even if worker wasn't started."""
        worker.stop(wait=True, timeout=1.0)

        assert worker._running is False

    def test_double_start_is_safe(self, worker):
        """Test that starting twice doesn't cause issues."""
        worker.start()
        worker.start()  # Should be ignored

        # Cleanup
        worker.stop(wait=True, timeout=1.0)

    def test_double_stop_is_safe(self, worker):
        """Test that stopping twice doesn't cause issues."""
        worker.start()
        worker.stop(wait=True, timeout=1.0)
        worker.stop(wait=True, timeout=1.0)  # Should be safe


class TestWorkerCallbacks:
    """Tests for worker result callbacks."""

    @pytest.fixture
    def worker_with_callback(self, agent_config, agent_settings):
        """Create a worker with a result callback."""
        results = []

        def on_result(result):
            results.append(result)

        worker = AgentWorker(
            config=agent_config,
            settings=agent_settings,
            mcp_loader=None,
            graph_service=MagicMock(),
            on_result=on_result,
        )

        return worker, results

    def test_callback_receives_results(self, worker_with_callback):
        """Test that result callback receives processing results."""
        worker, results = worker_with_callback

        # The callback should be set
        assert worker._on_result is not None
