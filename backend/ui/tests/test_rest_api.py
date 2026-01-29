"""
Tests for UI Backend REST API.

Verifies that:
- /ui/chat endpoint works correctly
- /ui/upload endpoint handles files properly
- Graph mutations go through GraphService
"""

import pytest
from unittest.mock import patch
import json
import io


class TestChatEndpoint:
    """Tests for /ui/chat endpoint."""

    def test_chat_endpoint_returns_response(self, fastapi_test_client):
        """POST /ui/chat should return LLM response."""
        client, mock_llm, _ = fastapi_test_client

        mock_llm.mock_tool_calls = []
        mock_llm.mock_text_response = "Hello! How can I help you with the graph?"

        response = client.post("/ui/chat", json={
            "messages": [{"role": "user", "content": "Hello"}]
        })

        assert response.status_code == 200
        data = response.json()
        assert "content" in data
        assert "Hello" in data["content"] or "help" in data["content"].lower()

    def test_chat_endpoint_with_tool_call(self, fastapi_test_client):
        """POST /ui/chat should handle tool calls."""
        client, mock_llm, graph_service = fastapi_test_client

        # Add a test node first
        graph_service.add_nodes(
            nodes=[{"id": "api-test-1", "name": "API Test Node", "type": "Actor", "description": "Test"}],
            edges=[]
        )

        mock_llm.mock_tool_calls = [
            {"name": "search_graph", "input": {"query": "API Test"}}
        ]
        mock_llm.mock_text_response = "Found the API Test Node."

        response = client.post("/ui/chat", json={
            "messages": [{"role": "user", "content": "Search for API Test"}]
        })

        assert response.status_code == 200
        data = response.json()
        assert data["toolUsed"] == "search_graph"
        assert data["toolResult"] is not None

    def test_chat_endpoint_adds_nodes_via_graph_service(self, fastapi_test_client):
        """POST /ui/chat should add nodes through GraphService."""
        client, mock_llm, graph_service = fastapi_test_client

        mock_llm.mock_tool_calls = [
            {
                "name": "add_nodes",
                "input": {
                    "nodes": [{"id": "chat-added-1", "name": "Chat Added Node", "type": "Initiative", "description": "Added via chat"}],
                    "edges": []
                }
            }
        ]
        mock_llm.mock_text_response = "Added the node."

        response = client.post("/ui/chat", json={
            "messages": [{"role": "user", "content": "Add a new initiative"}]
        })

        assert response.status_code == 200

        # Verify node was added to GraphService
        result = graph_service.get_node_details("chat-added-1")
        assert result["success"]
        assert result["node"]["name"] == "Chat Added Node"


class TestSimpleChatEndpoint:
    """Tests for /ui/chat/simple endpoint."""

    def test_simple_chat_endpoint(self, fastapi_test_client):
        """POST /ui/chat/simple should work with just a message."""
        client, mock_llm, _ = fastapi_test_client

        mock_llm.mock_tool_calls = []
        mock_llm.mock_text_response = "Here are the graph statistics..."

        response = client.post("/ui/chat/simple", json={
            "message": "What are the graph stats?"
        })

        assert response.status_code == 200
        data = response.json()
        assert "content" in data


