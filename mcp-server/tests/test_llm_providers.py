"""
Unit tests for LLM provider abstraction layer.
Tests provider factory, message format conversion, and provider interfaces.
"""

import pytest
import os
import json
from unittest.mock import Mock, patch, MagicMock
from llm_providers import (
    LLMProvider,
    ClaudeProvider,
    OpenAIProvider,
    LLMResponse,
    create_provider
)


class TestProviderFactory:
    """Test the provider factory function"""

    def test_create_claude_provider_explicit(self):
        """Test creating Claude provider explicitly"""
        provider = create_provider("test-key", "claude")
        assert isinstance(provider, ClaudeProvider)
        assert provider.model == "claude-sonnet-4-5"

    def test_create_openai_provider_explicit(self):
        """Test creating OpenAI provider explicitly"""
        provider = create_provider("test-key", "openai")
        assert isinstance(provider, OpenAIProvider)
        assert provider.model == "gpt-4o"

    def test_create_provider_defaults_to_claude(self):
        """Test that factory defaults to Claude when no provider specified"""
        with patch.dict(os.environ, {}, clear=True):
            provider = create_provider("test-key")
            assert isinstance(provider, ClaudeProvider)

    def test_create_provider_from_env_variable(self):
        """Test provider selection from LLM_PROVIDER env variable"""
        with patch.dict(os.environ, {"LLM_PROVIDER": "openai"}):
            provider = create_provider("test-key")
            assert isinstance(provider, OpenAIProvider)

    def test_create_provider_invalid_type_raises_error(self):
        """Test that invalid provider type raises ValueError"""
        with pytest.raises(ValueError, match="Unknown provider type"):
            create_provider("test-key", "invalid-provider")

    def test_openai_model_from_env_variable(self):
        """Test that OpenAI model can be set via env variable"""
        with patch.dict(os.environ, {"OPENAI_MODEL": "gpt-4-turbo"}):
            provider = create_provider("test-key", "openai")
            assert provider.model == "gpt-4-turbo"


class TestClaudeProvider:
    """Test Claude provider implementation"""

    def test_format_tool_definitions_returns_as_is(self):
        """Test that Claude provider returns tool definitions unchanged"""
        provider = ClaudeProvider("test-key")

        tools = [
            {
                "name": "test_tool",
                "description": "A test tool",
                "input_schema": {
                    "type": "object",
                    "properties": {"arg1": {"type": "string"}}
                }
            }
        ]

        formatted = provider.format_tool_definitions(tools)
        assert formatted == tools

    @patch('llm_providers.Anthropic')
    def test_create_completion_structure(self, mock_anthropic):
        """Test that create_completion calls Anthropic API with correct structure"""
        # Setup mock
        mock_client = Mock()
        mock_anthropic.return_value = mock_client

        mock_response = Mock()
        mock_response.content = [
            Mock(type="text", text="Hello world")
        ]
        mock_response.stop_reason = "end_turn"
        mock_client.messages.create.return_value = mock_response

        # Create provider and call
        provider = ClaudeProvider("test-key")
        messages = [{"role": "user", "content": "Hello"}]
        tools = [{"name": "test", "description": "test"}]

        response = provider.create_completion(
            messages=messages,
            system_prompt="Test prompt",
            tools=tools,
            max_tokens=1000
        )

        # Verify API was called correctly
        mock_client.messages.create.assert_called_once()
        call_kwargs = mock_client.messages.create.call_args[1]

        assert call_kwargs["model"] == "claude-sonnet-4-5"
        assert call_kwargs["max_tokens"] == 1000
        assert call_kwargs["system"] == "Test prompt"
        assert call_kwargs["tools"] == tools
        assert call_kwargs["messages"] == messages

        # Verify response format
        assert isinstance(response, LLMResponse)
        assert response.stop_reason == "end_turn"
        assert len(response.content) == 1
        assert response.content[0]["type"] == "text"
        assert response.content[0]["text"] == "Hello world"

    @patch('llm_providers.Anthropic')
    def test_create_completion_with_tool_use(self, mock_anthropic):
        """Test handling of tool use responses"""
        mock_client = Mock()
        mock_anthropic.return_value = mock_client

        mock_tool_block = Mock(
            type="tool_use",
            id="tool_123",
            name="search_graph",
            input={"query": "test"}
        )

        mock_response = Mock()
        mock_response.content = [mock_tool_block]
        mock_response.stop_reason = "tool_use"
        mock_client.messages.create.return_value = mock_response

        provider = ClaudeProvider("test-key")
        response = provider.create_completion(
            messages=[{"role": "user", "content": "Search for test"}],
            system_prompt="",
            tools=[],
            max_tokens=1000
        )

        assert response.stop_reason == "tool_use"
        assert len(response.content) == 1
        assert response.content[0]["type"] == "tool_use"
        assert response.content[0]["id"] == "tool_123"
        assert response.content[0]["name"] == "search_graph"
        assert response.content[0]["input"] == {"query": "test"}


