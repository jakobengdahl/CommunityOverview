"""
Tests for agent worker.
"""

import threading
from dataclasses import replace

import pytest
from unittest.mock import MagicMock

from backend.agents.config import AgentConfig, AgentsSettings, AgentPrompts
from backend.agents.worker import AgentWorker, ProcessingResult
from backend.agents.tests.conftest import MockNode
from backend.skills.loader import SkillDefinition


@pytest.fixture
def agent_config():
    """Create a test agent configuration."""
    return AgentConfig(
        agent_id="agent-001",
        name="Test Agent",
        # description field removed
        enabled=True,
        prompts=AgentPrompts(task_prompt="Process events and return a summary."),
        subscription_id="sub-001",
        mcp_integration_ids=["GRAPH"],
    )


@pytest.fixture
def agent_settings():
    """Create test agent settings."""
    return AgentsSettings(
        enabled=True,
        llm_provider="openai",
        openai_api_key="test-key",
        max_agent_turns=5,
    )


class TestAgentWorker:
    """Tests for AgentWorker functionality."""

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
            # description removed
            enabled=True,
            prompts=AgentPrompts(task_prompt="New task prompt."),
            subscription_id="sub-001",
            mcp_integration_ids=["GRAPH", "WEB"],
        )

        worker.reload_config(new_config)

        assert worker.config.name == "Updated Agent"
        assert worker.config.prompts.task_prompt == "New task prompt."
        assert worker.config.mcp_integration_ids == ["GRAPH", "WEB"]


