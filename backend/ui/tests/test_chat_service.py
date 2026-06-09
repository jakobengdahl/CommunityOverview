"""
Tests for ChatService.

Verifies that:
- ChatService correctly routes tool calls to GraphService
- LLM responses are properly formatted
- Graph mutations go through GraphService (not direct storage access)
"""

import os
import pytest
from unittest.mock import patch, MagicMock
import json


class TestChatServiceInit:
    """Tests for ChatService initialization."""

    def test_creates_tools_map(self, graph_service):
        """ChatService should create a tools map with all expected tools."""
        from backend.ui import ChatService

        with patch('backend.chat_logic.create_provider'):
            service = ChatService(graph_service)

        expected_tools = [
            "search_graph",
            "get_node_details",
            "get_related_nodes",
            "find_similar_nodes",
            "find_similar_nodes_batch",
            "add_nodes",
            "update_node",
            "delete_nodes",
            "list_node_types",
            "get_graph_stats",
            "save_view",
            "get_saved_view",
            "list_saved_views",
        ]

        for tool in expected_tools:
            assert tool in service._tools_map, f"Missing tool: {tool}"

    def test_tools_map_routes_to_graph_service(self, graph_service):
        """All tools in the map should route to GraphService methods."""
        from backend.ui import ChatService

        with patch('backend.chat_logic.create_provider'):
            service = ChatService(graph_service)

        # Verify tools are bound to graph_service methods
        assert service._tools_map["search_graph"] == graph_service.search_graph
        assert service._tools_map["add_nodes"] == graph_service.add_nodes
        assert service._tools_map["update_node"] == graph_service.update_node


class TestChatServiceToolExecution:
    """Tests for ChatService tool execution via GraphService."""

    def test_search_graph_tool_uses_graph_service(self, chat_service, sample_nodes):
        """search_graph tool should use GraphService.search_graph."""
        service, mock_llm = chat_service

        # Configure mock to call search_graph
        mock_llm.mock_tool_calls = [
            {"name": "search_graph", "input": {"query": "Test", "limit": 10}}
        ]
        mock_llm.mock_text_response = "Found 2 nodes matching 'Test'."

        result = service.process_message([{"role": "user", "content": "Search for Test"}])

        # Verify response
        assert "Found" in result["content"] or "nodes" in str(result.get("toolResult", {}))
        assert result["toolUsed"] == "search_graph"

    def test_add_nodes_tool_uses_graph_service(self, chat_service):
        """add_nodes tool should use GraphService.add_nodes."""
        service, mock_llm = chat_service

        # Configure mock to call add_nodes
        mock_llm.mock_tool_calls = [
            {
                "name": "add_nodes",
                "input": {
                    "nodes": [
                        {"id": "new-node-1", "name": "New Node", "type": "Actor", "description": "Test"}
                    ],
                    "edges": []
                }
            }
        ]
        mock_llm.mock_text_response = "Added 1 new node."

        result = service.process_message([{"role": "user", "content": "Add a new actor"}])

        # Verify node was added via GraphService
        graph_result = service.graph_service.search_graph(query="New Node")
        assert graph_result["total"] >= 1

    def test_update_node_tool_uses_graph_service(self, chat_service, sample_nodes):
        """update_node tool should use GraphService.update_node."""
        service, mock_llm = chat_service

        # Configure mock to call update_node
        mock_llm.mock_tool_calls = [
            {
                "name": "update_node",
                "input": {
                    "node_id": "test-actor-1",
                    "updates": {"description": "Updated description"}
                }
            }
        ]
        mock_llm.mock_text_response = "Node updated successfully."

        result = service.process_message([{"role": "user", "content": "Update the test agency"}])

        # Verify node was updated
        node_result = service.graph_service.get_node_details("test-actor-1")
        assert node_result["success"]
        assert node_result["node"]["description"] == "Updated description"

    def test_delete_nodes_tool_uses_graph_service(self, chat_service, sample_nodes):
        """delete_nodes tool should use GraphService.delete_nodes."""
        service, mock_llm = chat_service

        # First verify node exists
        before = service.graph_service.get_node_details("test-actor-1")
        assert before["success"]

        # Configure mock to call delete_nodes
        mock_llm.mock_tool_calls = [
            {
                "name": "delete_nodes",
                "input": {
                    "node_ids": ["test-actor-1"],
                    "confirmed": True
                }
            }
        ]
        mock_llm.mock_text_response = "Node deleted."

        result = service.process_message([{"role": "user", "content": "Delete test-actor-1"}])

        # Verify node was deleted
        after = service.graph_service.get_node_details("test-actor-1")
        assert not after["success"]

    def test_get_related_nodes_tool(self, chat_service, sample_nodes):
        """get_related_nodes tool should use GraphService."""
        service, mock_llm = chat_service

        mock_llm.mock_tool_calls = [
            {
                "name": "get_related_nodes",
                "input": {"node_id": "test-actor-1", "depth": 1}
            }
        ]
        mock_llm.mock_text_response = "Found related nodes."

        result = service.process_message([{"role": "user", "content": "Show related nodes"}])

        assert result["toolUsed"] == "get_related_nodes"
        assert "toolResult" in result


