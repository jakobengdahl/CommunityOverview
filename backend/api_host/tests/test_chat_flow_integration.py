# covers: backend/ui/chat_logic.py, backend/ui/chat_service.py, backend/ui/rest_api.py
"""
Integration tests for full chat flows.

These tests verify the complete chat workflow:
1. User sends message to create a node
2. LLM proposes the node (using mock)
3. User confirms
4. Node is created in the graph
5. Node appears in search results
"""

import asyncio
import threading
from unittest.mock import MagicMock

import httpx
import pytest

from backend.llm.llm_providers import LLMResponse
from backend.runtime.authorization import get_current_request_authorization_inputs
from backend.runtime.request_context import ACTOR_ID_HEADER
from backend.ui.chat_logic import run_chat_off_event_loop, run_on_event_loop

_WAIT_S = 5
_MESSAGES = [{"role": "user", "content": "x"}]


class _GatedLLM:
    """Holds each chat's first call until released, then asks for one search and
    answers once it has the result."""

    def __init__(self, app):
        self.release = threading.Event()
        self.threads = []
        self.released_in_time = []
        processor = app.state.chat_service._processor
        processor._resolve_llm_provider = lambda *_: (self, None)

    def create_completion(self, messages, **_):
        self.threads.append(threading.get_ident())
        if isinstance(messages[-1]["content"], list):
            text = {"type": "text", "text": "done"}
            return LLMResponse(content=[text], stop_reason="end_turn")
        self.released_in_time.append(self.release.wait(_WAIT_S))
        call = {"type": "tool_use", "id": "t1", "name": "search_graph"}
        call["input"] = {"query": "x"}
        return LLMResponse(content=[call], stop_reason="tool_use")


def _http(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _record_searches(app):
    seen = []

    def search(**kwargs):
        headers = get_current_request_authorization_inputs()["headers"]
        seen.append((headers[ACTOR_ID_HEADER], kwargs["federation_depth"]))
        return {"nodes": []}

    app.state.chat_service.graph_service.search_graph = search
    return seen


async def _until(condition):
    async with asyncio.timeout(_WAIT_S * 2):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_health_and_writes_answered_while_concurrent_chats_stay_isolated(
    test_app_with_mock,
):
    app = test_app_with_mock[0].app
    llm = _GatedLLM(app)
    seen = _record_searches(app)
    node = {"name": "Written mid-chat", "type": "Actor", "description": "d"}
    chats = [
        ("/ui/chat", {"messages": _MESSAGES, "federation_depth": 1}, "a"),
        ("/ui/chat/simple", {"message": "x", "federation_depth": 2}, "b"),
    ]
    async with _http(app) as http:
        tasks = [
            asyncio.create_task(http.post(p, json=b, headers={ACTOR_ID_HEADER: a}))
            for p, b, a in chats
        ]
        await _until(lambda: len(llm.threads) == 2)
        health = await http.get("/health")
        write = await http.post("/api/nodes", json={"nodes": [node], "edges": []})
        llm.release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), _WAIT_S * 2)

    assert health.status_code == 200 and write.status_code == 200
    assert llm.released_in_time == [True, True]
    assert sorted(seen) == [("a", 1), ("b", 2)]


@pytest.mark.asyncio
async def test_all_graph_access_runs_on_the_loop_thread_and_llm_calls_off_it(
    test_app_with_mock,
):
    app = test_app_with_mock[0].app
    llm = _GatedLLM(app)
    llm.release.set()
    service = app.state.chat_service
    akc = {
        "short_name": "kiosk",
        "tool_allowlist": ["read_node_attachment", "search_graph"],
    }
    service.graph_service.add_nodes(
        nodes=[{"name": "K", "type": "ActiveKnowledgeCollection", "metadata": akc}],
        edges=[],
    )
    # An advertised attachment tool makes the chat re-read the collection's grant.
    service._processor.tool_definitions.append({"name": "read_node_attachment"})
    hook = service.graph_service._authorization_hook
    evaluate, hook_threads = hook.evaluate, []
    hook.evaluate = lambda c: hook_threads.append(threading.get_ident()) or evaluate(c)
    body = {"messages": _MESSAGES, "collection_short_name": "kiosk"}

    async with _http(app) as http:
        assert (await http.post("/ui/chat", json=body)).status_code == 200

    loop_thread = threading.get_ident()
    assert len(hook_threads) == 3 and set(hook_threads) == {loop_thread}
    assert llm.threads and loop_thread not in llm.threads