class TestOpenAIProvider:
    """Test OpenAI provider implementation"""

    def test_format_tool_definitions_converts_to_functions(self):
        """Test that tool definitions are converted to OpenAI function format"""
        provider = OpenAIProvider("test-key")

        claude_tools = [
            {
                "name": "search_graph",
                "description": "Search the graph",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"}
                    },
                    "required": ["query"]
                }
            }
        ]

        openai_tools = provider.format_tool_definitions(claude_tools)

        assert len(openai_tools) == 1
        assert openai_tools[0]["type"] == "function"
        assert openai_tools[0]["function"]["name"] == "search_graph"
        assert openai_tools[0]["function"]["description"] == "Search the graph"
        assert openai_tools[0]["function"]["parameters"] == claude_tools[0]["input_schema"]

    def test_format_tool_definitions_empty_list(self):
        """Test handling of empty tool list"""
        provider = OpenAIProvider("test-key")
        assert provider.format_tool_definitions([]) == []
        assert provider.format_tool_definitions(None) == []

    def test_convert_messages_to_openai_simple(self):
        """Test conversion of simple messages"""
        provider = OpenAIProvider("test-key")

        claude_messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"}
        ]

        openai_messages = provider._convert_messages_to_openai(
            claude_messages,
            "You are a helpful assistant"
        )

        # Should have system message + 2 messages
        assert len(openai_messages) == 3
        assert openai_messages[0]["role"] == "system"
        assert openai_messages[0]["content"] == "You are a helpful assistant"
        assert openai_messages[1]["role"] == "user"
        assert openai_messages[1]["content"] == "Hello"
        assert openai_messages[2]["role"] == "assistant"
        assert openai_messages[2]["content"] == "Hi there"

    def test_convert_messages_with_tool_results(self):
        """Test conversion of messages with tool results"""
        provider = OpenAIProvider("test-key")

        claude_messages = [
            {"role": "user", "content": "Search for AI"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tool_1", "name": "search", "input": {"q": "AI"}}
                ]
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool_1", "content": '{"results": []}'}
                ]
            }
        ]

        openai_messages = provider._convert_messages_to_openai(claude_messages, "")

        # Check that tool result is converted to role: "tool"
        tool_message = [m for m in openai_messages if m.get("role") == "tool"]
        assert len(tool_message) == 1
        assert tool_message[0]["tool_call_id"] == "tool_1"
        assert tool_message[0]["content"] == '{"results": []}'

    def test_convert_assistant_message_with_tool_calls(self):
        """Test conversion of assistant messages with tool calls"""
        provider = OpenAIProvider("test-key")

        claude_messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me search for that"},
                    {
                        "type": "tool_use",
                        "id": "call_123",
                        "name": "search_graph",
                        "input": {"query": "test"}
                    }
                ]
            }
        ]

        openai_messages = provider._convert_messages_to_openai(claude_messages, "")

        assistant_msg = openai_messages[0]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["content"] == "Let me search for that"
        assert "tool_calls" in assistant_msg
        assert len(assistant_msg["tool_calls"]) == 1

        tool_call = assistant_msg["tool_calls"][0]
        assert tool_call["id"] == "call_123"
        assert tool_call["type"] == "function"
        assert tool_call["function"]["name"] == "search_graph"
        assert json.loads(tool_call["function"]["arguments"]) == {"query": "test"}

    def test_map_finish_reason(self):
        """Test mapping of OpenAI finish_reason to Claude stop_reason"""
        provider = OpenAIProvider("test-key")

        assert provider._map_finish_reason("stop") == "end_turn"
        assert provider._map_finish_reason("tool_calls") == "tool_use"
        assert provider._map_finish_reason("length") == "max_tokens"
        assert provider._map_finish_reason("content_filter") == "end_turn"
        assert provider._map_finish_reason("unknown") == "end_turn"

    @patch('llm_providers.OpenAI')
    def test_create_completion_structure(self, mock_openai_class):
        """Test that create_completion calls OpenAI API with correct structure"""
        # Setup mock
        mock_client = Mock()
        mock_openai_class.return_value = mock_client

        mock_message = Mock()
        mock_message.content = "Hello world"
        mock_message.tool_calls = None

        mock_choice = Mock()
        mock_choice.message = mock_message
        mock_choice.finish_reason = "stop"

        mock_response = Mock()
        mock_response.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_response

        # Create provider and call
        provider = OpenAIProvider("test-key")
        messages = [{"role": "user", "content": "Hello"}]
        tools = [{"name": "test", "description": "test", "input_schema": {}}]

        response = provider.create_completion(
            messages=messages,
            system_prompt="Test prompt",
            tools=tools,
            max_tokens=1000
        )

        # Verify API was called
        mock_client.chat.completions.create.assert_called_once()
        call_kwargs = mock_client.chat.completions.create.call_args[1]

        assert call_kwargs["model"] == "gpt-4o"
        assert call_kwargs["max_tokens"] == 1000
        assert call_kwargs["tools"] is not None

        # Verify response format
        assert isinstance(response, LLMResponse)
        assert response.stop_reason == "end_turn"
        assert len(response.content) == 1
        assert response.content[0]["type"] == "text"
        assert response.content[0]["text"] == "Hello world"

    @patch('llm_providers.OpenAI')
    def test_create_completion_with_tool_calls(self, mock_openai_class):
        """Test handling of tool calls in OpenAI response"""
        mock_client = Mock()
        mock_openai_class.return_value = mock_client

        # Mock tool call
        mock_tool_call = Mock()
        mock_tool_call.id = "call_abc123"
        mock_tool_call.function.name = "search_graph"
        mock_tool_call.function.arguments = '{"query": "test"}'

        mock_message = Mock()
        mock_message.content = None
        mock_message.tool_calls = [mock_tool_call]

        mock_choice = Mock()
        mock_choice.message = mock_message
        mock_choice.finish_reason = "tool_calls"

        mock_response = Mock()
        mock_response.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_response

        provider = OpenAIProvider("test-key")
        response = provider.create_completion(
            messages=[{"role": "user", "content": "Search"}],
            system_prompt="",
            tools=[],
            max_tokens=1000
        )

        assert response.stop_reason == "tool_use"
        assert len(response.content) == 1
        assert response.content[0]["type"] == "tool_use"
        assert response.content[0]["id"] == "call_abc123"
        assert response.content[0]["name"] == "search_graph"
        assert response.content[0]["input"] == {"query": "test"}


