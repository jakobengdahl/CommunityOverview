"""
LLM Integration tests - tests that actually call Anthropic API
These tests verify that:
1. MCP tools are correctly invoked by Claude
2. System prompt guides Claude to use tools appropriately
3. Tool results are properly formatted and returned

These tests only run when ANTHROPIC_API_KEY is available in environment.
"""

import pytest
import os
from chat_logic import ChatProcessor
from graph_storage import GraphStorage

# Skip all tests in this module if no API key is available
pytestmark = pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not available - skipping LLM integration tests"
)

@pytest.fixture
def graph_storage(tmp_path):
    """Create a test graph storage with sample data"""
    graph_file = tmp_path / "test_graph.json"

    # Create sample graph data
    sample_data = {
        "nodes": [
            {
                "id": "actor_1",
                "name": "Digg",
                "type": "Actor",
                "description": "Myndigheten för digital förvaltning",
                "summary": "Ansvarar för digital utveckling",
                "communities": ["eSam"],
                "metadata": {},
                "created_at": "2025-01-01T00:00:00",
                "updated_at": "2025-01-01T00:00:00"
            },
            {
                "id": "legislation_1",
                "name": "NIS2-direktivet",
                "type": "Legislation",
                "description": "EU-direktiv om cybersäkerhet",
                "summary": "Krav på säkerhetsåtgärder",
                "communities": ["eSam"],
                "metadata": {},
                "created_at": "2025-01-01T00:00:00",
                "updated_at": "2025-01-01T00:00:00"
            },
            {
                "id": "initiative_1",
                "name": "NIS2-implementering",
                "type": "Initiative",
                "description": "Projekt för NIS2-implementering",
                "summary": "Genomförande av NIS2",
                "communities": ["eSam"],
                "metadata": {},
                "created_at": "2025-01-01T00:00:00",
                "updated_at": "2025-01-01T00:00:00"
            }
        ],
        "edges": [
            {
                "id": "edge_1",
                "source": "initiative_1",
                "target": "actor_1",
                "type": "BELONGS_TO"
            },
            {
                "id": "edge_2",
                "source": "initiative_1",
                "target": "legislation_1",
                "type": "IMPLEMENTS"
            }
        ]
    }

    # Write sample data
    import json
    with open(graph_file, 'w') as f:
        json.dump(sample_data, f)

    return GraphStorage(str(graph_file))

@pytest.fixture
def tools_map(graph_storage):
    """Create tools map for testing"""
    return {
        "search_graph": lambda **kwargs: graph_storage.search_nodes(**{k: v for k, v in kwargs.items() if k in ['query', 'node_types', 'communities', 'limit']}),
        "get_node_details": lambda node_id: {"success": True, "node": graph_storage.get_node(node_id).model_dump() if graph_storage.get_node(node_id) else None},
        "get_related_nodes": lambda **kwargs: graph_storage.get_related_nodes(**kwargs),
        "find_similar_nodes": lambda **kwargs: graph_storage.find_similar_nodes(**kwargs),
    }

@pytest.fixture
def chat_processor(tools_map):
    """Create ChatProcessor instance for testing"""
    return ChatProcessor(tools_map)

def test_llm_uses_search_tool_for_search_query(chat_processor):
    """Test that Claude uses search_graph tool when asked to search"""
    messages = [
        {"role": "user", "content": "Sök efter NIS2"}
    ]

    response = chat_processor.process_message(messages)

    # Verify response
    assert response is not None
    assert "content" in response
    assert response["content"] is not None

    # Verify tool was used
    assert response.get("toolUsed") == "search_graph", \
        f"Expected search_graph tool to be used, got {response.get('toolUsed')}"

    # Response should mention finding results
    assert any(keyword in response["content"].lower() for keyword in ["nis2", "found", "hittade", "result"]), \
        f"Response should mention search results: {response['content']}"

def test_llm_uses_get_related_nodes_tool(chat_processor):
    """Test that Claude uses get_related_nodes when asked about connections"""
    messages = [
        {"role": "user", "content": "Vilka noder är kopplade till Digg?"}
    ]

    response = chat_processor.process_message(messages)

    assert response is not None
    assert "content" in response

    # Claude should use search first to find Digg, then get related nodes
    # or just use get_related_nodes if it knows the ID
    assert response.get("toolUsed") in ["search_graph", "get_related_nodes"], \
        f"Expected search or get_related tool, got {response.get('toolUsed')}"