@pytest.mark.asyncio
async def test_hop_copies_its_result_and_other_callers_run_in_place():
    live = {"nodes": []}

    hopped = await run_chat_off_event_loop(lambda: run_on_event_loop(lambda: live))

    assert hopped == live and hopped["nodes"] is not live["nodes"]
    assert run_on_event_loop(lambda: live) is live


@pytest.mark.asyncio
async def test_cancelled_or_failing_chats_keep_their_outcomes(test_app_with_mock):
    app = test_app_with_mock[0].app
    llm = _GatedLLM(app)
    seen = _record_searches(app)
    service, failure = app.state.chat_service, RuntimeError("failed")

    def chat(http):
        body, headers = {"messages": _MESSAGES}, {ACTOR_ID_HEADER: "a"}
        return http.post("/ui/chat", json=body, headers=headers)

    async with _http(app) as http:
        cancelled = asyncio.create_task(chat(http))
        await _until(lambda: llm.threads)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        llm.release.set()
        await _until(lambda: len(llm.threads) == 2)
        service._processor.tools_map["search_graph"] = MagicMock(side_effect=failure)
        tool_error = await chat(http)
        service.process_message = MagicMock(side_effect=failure)
        chat_error = await chat(http)

    assert seen == [("a", None)]
    assert tool_error.json()["toolResult"] == {"error": str(failure)}
    assert chat_error.status_code == 500


