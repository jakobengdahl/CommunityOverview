"""
LLM Provider abstraction layer for supporting multiple AI backends.
Supports both Claude (Anthropic) and OpenAI APIs.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
import json
import os


class LLMProvider(ABC):
    """Abstract base class for LLM providers"""

    @abstractmethod
    def create_completion(
        self,
        messages: List[Dict],
        system_prompt: str,
        tools: List[Dict],
        max_tokens: int = 4096
    ) -> 'LLMResponse':
        """Create a completion with the LLM"""
        pass

    @abstractmethod
    def format_tool_definitions(self, tools: List[Dict]) -> Any:
        """Format tool definitions for the specific provider"""
        pass


class LLMResponse:
    """Unified response format across providers"""

    def __init__(
        self,
        content: List[Dict],
        stop_reason: str,
        raw_response: Any = None
    ):
        self.content = content
        self.stop_reason = stop_reason
        self.raw_response = raw_response


class ClaudeProvider(LLMProvider):
    """Anthropic Claude provider"""

    def __init__(self, api_key: str):
        from anthropic import Anthropic
        self.client = Anthropic(api_key=api_key)
        self.model = "claude-sonnet-4-5"

    def create_completion(
        self,
        messages: List[Dict],
        system_prompt: str,
        tools: List[Dict],
        max_tokens: int = 4096
    ) -> LLMResponse:
        """Create completion using Claude API"""
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=tools,
            messages=messages
        )

        # Convert Claude response to unified format
        content = []
        for block in response.content:
            if block.type == "text":
                content.append({
                    "type": "text",
                    "text": block.text
                })
            elif block.type == "tool_use":
                content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input
                })

        return LLMResponse(
            content=content,
            stop_reason=response.stop_reason,
            raw_response=response
        )

    def format_tool_definitions(self, tools: List[Dict]) -> List[Dict]:
        """Claude uses the tool definitions as-is"""
        return tools


class OpenAIProvider(LLMProvider):
    """OpenAI provider"""

    def __init__(self, api_key: str):
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key)
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o")

    def create_completion(
        self,
        messages: List[Dict],
        system_prompt: str,
        tools: List[Dict],
        max_tokens: int = 4096
    ) -> LLMResponse:
        """Create completion using OpenAI API"""

        # Convert messages to OpenAI format
        openai_messages = self._convert_messages_to_openai(messages, system_prompt)

        # Format tools for OpenAI
        openai_tools = self.format_tool_definitions(tools)

        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=openai_messages,
            tools=openai_tools if openai_tools else None,
            tool_choice="auto" if openai_tools else None
        )

        # Convert OpenAI response to unified format
        message = response.choices[0].message
        content = []

        # Add text content if present
        if message.content:
            content.append({
                "type": "text",
                "text": message.content
            })

        # Add tool calls if present
        if message.tool_calls:
            for tool_call in message.tool_calls:
                content.append({
                    "type": "tool_use",
                    "id": tool_call.id,
                    "name": tool_call.function.name,
                    "input": json.loads(tool_call.function.arguments)
                })

        # Map finish_reason to stop_reason
        stop_reason = self._map_finish_reason(response.choices[0].finish_reason)

        return LLMResponse(
            content=content,
            stop_reason=stop_reason,
            raw_response=response
        )

    def format_tool_definitions(self, tools: List[Dict]) -> List[Dict]:
        """Convert Claude tool format to OpenAI function calling format"""
        if not tools:
            return []

        openai_tools = []
        for tool in tools:
            openai_tool = {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"]
                }
            }
            openai_tools.append(openai_tool)

        return openai_tools

    def _convert_messages_to_openai(
        self,
        messages: List[Dict],
        system_prompt: str
    ) -> List[Dict]:
        """Convert Claude message format to OpenAI format"""
        openai_messages = []

        # Add system prompt as first message
        if system_prompt:
            openai_messages.append({
                "role": "system",
                "content": system_prompt
            })

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            if role == "user":
                # Handle both string content and array of content blocks
                if isinstance(content, str):
                    openai_messages.append({
                        "role": "user",
                        "content": content
                    })
                elif isinstance(content, list):
                    # Check if this is a tool_result message
                    if content and content[0].get("type") == "tool_result":
                        # Convert tool results to OpenAI format
                        for item in content:
                            openai_messages.append({
                                "role": "tool",
                                "tool_call_id": item.get("tool_use_id"),
                                "content": item.get("content", "")
                            })
                    else:
                        # Regular content blocks
                        text_parts = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                        if text_parts:
                            openai_messages.append({
                                "role": "user",
                                "content": "\n".join(text_parts)
                            })

            elif role == "assistant":
                # Handle assistant messages with tool calls
                if isinstance(content, list):
                    text_parts = []
                    tool_calls = []

                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                            elif block.get("type") == "tool_use":
                                tool_calls.append({
                                    "id": block.get("id"),
                                    "type": "function",
                                    "function": {
                                        "name": block.get("name"),
                                        "arguments": json.dumps(block.get("input", {}))
                                    }
                                })

                    msg_dict = {"role": "assistant"}
                    if text_parts:
                        msg_dict["content"] = "\n".join(text_parts)
                    if tool_calls:
                        msg_dict["tool_calls"] = tool_calls

                    openai_messages.append(msg_dict)
                elif isinstance(content, str):
                    openai_messages.append({
                        "role": "assistant",
                        "content": content
                    })

        return openai_messages

    def _map_finish_reason(self, finish_reason: str) -> str:
        """Map OpenAI finish_reason to Claude stop_reason"""
        mapping = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
            "content_filter": "end_turn"
        }
        return mapping.get(finish_reason, "end_turn")


def create_provider(api_key: str, provider_type: Optional[str] = None) -> LLMProvider:
    """
    Factory function to create the appropriate LLM provider.

    Args:
        api_key: API key for the provider
        provider_type: 'claude' or 'openai'. If None, uses LLM_PROVIDER env var (defaults to 'claude')

    Returns:
        LLMProvider instance
    """
    if provider_type is None:
        provider_type = os.getenv("LLM_PROVIDER", "claude").lower()

    if provider_type == "openai":
        return OpenAIProvider(api_key)
    elif provider_type == "claude":
        return ClaudeProvider(api_key)
    else:
        raise ValueError(f"Unknown provider type: {provider_type}. Supported: 'claude', 'openai'")
