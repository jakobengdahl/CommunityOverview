"""
Integration tests for UI Backend with full app stack.

Tests that:
- UI Backend is correctly mounted in the app
- Chat requests flow through to GraphService
- Graph mutations via chat update the actual graph
- Document uploads work end-to-end
"""

import pytest
from unittest.mock import patch
import io


class TestUIBackendMounting:
    """Tests that UI Backend is correctly mounted."""

    def test_ui_endpoints_available(self, test_app_with_mock):
        """UI Backend endpoints should be accessible."""
        client, _ = test_app_with_mock
        # Check /ui/info endpoint
        response = client.get("/ui/info")
        assert response.status_code == 200
        data = response.json()
        assert "provider" in data
        assert "available_tools" in data

    def test_root_includes_ui_endpoint(self, test_app_with_mock):
        """Root endpoint should list /ui endpoint."""
        client, _ = test_app_with_mock
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "/ui" in data["endpoints"]["ui"]

    def test_supported_formats_endpoint(self, test_app_with_mock):
        """Supported formats endpoint should work."""
        client, _ = test_app_with_mock
        response = client.get("/ui/supported-formats")
        assert response.status_code == 200
        data = response.json()
        assert ".pdf" in data["formats"]
        assert ".txt" in data["formats"]


class TestChatIntegration:
    """Integration tests for chat with GraphService."""

    def test_chat_with_search_tool(self, test_app_with_mock):
        """Chat search should query GraphService and return results."""
        client, mock_llm = test_app_with_mock
        mock_llm.mock_tool_calls = [
            {"name": "search_graph", "input": {"query": "Test", "limit": 10}}
        ]
        mock_llm.mock_text_response = "Found test nodes."

        response = client.post("/ui/chat", json={
            "messages": [{"role": "user", "content": "Search for Test"}]
        })

        assert response.status_code == 200
        data = response.json()
        assert data["toolUsed"] == "search_graph"
        # Should find the sample nodes from fixture
        tool_result = data["toolResult"]
        assert tool_result.get("total", 0) >= 1 or len(tool_result.get("nodes", [])) >= 1

    def test_chat_add_node_updates_graph(self, test_app_with_mock):
        """Adding a node via chat should persist in GraphService."""
        client, mock_llm = test_app_with_mock
        # First add a node
        mock_llm.mock_tool_calls = [
            {
                "name": "add_nodes",
                "input": {
                    "nodes": [{
                        "id": "integration-test-node",
                        "name": "Integration Test Node",
                        "type": "Actor",
                        "description": "Added via integration test"
                    }],
                    "edges": []
                }
            }
        ]
        mock_llm.mock_text_response = "Added the node."

        response = client.post("/ui/chat", json={
            "messages": [{"role": "user", "content": "Add a test node"}]
        })
        assert response.status_code == 200

        # Reset mock for search
        mock_llm.reset()
        mock_llm.mock_tool_calls = [
            {"name": "search_graph", "input": {"query": "Integration Test Node"}}
        ]
        mock_llm.mock_text_response = "Found it."

        # Search for the added node
        response = client.post("/ui/chat", json={
            "messages": [{"role": "user", "content": "Find Integration Test Node"}]
        })
        assert response.status_code == 200
        data = response.json()
        # Node should be found
        tool_result = data["toolResult"]
        assert tool_result.get("total", 0) >= 1 or len(tool_result.get("nodes", [])) >= 1

    def test_chat_update_node_persists(self, test_app_with_mock):
        """Updating a node via chat should persist in GraphService."""
        client, mock_llm = test_app_with_mock
        # Update existing node from sample data
        mock_llm.mock_tool_calls = [
            {
                "name": "update_node",
                "input": {
                    "node_id": "node-1",
                    "updates": {"description": "Updated via integration test"}
                }
            }
        ]
        mock_llm.mock_text_response = "Updated."

        response = client.post("/ui/chat", json={
            "messages": [{"role": "user", "content": "Update node-1"}]
        })
        assert response.status_code == 200

        # Verify via REST API
        response = client.get("/api/nodes/node-1")
        assert response.status_code == 200
        data = response.json()
        assert data["node"]["description"] == "Updated via integration test"

    def test_chat_delete_node_removes_from_graph(self, test_app_with_mock):
        """Deleting a node via chat should remove it from GraphService."""
        client, mock_llm = test_app_with_mock
        # First add a node to delete
        mock_llm.mock_tool_calls = [
            {
                "name": "add_nodes",
                "input": {
                    "nodes": [{
                        "id": "to-delete-node",
                        "name": "Node To Delete",
                        "type": "Actor",
                        "description": "Will be deleted"
                    }],
                    "edges": []
                }
            }
        ]
        mock_llm.mock_text_response = "Added."
        client.post("/ui/chat", json={
            "messages": [{"role": "user", "content": "Add node"}]
        })

        # Verify it exists via REST
        response = client.get("/api/nodes/to-delete-node")
        assert response.status_code == 200

        # Now delete it
        mock_llm.reset()
        mock_llm.mock_tool_calls = [
            {"name": "delete_nodes", "input": {"node_ids": ["to-delete-node"], "confirmed": True}}
        ]
        mock_llm.mock_text_response = "Deleted."

        response = client.post("/ui/chat", json={
            "messages": [{"role": "user", "content": "Delete the node"}]
        })
        assert response.status_code == 200

        # Verify it's gone via REST (REST returns 404 for non-existent nodes)
        response = client.get("/api/nodes/to-delete-node")
        # Node should either return 404 or 200 with success=false
        if response.status_code == 200:
            assert not response.json()["success"]
        else:
            assert response.status_code == 404