class TestChatFlowIntegration:
    """Integration tests for complete chat workflows."""

    def test_chat_search_flow(self, test_app_with_mock):
        """Test: User searches for nodes via chat, results appear in graph."""
        client, mock_llm = test_app_with_mock

        # First add a test node
        response = client.post(
            "/api/nodes",
            json={
                "nodes": [
                    {
                        "name": "AI Research Lab",
                        "type": "Actor",
                        "description": "Research laboratory focused on AI",
                        "summary": "AI research facility",
                        "communities": ["Research"],
                        "tags": ["ai", "research"],
                    }
                ],
                "edges": [],
            },
        )
        assert response.status_code == 200
        assert response.json()["success"]

        # Configure mock to return search results
        mock_llm.set_response(
            "I found the AI Research Lab. It's an Actor in the Research community.",
            tool_use={"name": "search_graph", "input": {"query": "AI", "limit": 10}},
        )

        # User asks to search via chat
        response = client.post(
            "/ui/chat",
            json={"messages": [{"role": "user", "content": "Find AI-related nodes"}]},
        )
        assert response.status_code == 200
        result = response.json()

        # Verify response contains search results
        assert "content" in result
        assert result.get("toolUsed") == "search_graph"
        tool_result = result.get("toolResult")
        assert tool_result is not None
        # Search should return results
        assert (
            tool_result.get("total", 0) >= 1 or len(tool_result.get("nodes", [])) >= 1
        )

    def test_chat_add_node_flow(self, test_app_with_mock):
        """Test: Full flow of adding a node via chat with confirmation."""
        client, mock_llm = test_app_with_mock

        # Step 1: User requests to add a node
        mock_llm.set_response(
            "I'll help you add a new initiative. Let me create the node.",
            tool_use={
                "name": "add_nodes",
                "input": {
                    "nodes": [
                        {
                            "name": "Smart City Initiative",
                            "type": "Initiative",
                            "description": "A project to develop smart city solutions",
                            "summary": "Smart city development project",
                            "communities": ["Digital"],
                            "tags": ["smart-city", "urban", "digital"],
                        }
                    ],
                    "edges": [],
                },
            },
        )

        response = client.post(
            "/ui/chat",
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": "Add a new initiative called Smart City Initiative about developing smart city solutions",
                    }
                ]
            },
        )
        assert response.status_code == 200
        result = response.json()

        assert result.get("toolUsed") == "add_nodes"
        tool_result = result.get("toolResult")
        assert tool_result is not None
        assert "nodes" in tool_result
        assert len(tool_result["nodes"]) == 1

        # Step 2: Verify node exists via REST API
        added_node_id = tool_result["nodes"][0]["id"]
        response = client.get(f"/api/nodes/{added_node_id}")
        assert response.status_code == 200
        node_data = response.json()
        assert node_data["success"]
        assert node_data["node"]["name"] == "Smart City Initiative"
        assert node_data["node"]["type"] == "Initiative"

    def test_chat_update_node_flow(self, test_app_with_mock):
        """Test: Update an existing node via chat."""
        client, mock_llm = test_app_with_mock

        # Create a node first
        response = client.post(
            "/api/nodes",
            json={
                "nodes": [
                    {
                        "name": "Old Project",
                        "type": "Initiative",
                        "description": "Original description",
                        "summary": "Original summary",
                        "communities": [],
                        "tags": [],
                    }
                ],
                "edges": [],
            },
        )
        node_id = response.json()["added_node_ids"][0]

        # Update via chat
        mock_llm.set_response(
            "I've updated the project description.",
            tool_use={
                "name": "update_node",
                "input": {
                    "node_id": node_id,
                    "updates": {
                        "description": "Updated description with more details",
                        "tags": ["updated", "improved"],
                    },
                },
            },
        )

        response = client.post(
            "/ui/chat",
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": f"Update the description of node {node_id}",
                    }
                ]
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result.get("toolUsed") == "update_node"
        assert "nodes" in result.get("toolResult", {})

        # Verify update via REST
        response = client.get(f"/api/nodes/{node_id}")
        node_data = response.json()["node"]
        assert "Updated description" in node_data["description"]
        assert "updated" in node_data["tags"]

    def test_chat_delete_flow_with_confirmation(self, test_app_with_mock):
        """Test: Delete nodes via chat requires confirmation."""
        client, mock_llm = test_app_with_mock

        # Create nodes to delete
        response = client.post(
            "/api/nodes",
            json={
                "nodes": [
                    {
                        "name": "ToDelete1",
                        "type": "Actor",
                        "description": "Test",
                        "summary": "Test",
                        "communities": [],
                        "tags": [],
                    },
                    {
                        "name": "ToDelete2",
                        "type": "Actor",
                        "description": "Test",
                        "summary": "Test",
                        "communities": [],
                        "tags": [],
                    },
                ],
                "edges": [],
            },
        )
        node_ids = response.json()["added_node_ids"]

        # First attempt without confirmation
        mock_llm.set_response(
            "I'll delete these nodes. Please confirm.",
            tool_use={
                "name": "delete_nodes",
                "input": {"node_ids": node_ids, "confirmed": False},
            },
        )

        response = client.post(
            "/ui/chat",
            json={"messages": [{"role": "user", "content": "Delete the test nodes"}]},
        )
        result = response.json()
        tool_result = result.get("toolResult", {})
        # Should require confirmation
        assert tool_result.get("requires_confirmation") or not tool_result.get(
            "success"
        )

        # Now with confirmation
        mock_llm.set_response(
            "Nodes deleted successfully.",
            tool_use={
                "name": "delete_nodes",
                "input": {"node_ids": node_ids, "confirmed": True},
            },
        )

        response = client.post(
            "/ui/chat",
            json={"messages": [{"role": "user", "content": "Yes, delete them"}]},
        )
        result = response.json()
        assert result.get("toolUsed") == "delete_nodes"
        tool_result = result.get("toolResult", {})
        assert tool_result.get("success")

        # Verify nodes are deleted
        for node_id in node_ids:
            response = client.get(f"/api/nodes/{node_id}")
            # Should return 404 or success=False
            if response.status_code == 200:
                assert not response.json().get("success")
            else:
                assert response.status_code == 404

    def test_chat_conversation_context(self, test_app_with_mock):
        """Test: Chat maintains conversation context across messages."""
        client, mock_llm = test_app_with_mock

        # First message
        mock_llm.set_response("I found 5 initiatives in the graph.")

        response = client.post(
            "/ui/chat",
            json={
                "messages": [
                    {"role": "user", "content": "How many initiatives are there?"}
                ]
            },
        )
        assert response.status_code == 200

        # Second message referencing the first
        mock_llm.set_response(
            "Yes, those 5 initiatives are all related to digital transformation."
        )

        response = client.post(
            "/ui/chat",
            json={
                "messages": [
                    {"role": "user", "content": "How many initiatives are there?"},
                    {
                        "role": "assistant",
                        "content": "I found 5 initiatives in the graph.",
                    },
                    {
                        "role": "user",
                        "content": "Are they all related to digital transformation?",
                    },
                ]
            },
        )
        assert response.status_code == 200
        # The mock received all messages in context

    def test_chat_with_document_context(self, test_app_with_mock):
        """Test: Chat can analyze uploaded document content."""
        client, mock_llm = test_app_with_mock

        # Upload a document
        from io import BytesIO

        file_content = (
            b"This is a test document about AI governance and policy frameworks."
        )

        response = client.post(
            "/ui/upload/extract",
            files={"file": ("test.txt", BytesIO(file_content), "text/plain")},
        )
        assert response.status_code == 200
        extracted = response.json()
        assert extracted["success"]
        document_text = extracted["text"]

        # Now chat with document context
        mock_llm.set_response(
            "The document discusses AI governance and policy frameworks. I can help extract relevant nodes.",
            tool_use={
                "name": "search_graph",
                "input": {"query": "AI governance policy", "limit": 5},
            },
        )

        response = client.post(
            "/ui/chat",
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": f"Analyze this document and find related nodes:\n\n{document_text}",
                    }
                ]
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert "AI governance" in result["content"] or result.get("toolUsed")

    def test_propose_nodes_from_text_endpoint(self, test_app_with_mock):
        """Test: propose-nodes endpoint extracts entities from text."""
        client, mock_llm = test_app_with_mock

        # Configure mock to return extracted entities
        mock_llm.set_response(
            '[{"type": "Actor", "name": "Ministry of Innovation", "description": "Government ministry", "summary": "Innovation ministry", "tags": ["government"]}, {"type": "Initiative", "name": "Digital Strategy 2030", "description": "National digital strategy", "summary": "Digital strategy", "tags": ["digital", "strategy"]}]'
        )

        response = client.post(
            "/ui/propose-nodes",
            json={
                "text": "The Ministry of Innovation has launched Digital Strategy 2030, a comprehensive plan for national digitalization.",
                "communities": ["Government"],
            },
        )
        assert response.status_code == 200
        result = response.json()

        # Should return proposed nodes
        assert result.get("success") or "proposed_nodes" in result
        if result.get("proposed_nodes"):
            assert len(result["proposed_nodes"]) >= 1
            assert result.get("requires_confirmation")

    def test_chat_simple_endpoint(self, test_app_with_mock):
        """Test: Simple chat endpoint for quick queries."""
        client, mock_llm = test_app_with_mock

        mock_llm.set_response("The graph currently contains 10 nodes and 5 edges.")

        response = client.post(
            "/ui/chat/simple", json={"message": "What's the graph size?"}
        )
        assert response.status_code == 200
        result = response.json()
        assert "content" in result

    def test_mcp_and_rest_share_graph_state(self, test_app_with_mock):
        """Test: Changes via chat affect REST API and vice versa."""
        client, mock_llm = test_app_with_mock

        # Add node via REST
        response = client.post(
            "/api/nodes",
            json={
                "nodes": [
                    {
                        "name": "REST Created Node",
                        "type": "Resource",
                        "description": "Created via REST",
                        "summary": "REST node",
                        "communities": [],
                        "tags": ["rest"],
                    }
                ],
                "edges": [],
            },
        )
        assert response.json()["added_node_ids"]

        # Search via chat should find it
        mock_llm.set_response(
            "Found the REST Created Node.",
            tool_use={"name": "search_graph", "input": {"query": "REST Created"}},
        )

        response = client.post(
            "/ui/chat",
            json={"messages": [{"role": "user", "content": "Find the REST node"}]},
        )
        result = response.json()
        tool_result = result.get("toolResult", {})
        nodes = tool_result.get("nodes", [])
        assert any(n.get("name") == "REST Created Node" for n in nodes)

        # Add node via chat
        mock_llm.set_response(
            "Created the Chat Node.",
            tool_use={
                "name": "add_nodes",
                "input": {
                    "nodes": [
                        {
                            "name": "Chat Created Node",
                            "type": "Resource",
                            "description": "Created via chat",
                            "summary": "Chat node",
                            "communities": [],
                            "tags": ["chat"],
                        }
                    ],
                    "edges": [],
                },
            },
        )

        response = client.post(
            "/ui/chat",
            json={"messages": [{"role": "user", "content": "Add a Chat Created Node"}]},
        )
        assert response.json()["toolResult"]["nodes"][0]["id"]

        # Should be findable via REST
        response = client.post("/api/search", json={"query": "Chat Created"})
        assert response.status_code == 200
        nodes = response.json()["nodes"]
        assert any(n.get("name") == "Chat Created Node" for n in nodes)

    def test_widget_compatible_response_format(self, test_app_with_mock):
        """Test: Chat responses are compatible with ChatGPT widget format."""
        client, mock_llm = test_app_with_mock

        mock_llm.set_response(
            "Here are the search results.",
            tool_use={"name": "search_graph", "input": {"query": "test", "limit": 5}},
        )

        response = client.post(
            "/ui/chat",
            json={"messages": [{"role": "user", "content": "Search for test"}]},
        )
        result = response.json()

        # Response should have expected structure for widget
        assert "content" in result
        assert isinstance(result["content"], str)

        # toolUsed and toolResult should be present when tools are used
        if result.get("toolUsed"):
            assert "toolResult" in result
            assert isinstance(result["toolResult"], dict)
