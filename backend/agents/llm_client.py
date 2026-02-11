"""
LLM Client for Agent Runtime.

Provides a simplified interface for agent LLM calls with tool support.
Reuses the existing LLM provider infrastructure.
"""

import json
import logging
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass

from backend.llm_providers import (
    LLMProvider,
    LLMResponse,
    ClaudeProvider,
    OpenAIProvider,
    create_provider,
)

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """Represents a tool call from the LLM."""
    id: str
    name: str
    input: Dict[str, Any]


@dataclass
class AgentTurn:
    """Result of a single agent turn."""
    text_response: Optional[str]
    tool_calls: List[ToolCall]
    stop_reason: str
    is_complete: bool  # True if no more tool calls needed


class LLMClient:
    """
    LLM client for agent execution.

    Provides methods for running agent turns with tool calling support.
    Uses the global LLM provider configuration.
    """

    def __init__(
        self,
        provider: str = "openai",
        model: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
    ):
        """
        Initialize the LLM client.

        Args:
            provider: LLM provider ("openai" or "anthropic")
            model: Model name (optional, uses provider default)
            openai_api_key: OpenAI API key
            anthropic_api_key: Anthropic API key
        """
        self.provider_name = provider
        self.model = model

        # Create the underlying provider
        self._provider = self._create_provider(
            provider=provider,
            openai_api_key=openai_api_key,
            anthropic_api_key=anthropic_api_key,
        )

    def _create_provider(
        self,
        provider: str,
        openai_api_key: Optional[str],
        anthropic_api_key: Optional[str],
    ) -> LLMProvider:
        """Create the appropriate LLM provider."""
        if provider.lower() in ("claude", "anthropic"):
            if not anthropic_api_key:
                raise ValueError("Anthropic API key required for Claude provider")
            return ClaudeProvider(api_key=anthropic_api_key)
        else:
            if not openai_api_key:
                raise ValueError("OpenAI API key required for OpenAI provider")
            return OpenAIProvider(api_key=openai_api_key)

    def run_turn(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int = 4096,
    ) -> AgentTurn:
        """
        Run a single agent turn.

        Args:
            system_prompt: System prompt including base agent prompt and task
            messages: Conversation history
            tools: Tool definitions in Claude format
            max_tokens: Maximum tokens for response

        Returns:
            AgentTurn with response and any tool calls
        """
        try:
            response = self._provider.create_completion(
                messages=messages,
                system_prompt=system_prompt,
                tools=tools,
                max_tokens=max_tokens,
            )

            return self._parse_response(response)

        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            raise

    def _parse_response(self, response: LLMResponse) -> AgentTurn:
        """Parse LLM response into AgentTurn."""
        text_response = None
        tool_calls = []

        for block in response.content:
            if block.get("type") == "text":
                text_response = block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    input=block.get("input", {}),
                ))

        # Determine if the turn is complete
        is_complete = response.stop_reason != "tool_use" and len(tool_calls) == 0

        return AgentTurn(
            text_response=text_response,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason,
            is_complete=is_complete,
        )

    def execute_with_tools(
        self,
        system_prompt: str,
        user_message: str,
        tools: List[Dict[str, Any]],
        tool_executor: Callable[[str, Dict[str, Any]], Any],
        max_turns: int = 10,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        """
        Execute a complete agent interaction with tool calling.

        Runs the LLM in a loop, executing tool calls until the agent
        produces a final response or max_turns is reached.

        Args:
            system_prompt: System prompt for the agent
            user_message: Initial user message (event to process)
            tools: Tool definitions
            tool_executor: Function to execute tools (name, input) -> result
            max_turns: Maximum number of LLM turns
            max_tokens: Maximum tokens per turn

        Returns:
            Dict with final response and execution trace
        """
        messages = [{"role": "user", "content": user_message}]
        trace = []
        turn_count = 0

        while turn_count < max_turns:
            turn_count += 1

            # Run a turn
            turn = self.run_turn(
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
            )

            # Record turn in trace
            turn_record = {
                "turn": turn_count,
                "text_response": turn.text_response,
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "input": tc.input}
                    for tc in turn.tool_calls
                ],
                "stop_reason": turn.stop_reason,
            }
            trace.append(turn_record)

            # If no tool calls, we're done
            if turn.is_complete:
                return {
                    "success": True,
                    "final_response": turn.text_response,
                    "turns": turn_count,
                    "trace": trace,
                }

            # Execute tool calls and build response
            tool_results = []
            for tc in turn.tool_calls:
                try:
                    result = tool_executor(tc.name, tc.input)
                    tool_results.append({
                        "tool_use_id": tc.id,
                        "name": tc.name,
                        "result": result,
                        "is_error": False,
                    })
                except Exception as e:
                    logger.warning(f"Tool {tc.name} failed: {e}")
                    tool_results.append({
                        "tool_use_id": tc.id,
                        "name": tc.name,
                        "result": {"error": str(e)},
                        "is_error": True,
                    })

            # Add assistant message with tool calls
            assistant_content = []
            if turn.text_response:
                assistant_content.append({
                    "type": "text",
                    "text": turn.text_response,
                })
            for tc in turn.tool_calls:
                assistant_content.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.input,
                })

            messages.append({
                "role": "assistant",
                "content": assistant_content,
            })

            # Add tool results
            user_content = []
            for tr in tool_results:
                user_content.append({
                    "type": "tool_result",
                    "tool_use_id": tr["tool_use_id"],
                    "content": json.dumps(tr["result"], default=str),
                    "is_error": tr["is_error"],
                })

            messages.append({
                "role": "user",
                "content": user_content,
            })

        # Max turns reached
        return {
            "success": False,
            "error": f"Max turns ({max_turns}) reached without completion",
            "final_response": turn.text_response if turn else None,
            "turns": turn_count,
            "trace": trace,
        }


def create_llm_client_from_settings(settings: "AgentsSettings") -> LLMClient:
    """
    Create an LLM client from agent settings.

    Args:
        settings: AgentsSettings instance

    Returns:
        Configured LLMClient
    """
    from .config import AgentsSettings

    return LLMClient(
        provider=settings.llm_provider,
        model=settings.llm_model,
        openai_api_key=settings.openai_api_key,
        anthropic_api_key=settings.anthropic_api_key,
    )