class TestChatServiceConversation:
    """Tests for conversation handling."""

    def test_process_chat_request_builds_messages(self, chat_service):
        """process_chat_request should build proper message list."""
        service, mock_llm = chat_service
        mock_llm.mock_tool_calls = []
        mock_llm.mock_text_response = "Hello! How can I help?"

        result = service.process_chat_request(
            user_message="Hello",
            conversation_history=[]
        )

        # Verify message was sent to LLM
        assert len(mock_llm.received_messages) > 0
        last_messages = mock_llm.received_messages[-1]
        assert any(msg.get("content") == "Hello" for msg in last_messages)

    def test_process_chat_request_includes_document_context(self, chat_service):
        """process_chat_request should include document context."""
        service, mock_llm = chat_service
        mock_llm.mock_tool_calls = []
        mock_llm.mock_text_response = "The document discusses AI."

        result = service.process_chat_request(
            user_message="What is this about?",
            document_context="This is a document about AI and machine learning."
        )

        # Verify document context was included
        last_messages = mock_llm.received_messages[-1]
        user_msg = next(msg for msg in last_messages if msg.get("role") == "user")
        assert "Document content" in user_msg["content"]
        assert "AI and machine learning" in user_msg["content"]

    def test_get_system_info(self, chat_service):
        """get_system_info should return provider and tools info."""
        service, _ = chat_service

        info = service.get_system_info()

        assert "provider" in info
        assert "available_tools" in info
        assert "graph_stats" in info
        assert len(info["available_tools"]) > 0


class TestGraphServiceIntegration:
    """Tests verifying GraphService is used correctly."""

    def test_multiple_tool_calls_use_graph_service(self, chat_service):
        """Multiple tool calls should all go through GraphService."""
        service, mock_llm = chat_service

        # First call: add a node
        mock_llm.mock_tool_calls = [
            {
                "name": "add_nodes",
                "input": {
                    "nodes": [{"id": "int-test-1", "name": "Integration Test", "type": "Initiative", "description": "Test"}],
                    "edges": []
                }
            }
        ]
        mock_llm.mock_text_response = "Added node."
        service.process_message([{"role": "user", "content": "Add a node"}])

        # Reset mock
        mock_llm.reset()

        # Second call: search for it
        mock_llm.mock_tool_calls = [
            {"name": "search_graph", "input": {"query": "Integration Test"}}
        ]
        mock_llm.mock_text_response = "Found the node."
        result = service.process_message([{"role": "user", "content": "Find Integration Test"}])

        # Verify the node was found (proving GraphService persisted it)
        tool_result = result.get("toolResult", {})
        assert tool_result.get("total", 0) >= 1 or "nodes" in tool_result


# ---------------------------------------------------------------------------
# Expert agent skills injection
# ---------------------------------------------------------------------------