class TestProcessingResult:
    """Tests for ProcessingResult dataclass."""

    def test_create_success_result(self):
        """Test creating a successful processing result."""
        result = ProcessingResult(
            agent_id="agent-001",
            event_id="evt-001",
            success=True,
            handled=True,
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
            handled=False,
            error="LLM call failed",
            summary="LLM call failed",
        )

        assert result.success is False
        assert result.error == "LLM call failed"

    def test_result_with_actions(self):
        """Test processing result with recorded actions."""
        result = ProcessingResult(
            agent_id="agent-001",
            event_id="evt-001",
            success=True,
            handled=True,
            summary="Updated node with web search results",
            actions=[
                {"tool": "GRAPH__search_graph", "input": {"query": "AI"}},
                {"tool": "GRAPH__update_node", "input": {"node_id": "n-1"}},
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

    def test_stop_interrupts_throttle_without_processing_queued_event(self, worker):
        throttled = threading.Event()

        def processing_delay():
            throttled.set()
            return 60.0

        worker._processing_delay = MagicMock(side_effect=processing_delay)
        worker._create_llm_client = MagicMock()
        worker._run_recorder = MagicMock()
        worker.on_result = MagicMock()
        worker.enqueue({"event_id": "throttled", "event_type": "node.create"})
        worker.start()
        try:
            assert throttled.wait(timeout=2.0)
            worker.stop(wait=True, timeout=1.0)
            assert not worker._thread.is_alive()
            worker._create_llm_client.assert_not_called()
            worker._run_recorder.record_start.assert_not_called()
            worker.on_result.assert_not_called()
            assert worker.events_processed == 0
        finally:
            worker.stop(wait=True, timeout=1.0)

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
        assert worker.on_result is not None


def _skill(skill_id, content="body", source="https://example.org/SKILL.md"):
    return SkillDefinition(
        id=skill_id,
        name=skill_id,
        description="",
        content=content,
        source_url=source,
    )


class FakeSkillsLoader:
    """Stands in for SkillsLoader: URL and local skills come from fixed lists."""

    def __init__(self, url_skills=(), local_skills=(), url_error=None, dir_error=None):
        self.url_skills = list(url_skills)
        self.local_skills = list(local_skills)
        self.url_error = url_error
        self.dir_error = dir_error
        self.requested_urls = None

    async def load_metadata_from_urls(self, urls):
        self.requested_urls = list(urls)
        return [("url", s) for s in self.url_skills]

    async def load_metadata_from_dir(self):
        if self.dir_error:
            raise self.dir_error
        return [("local", s) for s in self.local_skills]

    async def load_full_skills(self, metas):
        if self.url_error and any(kind == "url" for kind, _ in metas):
            raise self.url_error
        return [s for _, s in metas]


class TestLoadSkills:
    """Skills from URLs, the local dir and Skill nodes, deduplicated by id."""

    @pytest.fixture
    def make_worker(self, agent_config, agent_settings, mock_storage):
        def make(loader, skills_urls=(), skill_node_ids=()):
            config = replace(
                agent_config,
                skills_urls=list(skills_urls),
                skill_node_ids=list(skill_node_ids),
            )
            worker = AgentWorker(
                config=config,
                settings=agent_settings,
                mcp_loader=None,
                graph_service=MagicMock(),
                graph_storage=mock_storage,
            )
            worker._skills_loader = loader
            return worker

        return make

    @staticmethod
    def _skill_node(storage, node_id, content="graph body", node_type="Skill"):
        storage.nodes[node_id] = MockNode(
            id=node_id,
            name=f"{node_id} name",
            type=node_type,
            description="graph skill",
            metadata={"content": content, "allowed_tools": ["search_graph"]},
        )

    def test_loads_url_local_and_graph_skills_in_order(self, make_worker, mock_storage):
        self._skill_node(mock_storage, "skill-node")
        loader = FakeSkillsLoader(
            url_skills=[_skill("from-url")], local_skills=[_skill("from-dir")]
        )
        worker = make_worker(
            loader,
            skills_urls=["https://example.org/SKILL.md"],
            skill_node_ids=["skill-node"],
        )

        skills = worker._load_skills()

        assert [s.id for s in skills] == ["from-url", "from-dir", "skill-node"]
        assert loader.requested_urls == ["https://example.org/SKILL.md"]
        graph_skill = skills[2]
        assert graph_skill.content == "graph body"
        assert graph_skill.allowed_tools == ["search_graph"]
        assert graph_skill.source_url == "graph://skill/skill-node"

    def test_no_skills_urls_skips_url_loading(self, make_worker):
        loader = FakeSkillsLoader(url_skills=[_skill("from-url")])
        worker = make_worker(loader)

        assert worker._load_skills() == []
        assert loader.requested_urls is None

    def test_duplicate_ids_keep_first_source(self, make_worker, mock_storage):
        self._skill_node(mock_storage, "shared", content="graph version")
        loader = FakeSkillsLoader(
            url_skills=[_skill("shared", content="url version"), _skill("shared")],
            local_skills=[_skill("shared", content="dir version"), _skill("only-dir")],
        )
        worker = make_worker(
            loader,
            skills_urls=["https://example.org/SKILL.md"],
            skill_node_ids=["shared", "shared"],
        )

        skills = worker._load_skills()

        assert [s.id for s in skills] == ["shared", "only-dir"]
        assert skills[0].content == "url version"

    def test_failed_url_load_injects_nothing_and_keeps_other_sources(
        self, make_worker, mock_storage
    ):
        self._skill_node(mock_storage, "skill-node")
        loader = FakeSkillsLoader(
            url_skills=[_skill("from-url")],
            local_skills=[_skill("from-dir")],
            url_error=RuntimeError("fetch failed"),
        )
        worker = make_worker(
            loader,
            skills_urls=["https://example.org/SKILL.md"],
            skill_node_ids=["skill-node"],
        )

        skills = worker._load_skills()

        assert [s.id for s in skills] == ["from-dir", "skill-node"]

    def test_failed_local_dir_load_keeps_url_and_graph_skills(
        self, make_worker, mock_storage
    ):
        self._skill_node(mock_storage, "skill-node")
        loader = FakeSkillsLoader(
            url_skills=[_skill("from-url")], dir_error=OSError("unreadable")
        )
        worker = make_worker(
            loader,
            skills_urls=["https://example.org/SKILL.md"],
            skill_node_ids=["skill-node"],
        )

        assert [s.id for s in worker._load_skills()] == ["from-url", "skill-node"]

    def test_failing_skill_node_does_not_stop_later_nodes(
        self, make_worker, mock_storage, monkeypatch
    ):
        self._skill_node(mock_storage, "broken")
        self._skill_node(mock_storage, "good")
        real_get_node = mock_storage.get_node

        def get_node(node_id):
            if node_id == "broken":
                raise RuntimeError("storage error")
            return real_get_node(node_id)

        monkeypatch.setattr(mock_storage, "get_node", get_node)
        worker = make_worker(FakeSkillsLoader(), skill_node_ids=["broken", "good"])

        assert [s.id for s in worker._load_skills()] == ["good"]

    def test_unusable_skill_nodes_are_skipped(self, make_worker, mock_storage):
        self._skill_node(mock_storage, "not-a-skill", node_type="Initiative")
        self._skill_node(mock_storage, "empty", content="")
        self._skill_node(mock_storage, "good")
        worker = make_worker(
            FakeSkillsLoader(),
            skill_node_ids=["missing", "not-a-skill", "empty", "good"],
        )

        assert [s.id for s in worker._load_skills()] == ["good"]

    def test_skill_node_content_is_truncated_to_body_limit(
        self, make_worker, mock_storage
    ):
        self._skill_node(mock_storage, "long", content="x" * 50)
        worker = make_worker(FakeSkillsLoader(), skill_node_ids=["long"])
        worker._skills_config.max_skill_body_chars = 10

        assert worker._load_skills()[0].content == "x" * 10
