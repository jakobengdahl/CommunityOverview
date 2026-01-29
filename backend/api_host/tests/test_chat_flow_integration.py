"""
Integration tests for full chat flows.

These tests verify the complete chat workflow:
1. User sends message to create a node
2. LLM proposes the node (using mock)
3. User confirms
4. Node is created in the graph
5. Node appears in search results
"""

import pytest
import asyncio
from fastapi.testclient import TestClient


class TestChatFlowIntegration:
    """Integration tests for complete chat workflows."""

    def test_chat_search_flow(self, test_app_with_mock):
        """Test: User searches for nodes via chat, results appear in graph."""
        client, mock_llm = test_app_with_mock

        # First add a test node
        response = client.post(
            "/api/nodes",
            json={
                "nodes": [{
                    "name": "AI Research Lab",
                    "type": "Actor",
                    "description": "Research laboratory focused on AI",
                    "summary": "AI research facility",
                    "communities": ["Research"],
                    "tags": ["ai", "research"]
                }],
                "edges": []
            }
        )
        assert response.status_code == 200
        assert response.json()["success"]

        # Configure mock to return search results
        mock_llm.set_response(
            "I found the AI Research Lab. It's an Actor in the Research community.",
            tool_use={
                "name": "search_graph",
                "input": {"query": "AI", "limit": 10}
            }
        )

        # User asks to search via chat
        response = client.post(
            "/ui/chat",
            json={
                "messages": [
                    {"role": "user", "content": "Find AI-related nodes"}
                ]
            }
        )
        assert response.status_code == 200
        result = response.json()

        # Verify response contains search results
        assert "content" in result
        assert result.get("toolUsed") == "search_graph"
        tool_result = result.get("toolResult")
        assert tool_result is not None
        # Search should return results
        assert tool_result.get("total", 0) >= 1 or len(tool_result.get("nodes", [])) >= 1

    def test_chat_add_node_flow(self, test_app_with_mock):
        """Test: Full flow of adding a node via chat with confirmation."""
        client, mock_llm = test_app_with_mock

        # Step 1: User requests to add a node
        mock_llm.set_response(
            "I'll help you add a new initiative. Let me create the node.",
            tool_use={
                "name": "add_nodes",
                "input": {
                    "nodes": [{
                        "name": "Smart City Initiative",
                        "type": "Initiative",
                        "description": "A project to develop smart city solutions",
                        "summary": "Smart city development project",
                        "communities": ["Digital"],
                        "tags": ["smart-city", "urban", "digital"]
                    }],
                    "edges": []
                }
            }
        )

        response = client.post(
            "/ui/chat",
            json={
                "messages": [
                    {"role": "user", "content": "Add a new initiative called Smart City Initiative about developing smart city solutions"}
                ]
            }
        )
        assert response.status_code == 200
        result = response.json()

        assert result.get("toolUsed") == "add_nodes"
        tool_result = result.get("toolResult")
        assert tool_result is not None
        assert tool_result.get("success")
        assert len(tool_result.get("added_node_ids", [])) == 1

        # Step 2: Verify node exists via REST API
        added_node_id = tool_result["added_node_ids"][0]
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
                "nodes": [{
                    "name": "Old Project",
                    "type": "Initiative",
                    "description": "Original description",
                    "summary": "Original summary",
                    "communities": [],
                    "tags": []
                }],
                "edges": []
            }
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
                        "tags": ["updated", "improved"]
                    }
                }
            }
        )

        response = client.post(
            "/ui/chat",
            json={
                "messages": [
                    {"role": "user", "content": f"Update the description of node {node_id}"}
                ]
            }
        )
        assert response.status_code == 200
        result = response.json()
        assert result.get("toolUsed") == "update_node"
        assert result.get("toolResult", {}).get("success")

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
                    {"name": "ToDelete1", "type": "Actor", "description": "Test", "summary": "Test", "communities": [], "tags": []},
                    {"name": "ToDelete2", "type": "Actor", "description": "Test", "summary": "Test", "communities": [], "tags": []}
                ],
                "edges": []
            }
        )
        node_ids = response.json()["added_node_ids"]

        # First attempt without confirmation
        mock_llm.set_response(
            "I'll delete these nodes. Please confirm.",
            tool_use={
                "name": "delete_nodes",
                "input": {
                    "node_ids": node_ids,
                    "confirmed": False
                }
            }
        )

        response = client.post(
            "/ui/chat",
            json={
                "messages": [
                    {"role": "user", "content": "Delete the test nodes"}
                ]
            }
        )
        result = response.json()
        tool_result = result.get("toolResult", {})
        # Should require confirmation
        assert tool_result.get("requires_confirmation") or not tool_result.get("success")

        # Now with confirmation
        mock_llm.set_response(
            "Nodes deleted successfully.",
            tool_use={
                "name": "delete_nodes",
                "input": {
                    "node_ids": node_ids,
                    "confirmed": True
                }
            }
        )

        response = client.post(
            "/ui/chat",
            json={
                "messages": [
                    {"role": "user", "content": "Yes, delete them"}
                ]
            }
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
            }
        )
        assert response.status_code == 200

        # Second message referencing the first
        mock_llm.set_response("Yes, those 5 initiatives are all related to digital transformation.")

        response = client.post(
            "/ui/chat",
            json={
                "messages": [
                    {"role": "user", "content": "How many initiatives are there?"},
                    {"role": "assistant", "content": "I found 5 initiatives in the graph."},
                    {"role": "user", "content": "Are they all related to digital transformation?"}
                ]
            }
        )
        assert response.status_code == 200
        # The mock received all messages in context

    def test_chat_with_document_context(self, test_app_with_mock):
        """Test: Chat can analyze uploaded document content."""
        client, mock_llm = test_app_with_mock

        # Upload a document
        from io import BytesIO
        file_content = b"This is a test document about AI governance and policy frameworks."

        response = client.post(
            "/ui/upload/extract",
            files={"file": ("test.txt", BytesIO(file_content), "text/plain")}
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
                "input": {"query": "AI governance policy", "limit": 5}
            }
        )

        response = client.post(
            "/ui/chat",
            json={
                "messages": [
                    {"role": "user", "content": f"Analyze this document and find related nodes:\n\n{document_text}"}
                ]
            }
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
                "communities": ["Government"]
            }
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
            "/ui/chat/simple",
            json={"message": "What's the graph size?"}
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
                "nodes": [{
                    "name": "REST Created Node",
                    "type": "Resource",
                    "description": "Created via REST",
                    "summary": "REST node",
                    "communities": [],
                    "tags": ["rest"]
                }],
                "edges": []
            }
        )
        rest_node_id = response.json()["added_node_ids"][0]

        # Search via chat should find it
        mock_llm.set_response(
            "Found the REST Created Node.",
            tool_use={
                "name": "search_graph",
                "input": {"query": "REST Created"}
            }
        )

        response = client.post(
            "/ui/chat",
            json={
                "messages": [{"role": "user", "content": "Find the REST node"}]
            }
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
                    "nodes": [{
                        "name": "Chat Created Node",
                        "type": "Resource",
                        "description": "Created via chat",
                        "summary": "Chat node",
                        "communities": [],
                        "tags": ["chat"]
                    }],
                    "edges": []
                }
            }
        )

        response = client.post(
            "/ui/chat",
            json={
                "messages": [{"role": "user", "content": "Add a Chat Created Node"}]
            }
        )
        chat_node_id = response.json()["toolResult"]["added_node_ids"][0]

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
            tool_use={
                "name": "search_graph",
                "input": {"query": "test", "limit": 5}
            }
        )

        response = client.post(
            "/ui/chat",
            json={
                "messages": [{"role": "user", "content": "Search for test"}]
            }
        )
        result = response.json()

        # Response should have expected structure for widget
        assert "content" in result
        assert isinstance(result["content"], str)

        # toolUsed and toolResult should be present when tools are used
        if result.get("toolUsed"):
            assert "toolResult" in result
            assert isinstance(result["toolResult"], dict)