class TestExpertAgentSkills:
    """Tests for expert agent skills loading and context injection."""

    def _make_expert(self, expert_id="legislation-expert", system_context="You are a legislation expert.", skills_urls=None):
        """Return a minimal ExpertAgentConfig-like object (plain SimpleNamespace)."""
        from types import SimpleNamespace
        return SimpleNamespace(
            id=expert_id,
            system_context=system_context,
            skills_urls=skills_urls or [],
        )

    @pytest.mark.asyncio
    async def test_load_expert_skills_registers_context(self, graph_service):
        """load_expert_skills() stores system_context per expert ID."""
        from backend.ui import ChatService
        from backend.skills.loader import SkillsConfig

        with patch('backend.chat_logic.create_provider'), \
             patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key'}):
            service = ChatService(graph_service)

        expert = self._make_expert(system_context="You are a legislation expert.")
        await service.load_expert_skills([expert], SkillsConfig())

        assert service._expert_contexts["legislation-expert"] == "You are a legislation expert."

    @pytest.mark.asyncio
    async def test_load_expert_skills_no_urls_stores_empty_list(self, graph_service):
        """An expert with no skills_urls gets an empty skills list (not an error)."""
        from backend.ui import ChatService
        from backend.skills.loader import SkillsConfig

        with patch('backend.chat_logic.create_provider'), \
             patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key'}):
            service = ChatService(graph_service)

        expert = self._make_expert(skills_urls=[])
        await service.load_expert_skills([expert], SkillsConfig())

        assert service._expert_skills["legislation-expert"] == []

    def test_build_expert_context_unknown_id_returns_none(self, graph_service):
        """Unknown expert_agent_id must not raise — returns None."""
        from backend.ui import ChatService

        with patch('backend.chat_logic.create_provider'), \
             patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key'}):
            service = ChatService(graph_service)

        assert service._build_expert_context("unknown-id") is None

    def test_build_expert_context_with_context_only(self, graph_service):
        """An expert with system_context but no skills returns just the context."""
        from backend.ui import ChatService
        from backend.skills.loader import SkillsConfig

        with patch('backend.chat_logic.create_provider'), \
             patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key'}):
            service = ChatService(graph_service)

        service._expert_contexts["leg"] = "You are a legislation expert."
        service._expert_skills["leg"] = []

        ctx = service._build_expert_context("leg")
        assert ctx == "You are a legislation expert."

    def test_build_expert_context_with_skills(self, graph_service):
        """An expert with both context and skills returns a combined string."""
        from backend.ui import ChatService
        from backend.skills.loader import SkillDefinition

        with patch('backend.chat_logic.create_provider'), \
             patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key'}):
            service = ChatService(graph_service)

        skill = SkillDefinition(
            id="s1", name="GDPR Skill", description="",
            content="GDPR guidance.", source_url="http://x.com/SKILL.md"
        )
        service._expert_contexts["leg"] = "You are a legislation expert."
        # Full skills go in _expert_skills_full (populated by load_expert_skills at startup)
        service._expert_skills_full["leg"] = [skill]

        ctx = service._build_expert_context("leg")
        assert "You are a legislation expert." in ctx
        assert "GDPR Skill" in ctx
        assert "GDPR guidance." in ctx

    def test_process_message_passes_extra_context_to_processor(self, graph_service, mock_llm_provider):
        """process_message() with expert_agent_id should pass extra_context to ChatProcessor."""
        from backend.ui import ChatService

        with patch('backend.chat_logic.create_provider', return_value=mock_llm_provider), \
             patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key'}):
            service = ChatService(graph_service)
            service._processor.provider_type = "mock"
            service._processor.default_api_key = "test-key"

        service._expert_contexts["leg"] = "You are a legislation expert."
        service._expert_skills["leg"] = []

        # Capture the system_prompt actually passed to create_completion
        received_prompts = []
        original = mock_llm_provider.create_completion
        def capture(*args, **kwargs):
            received_prompts.append(kwargs.get("system_prompt", ""))
            return original(*args, **kwargs)
        mock_llm_provider.create_completion = capture

        service.process_message(
            messages=[{"role": "user", "content": "hello"}],
            expert_agent_id="leg",
        )

        assert received_prompts, "create_completion was never called"
        assert "You are a legislation expert." in received_prompts[0]