class TestDocumentUploadIntegration:
    """Integration tests for document upload."""

    def test_upload_text_file_extracts_content(self, test_app_with_mock):
        """Uploading a text file should extract content."""
        client, _ = test_app_with_mock
        file_content = b"This is a test document about government services."

        response = client.post(
            "/ui/upload/extract",
            files={"file": ("test.txt", io.BytesIO(file_content), "text/plain")}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"]
        assert "government" in data["text"].lower()

    def test_upload_with_analysis(self, test_app_with_mock):
        """Uploading with analysis should process through ChatService."""
        client, mock_llm = test_app_with_mock
        mock_llm.mock_tool_calls = []
        mock_llm.mock_text_response = "The document discusses government services."

        file_content = b"A document about digital transformation in government."

        response = client.post(
            "/ui/upload",
            files={"file": ("gov.txt", io.BytesIO(file_content), "text/plain")},
            data={"analyze": "true", "message": "What is this about?"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"]
        assert data["chat_response"] is not None

    def test_upload_unsupported_format_rejected(self, test_app_with_mock):
        """Unsupported file formats should be rejected."""
        client, _ = test_app_with_mock
        response = client.post(
            "/ui/upload",
            files={"file": ("test.xyz", io.BytesIO(b"data"), "application/octet-stream")},
            data={"analyze": "false"}
        )

        assert response.status_code == 200
        data = response.json()
        assert not data["success"]


class TestGraphServiceSharing:
    """Tests that GraphService is properly shared between REST and UI Backend."""

    def test_rest_and_chat_share_graph(self, test_app_with_mock):
        """REST API and Chat should share the same GraphService."""
        client, mock_llm = test_app_with_mock
        # Add node via REST API
        response = client.post("/api/nodes", json={
            "nodes": [{
                "id": "shared-test-node",
                "name": "Shared Test",
                "type": "Actor",
                "description": "Added via REST"
            }],
            "edges": []
        })
        assert response.status_code == 200

        # Search for it via chat
        mock_llm.mock_tool_calls = [
            {"name": "search_graph", "input": {"query": "Shared Test"}}
        ]
        mock_llm.mock_text_response = "Found."

        response = client.post("/ui/chat", json={
            "messages": [{"role": "user", "content": "Find Shared Test"}]
        })
        assert response.status_code == 200
        data = response.json()
        # Should find the node added via REST
        tool_result = data["toolResult"]
        assert tool_result.get("total", 0) >= 1 or len(tool_result.get("nodes", [])) >= 1

    def test_chat_changes_visible_in_rest(self, test_app_with_mock):
        """Changes made via chat should be visible via REST API."""
        client, mock_llm = test_app_with_mock
        # Add node via chat
        mock_llm.mock_tool_calls = [
            {
                "name": "add_nodes",
                "input": {
                    "nodes": [{
                        "id": "chat-to-rest-node",
                        "name": "Chat to REST",
                        "type": "Initiative",
                        "description": "Added via chat"
                    }],
                    "edges": []
                }
            }
        ]
        mock_llm.mock_text_response = "Added."

        response = client.post("/ui/chat", json={
            "messages": [{"role": "user", "content": "Add node"}]
        })
        assert response.status_code == 200

        # Query via REST
        response = client.get("/api/nodes/chat-to-rest-node")
        assert response.status_code == 200
        data = response.json()
        assert data["success"]
        assert data["node"]["name"] == "Chat to REST"

    def test_mcp_and_chat_share_graph(self, test_app_with_mock):
        """MCP execute_tool and Chat should share the same GraphService."""
        client, mock_llm = test_app_with_mock
        # Add node via execute_tool (MCP)
        response = client.post("/execute_tool", json={
            "tool_name": "add_nodes",
            "arguments": {
                "nodes": [{
                    "id": "mcp-to-chat-node",
                    "name": "MCP to Chat",
                    "type": "Resource",
                    "description": "Added via MCP"
                }],
                "edges": []
            }
        })
        assert response.status_code == 200

        # Search via chat
        mock_llm.mock_tool_calls = [
            {"name": "search_graph", "input": {"query": "MCP to Chat"}}
        ]
        mock_llm.mock_text_response = "Found."

        response = client.post("/ui/chat", json={
            "messages": [{"role": "user", "content": "Find MCP to Chat"}]
        })
        assert response.status_code == 200
        data = response.json()
        tool_result = data["toolResult"]
        assert tool_result.get("total", 0) >= 1 or len(tool_result.get("nodes", [])) >= 1


class TestSimpleChatEndpoint:
    """Tests for simplified chat endpoint."""

    def test_simple_chat_works(self, test_app_with_mock):
        """Simple chat endpoint should work."""
        client, mock_llm = test_app_with_mock
        mock_llm.mock_tool_calls = []
        mock_llm.mock_text_response = "Hello! I can help with the graph."

        response = client.post("/ui/chat/simple", json={
            "message": "Hello"
        })

        assert response.status_code == 200
        data = response.json()
        assert "content" in data