class TestUploadEndpoint:
    """Tests for /ui/upload endpoint."""

    def test_upload_text_file(self, fastapi_test_client):
        """POST /ui/upload should handle text files."""
        client, mock_llm, _ = fastapi_test_client

        mock_llm.mock_tool_calls = []
        mock_llm.mock_text_response = "The document discusses testing."

        # Create a test file
        file_content = b"This is a test document about software testing."

        response = client.post(
            "/ui/upload",
            files={"file": ("test.txt", io.BytesIO(file_content), "text/plain")},
            data={"analyze": "true", "message": "What is this about?"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"]
        assert "test" in data["text"].lower()

    def test_upload_with_analysis(self, fastapi_test_client):
        """POST /ui/upload with analyze=true should include chat response."""
        client, mock_llm, _ = fastapi_test_client

        mock_llm.mock_tool_calls = []
        mock_llm.mock_text_response = "This document describes a software project."

        file_content = b"Project Alpha: A new software initiative for digitalization."

        response = client.post(
            "/ui/upload",
            files={"file": ("project.txt", io.BytesIO(file_content), "text/plain")},
            data={"analyze": "true"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"]
        assert data["chat_response"] is not None
        assert "content" in data["chat_response"]

    def test_upload_extract_only(self, fastapi_test_client):
        """POST /ui/upload/extract should only extract text."""
        client, _, _ = fastapi_test_client

        file_content = b"Just extracting this text."

        response = client.post(
            "/ui/upload/extract",
            files={"file": ("extract.txt", io.BytesIO(file_content), "text/plain")}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"]
        assert "extracting" in data["text"].lower()
        # Should not have chat_response since we're only extracting
        assert "chat_response" not in data

    def test_upload_unsupported_format(self, fastapi_test_client):
        """POST /ui/upload should reject unsupported formats."""
        client, _, _ = fastapi_test_client

        file_content = b"Some binary content"

        response = client.post(
            "/ui/upload",
            files={"file": ("test.xyz", io.BytesIO(file_content), "application/octet-stream")},
            data={"analyze": "false"}
        )

        assert response.status_code == 200
        data = response.json()
        assert not data["success"]
        assert "unsupported" in data["error"].lower()


class TestInfoEndpoints:
    """Tests for info endpoints."""

    def test_info_endpoint(self, fastapi_test_client):
        """GET /ui/info should return service info."""
        client, _, _ = fastapi_test_client

        response = client.get("/ui/info")

        assert response.status_code == 200
        data = response.json()
        assert "provider" in data
        assert "available_tools" in data
        assert "graph_stats" in data

    def test_supported_formats_endpoint(self, fastapi_test_client):
        """GET /ui/supported-formats should return supported formats."""
        client, _, _ = fastapi_test_client

        response = client.get("/ui/supported-formats")

        assert response.status_code == 200
        data = response.json()
        assert "formats" in data
        assert ".pdf" in data["formats"]
        assert ".txt" in data["formats"]


class TestGraphServiceRouting:
    """Tests verifying all graph operations go through GraphService."""

    def test_chat_search_uses_graph_service(self, fastapi_test_client):
        """Search operations should use GraphService."""
        client, mock_llm, graph_service = fastapi_test_client

        # Add node via GraphService
        graph_service.add_nodes(
            nodes=[{"id": "routing-test-1", "name": "Routing Test", "type": "Actor", "description": "Test"}],
            edges=[]
        )

        # Search via chat
        mock_llm.mock_tool_calls = [
            {"name": "search_graph", "input": {"query": "Routing Test"}}
        ]
        mock_llm.mock_text_response = "Found the node."

        response = client.post("/ui/chat", json={
            "messages": [{"role": "user", "content": "Search Routing Test"}]
        })

        assert response.status_code == 200
        data = response.json()
        # Tool result should contain the node we added (check for nodes list or total)
        tool_result = data["toolResult"]
        assert tool_result.get("total", 0) >= 1 or len(tool_result.get("nodes", [])) >= 1

    def test_chat_update_uses_graph_service(self, fastapi_test_client):
        """Update operations should use GraphService."""
        client, mock_llm, graph_service = fastapi_test_client

        # Add node
        graph_service.add_nodes(
            nodes=[{"id": "update-test-1", "name": "Update Test", "type": "Actor", "description": "Original"}],
            edges=[]
        )

        # Update via chat
        mock_llm.mock_tool_calls = [
            {
                "name": "update_node",
                "input": {"node_id": "update-test-1", "updates": {"description": "Updated via chat"}}
            }
        ]
        mock_llm.mock_text_response = "Updated."

        response = client.post("/ui/chat", json={
            "messages": [{"role": "user", "content": "Update the node"}]
        })

        assert response.status_code == 200

        # Verify update via GraphService
        result = graph_service.get_node_details("update-test-1")
        assert result["node"]["description"] == "Updated via chat"

    def test_chat_delete_uses_graph_service(self, fastapi_test_client):
        """Delete operations should use GraphService."""
        client, mock_llm, graph_service = fastapi_test_client

        # Add node
        graph_service.add_nodes(
            nodes=[{"id": "delete-test-1", "name": "Delete Test", "type": "Actor", "description": "To delete"}],
            edges=[]
        )

        # Verify it exists
        assert graph_service.get_node_details("delete-test-1")["success"]

        # Delete via chat
        mock_llm.mock_tool_calls = [
            {"name": "delete_nodes", "input": {"node_ids": ["delete-test-1"], "confirmed": True}}
        ]
        mock_llm.mock_text_response = "Deleted."

        response = client.post("/ui/chat", json={
            "messages": [{"role": "user", "content": "Delete the node"}]
        })

        assert response.status_code == 200

        # Verify deletion via GraphService
        result = graph_service.get_node_details("delete-test-1")
        assert not result["success"]
