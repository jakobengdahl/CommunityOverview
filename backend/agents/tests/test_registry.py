"""
Tests for agent registry.
"""

import pytest
from unittest.mock import MagicMock

from backend.agents.config import (
    AgentConfig,
    AgentSchedule,
    AgentsSettings,
    MCPIntegration,
    MCPTransport,
)
from backend.agents.registry import AgentRegistry


class TestAgentRegistry:
    """Tests for AgentRegistry functionality."""

    @pytest.fixture
    def disabled_settings(self):
        """Settings with agents disabled."""
        return AgentsSettings(enabled=False)

    @pytest.fixture
    def enabled_settings(self):
        """Settings with agents enabled."""
        return AgentsSettings(
            enabled=True,
            llm_provider="openai",
            openai_api_key="test-key",
            mcp_integrations=[
                MCPIntegration(
                    id="GRAPH",
                    name="Graph API",
                    transport=MCPTransport.HTTP,
                    url="http://localhost:8000/mcp",
                )
            ],
        )

    def test_registry_disabled_by_default(
        self, mock_storage, mock_service, disabled_settings
    ):
        """Test that registry is disabled when settings.enabled is False."""
        registry = AgentRegistry(
            settings=disabled_settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        assert registry.is_enabled is False

    def test_registry_enabled_when_configured(
        self, mock_storage, mock_service, enabled_settings
    ):
        """Test that registry is enabled when settings.enabled is True."""
        registry = AgentRegistry(
            settings=enabled_settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        assert registry.is_enabled is True

    def test_start_disabled_does_nothing(
        self, mock_storage, mock_service, disabled_settings
    ):
        """Test that start() does nothing when disabled."""
        registry = AgentRegistry(
            settings=disabled_settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        registry.start()  # Should not raise

        assert registry.list_workers() == []

    def test_subscription_agent_mapping(
        self, mock_storage, mock_service, enabled_settings
    ):
        """Test subscription to agent ID mapping."""
        registry = AgentRegistry(
            settings=enabled_settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        # Manually add a mapping (normally done by _start_worker)
        registry._subscription_agent_map["sub-001"] = "agent-001"

        assert registry.is_agent_subscription("sub-001") is True
        assert registry.is_agent_subscription("sub-unknown") is False
        assert registry.get_agent_for_subscription("sub-001") == "agent-001"
        assert registry.get_agent_for_subscription("sub-unknown") is None

    def test_get_all_status(self, mock_storage, mock_service, enabled_settings):
        """Test getting registry status."""
        registry = AgentRegistry(
            settings=enabled_settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        status = registry.get_all_status()

        assert "enabled" in status
        assert "worker_count" in status
        assert "subscription_count" in status
        assert "mcp_integrations" in status
        assert status["enabled"] is True
        assert status["worker_count"] == 0

    def test_list_workers_empty(self, mock_storage, mock_service, enabled_settings):
        """Test listing workers when none running."""
        registry = AgentRegistry(
            settings=enabled_settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        workers = registry.list_workers()

        assert workers == []


class TestAgentLifecycle:
    """Tests for agent lifecycle management."""

    @pytest.fixture
    def registry_with_agent(self, mock_storage, mock_service, sample_agent_node):
        """Create a registry with an agent node in storage."""
        mock_storage.nodes[sample_agent_node.id] = sample_agent_node

        settings = AgentsSettings(
            enabled=True,
            llm_provider="openai",
            openai_api_key="test-key",
            mcp_integrations=[],
        )

        return AgentRegistry(
            settings=settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

    def test_load_agents_finds_agent_nodes(
        self, registry_with_agent, sample_agent_node
    ):
        """Test that _load_agents finds Agent nodes in storage."""
        agents = registry_with_agent._load_agents()

        assert len(agents) == 1
        assert agents[0].agent_id == sample_agent_node.id
        assert agents[0].name == sample_agent_node.name

    def test_handle_agent_deleted_stops_worker(self, mock_storage, mock_service):
        """Test that deleting an agent stops its worker."""
        settings = AgentsSettings(enabled=True, mcp_integrations=[])
        registry = AgentRegistry(
            settings=settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        # Mock a running worker
        mock_worker = MagicMock()
        registry._workers["agent-001"] = mock_worker
        registry._subscription_agent_map["sub-001"] = "agent-001"
        mock_worker.config = MagicMock(subscription_id="sub-001")

        registry.handle_agent_deleted("agent-001")

        # Worker should have been stopped
        mock_worker.stop.assert_called_once_with(wait=True)
        assert "agent-001" not in registry._workers
        assert "sub-001" not in registry._subscription_agent_map


class TestEventRouting:
    """Tests for event routing to agents."""

    @pytest.fixture
    def registry_with_worker(self, mock_storage, mock_service):
        """Create a registry with a mock worker."""
        settings = AgentsSettings(enabled=True, mcp_integrations=[])
        registry = AgentRegistry(
            settings=settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        mock_worker = MagicMock()
        registry._workers["agent-001"] = mock_worker
        registry._subscription_agent_map["sub-001"] = "agent-001"

        return registry, mock_worker

    def test_enqueue_for_subscription_routes_to_agent(
        self, registry_with_worker, sample_event_payload
    ):
        """Test that enqueue_for_subscription routes events to the correct agent."""
        registry, mock_worker = registry_with_worker

        result = registry.enqueue_for_subscription("sub-001", sample_event_payload)

        assert result is True
        mock_worker.enqueue.assert_called_once_with(sample_event_payload)

    def test_enqueue_for_subscription_unknown_subscription(
        self, registry_with_worker, sample_event_payload
    ):
        """Test that unknown subscriptions return False."""
        registry, mock_worker = registry_with_worker

        result = registry.enqueue_for_subscription("sub-unknown", sample_event_payload)

        assert result is False
        mock_worker.enqueue.assert_not_called()

    def test_enqueue_direct_to_agent(self, registry_with_worker, sample_event_payload):
        """Test enqueuing directly to an agent by ID."""
        registry, mock_worker = registry_with_worker

        result = registry.enqueue("agent-001", sample_event_payload)

        assert result is True
        mock_worker.enqueue.assert_called_once_with(sample_event_payload)

    @pytest.mark.parametrize("via_subscription", [False, True])
    def test_guard_rejection_is_not_reported_as_delivery(
        self, registry_with_worker, sample_event_payload, via_subscription
    ):
        registry, mock_worker = registry_with_worker
        mock_worker.enqueue.return_value = False

        if via_subscription:
            result = registry.enqueue_for_subscription("sub-001", sample_event_payload)
        else:
            result = registry.enqueue("agent-001", sample_event_payload)

        assert result is False

    def test_enqueue_unknown_agent_returns_false(
        self, registry_with_worker, sample_event_payload
    ):
        """Test that enqueue to unknown agent returns False."""
        registry, mock_worker = registry_with_worker

        result = registry.enqueue("agent-unknown", sample_event_payload)

        assert result is False


class TestGetAvailableIntegrations:
    """Tests for getting available MCP integrations."""

    def test_returns_configured_integrations(self, mock_storage, mock_service):
        """Test that configured integrations are returned."""
        settings = AgentsSettings(
            enabled=True,
            mcp_integrations=[
                MCPIntegration(
                    id="GRAPH",
                    name="Graph API",
                    transport=MCPTransport.HTTP,
                    url="http://localhost:8000/mcp",
                    description="Graph tools",
                ),
                MCPIntegration(
                    id="WEB",
                    name="Web Fetch",
                    transport=MCPTransport.STDIO,
                    command=["node", "mcp-web"],
                    description="Web search",
                ),
            ],
        )

        registry = AgentRegistry(
            settings=settings,
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        integrations = registry.get_available_mcp_integrations()

        assert len(integrations) == 2
        ids = [i["id"] for i in integrations]
        assert "GRAPH" in ids
        assert "WEB" in ids

    def test_get_schedules_returns_agents_with_schedule(
        self, mock_storage, mock_service
    ):
        """get_schedules() includes cron expression for SaaS/external scheduler use."""
        registry = AgentRegistry(
            settings=AgentsSettings(enabled=False),
            graph_storage=mock_storage,
            graph_service=mock_service,
        )

        # Inject a fake worker directly so we can test without starting MCP
        scheduled_config = AgentConfig(
            agent_id="agent-sched",
            name="Scheduled Agent",
            schedule=AgentSchedule(
                day_of_week=1, hour=14, minute=0, timezone="Europe/Stockholm"
            ),
        )
        unscheduled_config = AgentConfig(agent_id="agent-nosched", name="No Schedule")

        mock_worker_a = MagicMock()
        mock_worker_a.config = scheduled_config
        mock_worker_b = MagicMock()
        mock_worker_b.config = unscheduled_config

        registry._workers = {
            "agent-sched": mock_worker_a,
            "agent-nosched": mock_worker_b,
        }

        schedules = registry.get_schedules()

        assert len(schedules) == 1
        entry = schedules[0]
        assert entry["agent_id"] == "agent-sched"
        assert entry["trigger_path"] == "/agents/agent-sched/trigger"
        assert entry["schedule"]["cron"] == "0 14 * * 2"  # Tuesday in cron
        assert entry["schedule"]["timezone"] == "Europe/Stockholm"
        assert entry["schedule"]["day_name"] == "Tuesday"

    def test_get_schedules_empty_when_no_agents_scheduled(
        self, mock_storage, mock_service
    ):
        registry = AgentRegistry(
            settings=AgentsSettings(enabled=False),
            graph_storage=mock_storage,
            graph_service=mock_service,
        )
        assert registry.get_schedules() == []

    def test_trigger_agent_returns_false_for_unknown_agent(
        self, mock_storage, mock_service
    ):
        registry = AgentRegistry(
            settings=AgentsSettings(enabled=False),
            graph_storage=mock_storage,
            graph_service=mock_service,
        )
        assert registry.trigger_agent("no-such-agent") is False

    def test_trigger_agent_returns_false_when_agent_has_no_schedule(
        self, mock_storage, mock_service
    ):
        registry = AgentRegistry(
            settings=AgentsSettings(enabled=False),
            graph_storage=mock_storage,
            graph_service=mock_service,
        )
        unscheduled_config = AgentConfig(agent_id="agent-1", name="No Schedule")
        mock_worker = MagicMock()
        mock_worker.config = unscheduled_config
        registry._workers = {"agent-1": mock_worker}

        assert registry.trigger_agent("agent-1") is False
        mock_worker.enqueue.assert_not_called()

    def test_trigger_agent_enqueues_payload_for_scheduled_agent(
        self, mock_storage, mock_service
    ):
        registry = AgentRegistry(
            settings=AgentsSettings(enabled=False),
            graph_storage=mock_storage,
            graph_service=mock_service,
        )
        config = AgentConfig(
            agent_id="agent-1",
            name="Scheduled",
            schedule=AgentSchedule(day_of_week=1, hour=14, minute=0),
        )
        enqueued = []
        mock_worker = MagicMock()
        mock_worker.config = config
        mock_worker.enqueue.side_effect = enqueued.append
        registry._workers = {"agent-1": mock_worker}

        result = registry.trigger_agent("agent-1")

        assert result is True
        assert len(enqueued) == 1
        payload = enqueued[0]
        assert payload["event_type"] == "scheduled_trigger"
        assert payload["schedule"]["day_name"] == "Tuesday"

    def test_trigger_agent_reports_guard_rejection(self, mock_storage, mock_service):
        registry = AgentRegistry(
            settings=AgentsSettings(enabled=False),
            graph_storage=mock_storage,
            graph_service=mock_service,
        )
        worker = MagicMock()
        worker.config = AgentConfig(
            agent_id="agent-1",
            name="Paused",
            schedule=AgentSchedule(day_of_week=1, hour=14, minute=0),
        )
        worker.enqueue.return_value = False
        registry._workers = {"agent-1": worker}
        assert registry.trigger_agent("agent-1") is False


class TestHandleAgentUpdated:
    """An Agent node update must leave the runtime matching the node."""

    @pytest.fixture
    def registry(self, mock_storage, mock_service, monkeypatch):
        """Registry whose workers are mocks, so no threads or LLM clients start."""
        registry = AgentRegistry(
            settings=AgentsSettings(enabled=True, mcp_integrations=[]),
            graph_storage=mock_storage,
            graph_service=mock_service,
        )
        registry._mcp_loader = MagicMock()  # _ensure_initialized short-circuits

        def make_worker(config, **kwargs):
            worker = MagicMock()
            worker.config = config
            worker.reload_config.side_effect = lambda c: setattr(worker, "config", c)
            return worker

        monkeypatch.setattr("backend.agents.registry.AgentWorker", make_worker)
        return registry

    @staticmethod
    def _put_agent(storage, sample_agent_node, **metadata):
        sample_agent_node.metadata = {**sample_agent_node.metadata, **metadata}
        storage.nodes[sample_agent_node.id] = sample_agent_node

    def test_enabling_agent_starts_worker_and_binds_subscription(
        self, registry, mock_storage, sample_agent_node
    ):
        self._put_agent(mock_storage, sample_agent_node, enabled=True)

        registry.handle_agent_updated("agent-001")

        worker = registry._workers["agent-001"]
        worker.start.assert_called_once()
        assert registry.get_agent_for_subscription("sub-001") == "agent-001"

    def test_enabling_agent_auto_enables_agent_system(
        self, registry, mock_storage, sample_agent_node
    ):
        registry.settings.enabled = False
        self._put_agent(mock_storage, sample_agent_node, enabled=True)

        registry.handle_agent_updated("agent-001")

        assert registry.settings.enabled is True
        assert "agent-001" in registry._workers

    def test_enabling_agent_without_runtime_starts_nothing(
        self, registry, mock_storage, sample_agent_node, monkeypatch
    ):
        monkeypatch.setattr(registry, "_ensure_initialized", lambda: False)
        self._put_agent(mock_storage, sample_agent_node, enabled=True)

        registry.handle_agent_updated("agent-001")

        assert registry._workers == {}
        assert registry.is_agent_subscription("sub-001") is False

    def test_disabling_agent_stops_worker_and_unbinds_subscription(
        self, registry, mock_storage, sample_agent_node
    ):
        self._put_agent(
            mock_storage,
            sample_agent_node,
            enabled=True,
            schedule={"day_of_week": 1, "time": "14:00"},
        )
        registry.handle_agent_updated("agent-001")
        worker = registry._workers["agent-001"]
        assert "agent-001" in registry._scheduler._entries

        self._put_agent(mock_storage, sample_agent_node, enabled=False)
        registry.handle_agent_updated("agent-001")

        worker.stop.assert_called_once_with(wait=True)
        assert "agent-001" not in registry._workers
        assert registry.is_agent_subscription("sub-001") is False
        assert "agent-001" not in registry._scheduler._entries
        assert registry.enqueue_for_subscription("sub-001", {"event_id": "e"}) is False

    def test_disabled_agent_without_worker_stays_stopped(
        self, registry, mock_storage, sample_agent_node
    ):
        self._put_agent(mock_storage, sample_agent_node, enabled=False)

        registry.handle_agent_updated("agent-001")

        assert registry._workers == {}
        assert registry.is_agent_subscription("sub-001") is False

    def test_changed_subscription_moves_binding_to_new_subscription(
        self, registry, mock_storage, sample_agent_node
    ):
        self._put_agent(mock_storage, sample_agent_node, enabled=True)
        registry.handle_agent_updated("agent-001")
        worker = registry._workers["agent-001"]

        self._put_agent(mock_storage, sample_agent_node, subscription_id="sub-002")
        registry.handle_agent_updated("agent-001")

        worker.reload_config.assert_called_once()
        assert worker.reload_config.call_args.args[0].subscription_id == "sub-002"
        worker.stop.assert_not_called()
        assert registry.is_agent_subscription("sub-001") is False
        assert registry.get_agent_for_subscription("sub-002") == "agent-001"
        assert registry.enqueue_for_subscription("sub-001", {"event_id": "e"}) is False
        assert registry.enqueue_for_subscription("sub-002", {"event_id": "e"}) is True

    def test_removed_subscription_unbinds_old_subscription(
        self, registry, mock_storage, sample_agent_node
    ):
        self._put_agent(mock_storage, sample_agent_node, enabled=True)
        registry.handle_agent_updated("agent-001")

        self._put_agent(mock_storage, sample_agent_node, subscription_id=None)
        registry.handle_agent_updated("agent-001")

        assert registry._subscription_agent_map == {}
        assert "agent-001" in registry._workers

    def test_reload_reschedules_with_new_config(
        self, registry, mock_storage, sample_agent_node, monkeypatch
    ):
        self._put_agent(mock_storage, sample_agent_node, enabled=True)
        registry.handle_agent_updated("agent-001")
        assert "agent-001" not in registry._scheduler._entries

        registry.settings.scheduler_enabled = True
        scheduler_start = MagicMock()
        monkeypatch.setattr(registry._scheduler, "start", scheduler_start)
        self._put_agent(
            mock_storage,
            sample_agent_node,
            schedule={"day_of_week": 1, "time": "14:00"},
        )
        registry.handle_agent_updated("agent-001")

        config, worker = registry._scheduler._entries["agent-001"]
        assert config.schedule is not None
        assert config.schedule.day_of_week == 1
        assert worker is registry._workers["agent-001"]
        scheduler_start.assert_called_once()

    def test_reload_drops_schedule_removed_from_node(
        self, registry, mock_storage, sample_agent_node
    ):
        self._put_agent(
            mock_storage,
            sample_agent_node,
            enabled=True,
            schedule={"day_of_week": 1, "time": "14:00"},
        )
        registry.handle_agent_updated("agent-001")
        assert "agent-001" in registry._scheduler._entries

        self._put_agent(mock_storage, sample_agent_node, schedule=None)
        registry.handle_agent_updated("agent-001")

        assert "agent-001" in registry._workers
        assert "agent-001" not in registry._scheduler._entries
        assert registry.get_schedules() == []

    def test_reload_replaces_changed_schedule(
        self, registry, mock_storage, sample_agent_node, monkeypatch
    ):
        scheduler_start = MagicMock()
        monkeypatch.setattr(registry._scheduler, "start", scheduler_start)
        self._put_agent(
            mock_storage,
            sample_agent_node,
            enabled=True,
            schedule={"day_of_week": 1, "time": "14:00"},
        )
        registry.handle_agent_updated("agent-001")

        self._put_agent(
            mock_storage,
            sample_agent_node,
            schedule={"day_of_week": "friday", "time": "09:30"},
        )
        registry.handle_agent_updated("agent-001")

        schedule = registry._scheduler._entries["agent-001"][0].schedule
        assert (schedule.day_of_week, schedule.hour, schedule.minute) == (4, 9, 30)
        scheduler_start.assert_not_called()  # scheduler_enabled defaults to False

    def test_deleted_node_stops_worker(self, registry, mock_storage, sample_agent_node):
        self._put_agent(
            mock_storage,
            sample_agent_node,
            enabled=True,
            schedule={"day_of_week": 1, "time": "14:00"},
        )
        registry.handle_agent_updated("agent-001")
        worker = registry._workers["agent-001"]
        assert "agent-001" in registry._scheduler._entries

        del mock_storage.nodes["agent-001"]
        registry.handle_agent_updated("agent-001")

        worker.stop.assert_called_once_with(wait=True)
        assert registry._workers == {}
        assert registry._subscription_agent_map == {}
        assert registry._scheduler._entries == {}

    def test_non_agent_node_is_ignored(self, registry, mock_storage, sample_agent_node):
        sample_agent_node.type = "Initiative"
        mock_storage.nodes[sample_agent_node.id] = sample_agent_node

        registry.handle_agent_updated("agent-001")

        assert registry._workers == {}

    def test_reload_clears_loop_guard_throttle(
        self, mock_storage, mock_service, sample_agent_node
    ):
        """An operator edit is the documented way out of a burst throttle (#1007)."""
        from backend.agents.worker import AgentWorker

        registry = AgentRegistry(
            settings=AgentsSettings(enabled=True, mcp_integrations=[]),
            graph_storage=mock_storage,
            graph_service=mock_service,
        )
        self._put_agent(mock_storage, sample_agent_node, enabled=True)
        worker = AgentWorker(
            config=AgentConfig.from_node(sample_agent_node),
            settings=registry.settings,
            mcp_loader=None,
            graph_service=mock_service,
        )
        registry._workers["agent-001"] = worker
        registry._subscription_agent_map["sub-001"] = "agent-001"
        worker._pause_reason = "Agent temporarily paused: event rate limit"
        worker._event_admissions.extend([0.0, 1.0, 2.0])

        registry.handle_agent_updated("agent-001")

        assert registry._workers["agent-001"] is worker
        assert worker._pause_reason is None
        assert len(worker._event_admissions) == 0