def test_llm_uses_find_similar_before_proposing_node(chat_processor):
    """Test that Claude checks for duplicates before proposing new nodes"""
    messages = [
        {"role": "user", "content": "Lägg till en ny Actor som heter 'Digital Agency'"}
    ]

    response = chat_processor.process_message(messages)

    assert response is not None
    assert "content" in response

    # Claude should use find_similar_nodes to check for duplicates
    assert response.get("toolUsed") in ["find_similar_nodes", "propose_new_node"], \
        f"Expected find_similar or propose tool, got {response.get('toolUsed')}"

    # Response should ask about duplicates or propose node
    assert any(keyword in response["content"].lower() for keyword in [
        "similar", "liknande", "duplicate", "dubblett", "propose", "föreslå"
    ]), f"Response should mention similarity check or proposal: {response['content']}"

def test_llm_responds_in_swedish(chat_processor):
    """Test that Claude responds in Swedish as configured in system prompt"""
    messages = [
        {"role": "user", "content": "Vad finns i grafen?"}
    ]

    response = chat_processor.process_message(messages)

    assert response is not None
    assert "content" in response

    # Response should contain Swedish words
    swedish_indicators = ["är", "och", "för", "i", "med", "på", "det", "som", "till", "av"]
    response_lower = response["content"].lower()

    assert any(word in response_lower for word in swedish_indicators), \
        f"Response should be in Swedish: {response['content']}"

def test_llm_handles_graph_stats_query(chat_processor, tools_map):
    """Test that Claude can answer questions about graph statistics"""
    # Add get_graph_stats to tools
    from models import GraphStats

    def get_stats(**kwargs):
        return GraphStats(
            total_nodes=3,
            total_edges=2,
            nodes_by_type={"Actor": 1, "Legislation": 1, "Initiative": 1},
            nodes_by_community={"eSam": 3}
        ).model_dump()

    tools_map["get_graph_stats"] = get_stats

    messages = [
        {"role": "user", "content": "Hur många noder finns i grafen?"}
    ]

    response = chat_processor.process_message(messages)

    assert response is not None
    assert "content" in response

    # Response should mention the number of nodes
    assert "3" in response["content"] or "tre" in response["content"].lower(), \
        f"Response should mention node count: {response['content']}"

def test_llm_tool_error_handling(chat_processor):
    """Test that Claude handles tool errors gracefully"""
    messages = [
        {"role": "user", "content": "Visa detaljer för nod med ID 'nonexistent_node_123'"}
    ]

    response = chat_processor.process_message(messages)

    assert response is not None
    assert "content" in response

    # Response should indicate that node was not found
    assert any(keyword in response["content"].lower() for keyword in [
        "not found", "finns inte", "hittades inte", "existerar inte"
    ]), f"Response should indicate node not found: {response['content']}"

def test_llm_system_prompt_effectiveness(chat_processor):
    """Test that system prompt effectively guides Claude's behavior"""
    # Test various scenarios to verify system prompt is working

    # 1. Claude should use tools, not fabricate data
    messages = [{"role": "user", "content": "Vilka initiativ finns?"}]
    response = chat_processor.process_message(messages)
    assert response.get("toolUsed") is not None, "Claude should use tools, not fabricate data"

    # 2. Claude should be transparent about tool usage
    assert any(keyword in response["content"].lower() for keyword in [
        "söker", "searching", "looking", "using", "tool"
    ]) or response.get("toolUsed"), \
        "Claude should be transparent about tool usage"

def test_llm_mcp_integration_end_to_end(chat_processor):
    """
    End-to-end test of LLM + MCP integration
    Tests a realistic conversation flow
    """
    # Conversation flow:
    # 1. User asks to search
    # 2. User asks about related nodes
    # 3. User asks about adding a node

    conversation = []

    # Step 1: Search
    conversation.append({"role": "user", "content": "Hitta information om NIS2"})
    response1 = chat_processor.process_message(conversation)

    assert response1 is not None
    assert response1.get("toolUsed") == "search_graph"

    conversation.append({"role": "assistant", "content": response1["content"]})

    # Step 2: Ask about connections
    conversation.append({"role": "user", "content": "Vilka initiativ är kopplade till NIS2?"})
    response2 = chat_processor.process_message(conversation)

    assert response2 is not None
    # Should use get_related_nodes or search
    assert response2.get("toolUsed") in ["get_related_nodes", "search_graph"]

    conversation.append({"role": "assistant", "content": response2["content"]})

    # Step 3: Propose adding a node
    conversation.append({"role": "user", "content": "Lägg till ett nytt initiativ om cybersäkerhet"})
    response3 = chat_processor.process_message(conversation)

    assert response3 is not None
    # Should check for duplicates first
    assert response3.get("toolUsed") in ["find_similar_nodes", "propose_new_node"]

    # Entire conversation should flow naturally
    assert all(r["content"] for r in [response1, response2, response3])

if __name__ == "__main__":
    # Run tests with: pytest test_llm_integration.py -v
    # Or skip if no API key: pytest test_llm_integration.py -v -m "not skipif"
    pytest.main([__file__, "-v", "-s"])