class TestLLMResponse:
    """Test LLMResponse unified response format"""

    def test_llm_response_creation(self):
        """Test creating LLMResponse"""
        content = [{"type": "text", "text": "Hello"}]
        response = LLMResponse(
            content=content,
            stop_reason="end_turn",
            raw_response={"test": "data"}
        )

        assert response.content == content
        assert response.stop_reason == "end_turn"
        assert response.raw_response == {"test": "data"}

    def test_llm_response_without_raw(self):
        """Test creating LLMResponse without raw_response"""
        content = [{"type": "text", "text": "Hello"}]
        response = LLMResponse(content=content, stop_reason="end_turn")

        assert response.content == content
        assert response.stop_reason == "end_turn"
        assert response.raw_response is None


class TestMessageFormatConversion:
    """Integration tests for message format conversion"""

    def test_roundtrip_simple_conversation(self):
        """Test that simple messages can be converted and remain semantically equivalent"""
        provider = OpenAIProvider("test-key")

        original_messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"}
        ]

        converted = provider._convert_messages_to_openai(original_messages, "System prompt")

        # Should preserve all user/assistant messages (plus system message)
        assert len(converted) == 4
        assert converted[0]["role"] == "system"
        assert all(msg["role"] in ["user", "assistant", "system"] for msg in converted)

    def test_complex_tool_workflow_conversion(self):
        """Test conversion of complex tool workflow"""
        provider = OpenAIProvider("test-key")

        # Simulate a full tool calling workflow
        claude_workflow = [
            {"role": "user", "content": "Search for AI projects"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "search", "input": {"q": "AI"}}
                ]
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": '{"count": 5}'}
                ]
            },
            {"role": "assistant", "content": "Found 5 projects"}
        ]

        converted = provider._convert_messages_to_openai(claude_workflow, "")

        # Verify structure is valid
        assert len(converted) > 0

        # Find tool message
        tool_msgs = [m for m in converted if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "t1"

        # Find assistant message with tool call
        assistant_msgs = [m for m in converted if m.get("role") == "assistant" and "tool_calls" in m]
        assert len(assistant_msgs) == 1


class TestProviderIntegration:
    """Integration tests for provider usage in ChatProcessor context"""

    @patch('llm_providers.Anthropic')
    def test_claude_provider_in_chat_processor_flow(self, mock_anthropic):
        """Test that ClaudeProvider works in the full ChatProcessor flow"""
        from chat_logic import ChatProcessor

        # Setup mock
        mock_client = Mock()
        mock_anthropic.return_value = mock_client

        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Test response")]
        mock_response.stop_reason = "end_turn"
        mock_client.messages.create.return_value = mock_response

        # Create ChatProcessor with mock tools
        tools_map = {"test_tool": lambda: {"result": "test"}}
        processor = ChatProcessor(tools_map)

        # Process message with Claude provider
        with patch.dict(os.environ, {"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "test-key"}):
            result = processor.process_message([{"role": "user", "content": "Hello"}])

        assert result is not None
        assert "content" in result
        assert result["content"] == "Test response"

    @patch('llm_providers.OpenAI')
    def test_openai_provider_in_chat_processor_flow(self, mock_openai_class):
        """Test that OpenAIProvider works in the full ChatProcessor flow"""
        from chat_logic import ChatProcessor

        # Setup mock
        mock_client = Mock()
        mock_openai_class.return_value = mock_client

        mock_message = Mock()
        mock_message.content = "Test response from OpenAI"
        mock_message.tool_calls = None

        mock_choice = Mock()
        mock_choice.message = mock_message
        mock_choice.finish_reason = "stop"

        mock_response = Mock()
        mock_response.choices = [mock_choice]
        mock_client.chat.completions.create.return_value = mock_response

        # Create ChatProcessor
        tools_map = {"test_tool": lambda: {"result": "test"}}
        processor = ChatProcessor(tools_map)

        # Process message with OpenAI provider
        with patch.dict(os.environ, {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "test-key"}):
            # Need to recreate processor to pick up env vars
            processor = ChatProcessor(tools_map)
            result = processor.process_message([{"role": "user", "content": "Hello"}])

        assert result is not None
        assert "content" in result
        assert result["content"] == "Test response from OpenAI"


if __name__ == "__main__":
    # Run with: pytest test_llm_providers.py -v
    pytest.main([__file__, "-v", "-s"])
