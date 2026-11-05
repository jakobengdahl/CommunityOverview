"""
LLM Client for Agent Runtime.

Provides a simplified interface for agent LLM calls with tool support.
Reuses the existing LLM provider infrastructure.
"""

import json
import logging
from typing import List, Dict, Any, Optional, Callable, TYPE_CHECKING
from dataclasses import dataclass

if TYPE_CHECKING:
    from .config import AgentsSettings
    from backend.config.model_profiles import ModelProfile

from backend.llm.llm_providers import (
    LLMProvider,
    LLMResponse,
    ClaudeProvider,
    OpenAIProvider,
    provider_max_tokens,
    TOOL_CALLS_NOT_RUN_MESSAGE,
    tool_input_type_error,
)

from backend.runtime.content_logging import is_tainted, log_failure, safe_tool_name

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
        llm_provider: Optional[LLMProvider] = None,
    ):
        """
        Initialize the LLM client.

        Args:
            provider: LLM provider ("openai" or "anthropic")
            model: Model name (optional, uses provider default)
            openai_api_key: OpenAI API key
            anthropic_api_key: Anthropic API key
            llm_provider: Optional pre-built LLMProvider instance (e.g. resolved
                from a model profile). When given, it is used as-is instead of
                building one from the key args above.
        """
        self.provider_name = provider
        self.model = model

        self._provider = llm_provider or self._create_provider(
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
        max_tokens: Optional[int] = None,
    ) -> AgentTurn:
        """
        Run a single agent turn.

        Args:
            system_prompt: System prompt including base agent prompt and task
            messages: Conversation history
            tools: Tool definitions in Claude format
            max_tokens: Maximum tokens for response (default: resolved from
                the provider's profile options, LLM_MAX_TOKENS or the default)

        Returns:
            AgentTurn with response and any tool calls
        """
        try:
            response = self._provider.create_completion(
                messages=messages,
                system_prompt=system_prompt,
                tools=tools,
                max_tokens=provider_max_tokens(self._provider)
                if max_tokens is None
                else max_tokens,
            )

            return self._parse_response(response)

        except Exception as e:
            log_failure(logger, logging.ERROR, "llm_failed", e)
            raise

    def _parse_response(self, response: LLMResponse) -> AgentTurn:
        """Parse LLM response into AgentTurn."""
        text_response = None
        tool_calls = []

        for block in response.content:
            if block.get("type") == "text":
                text_response = block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        input=block.get("input", {}),
                    )
                )

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
        max_tokens: Optional[int] = None,
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

            # A batch at the token limit may be partial, so none of it runs. The
            # trace keeps what earlier turns did; "tool_calls" lists only calls
            # handed to dispatch, since the worker reports those as actions.
            if turn.stop_reason == "max_tokens" and turn.tool_calls:
                turn_record["tool_calls_not_run"] = turn_record["tool_calls"]
                turn_record["tool_calls"] = []
                logger.warning(
                    "Reply hit the token limit; tool calls not run: %s",
                    [
                        safe_tool_name(tc.name) if is_tainted() else tc.name
                        for tc in turn.tool_calls
                    ],
                )
                return {
                    "success": False,
                    "error": TOOL_CALLS_NOT_RUN_MESSAGE,
                    "final_response": turn.text_response,
                    "turns": turn_count,
                    "trace": trace,
                }

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
                input_error = tool_input_type_error(tc.name, tc.input)
                if input_error is not None:
                    from backend.service.attachment_tools import (
                        audit_attachment_refusal,
                    )

                    audit_attachment_refusal(
                        tc.name.removeprefix("GRAPH__"), "invalid_arguments"
                    )
                    tool_results.append(
                        {
                            "tool_use_id": tc.id,
                            "result": {"error": input_error},
                            "is_error": True,
                        }
                    )
                    continue
                try:
                    result = tool_executor(tc.name, tc.input)
                    tool_results.append(
                        {
                            "tool_use_id": tc.id,
                            "result": result,
                            "is_error": False,
                        }
                    )
                except Exception as e:
                    from backend.runtime.attachment_access import ATTACHMENT_TOOLS

                    redact = (
                        is_tainted()
                        or tc.name.removeprefix("GRAPH__") in ATTACHMENT_TOOLS
                    )
                    log_failure(
                        logger,
                        logging.WARNING,
                        "tool_failed",
                        e,
                        redact=redact,
                        context=f"Tool {safe_tool_name(tc.name) if redact else tc.name}",
                    )

                    tool_results.append(
                        {
                            "tool_use_id": tc.id,
                            "result": {"error": "Tool failed" if redact else str(e)},
                            "is_error": True,
                        }
                    )

            # Add assistant message with tool calls
            assistant_content = []
            if turn.text_response:
                assistant_content.append(
                    {
                        "type": "text",
                        "text": turn.text_response,
                    }
                )
            for tc in turn.tool_calls:
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.input,
                    }
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_content,
                }
            )

            # Add tool results
            user_content = []
            for tr in tool_results:
                user_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tr["tool_use_id"],
                        "content": json.dumps(tr["result"], default=str),
                        "is_error": tr["is_error"],
                    }
                )

            messages.append(
                {
                    "role": "user",
                    "content": user_content,
                }
            )

        # Max turns reached
        return {
            "success": False,
            "error": f"Max turns ({max_turns}) reached without completion",
            "final_response": turn.text_response if turn else None,
            "turns": turn_count,
            "trace": trace,
        }


def create_llm_client_from_profile(
    profile: "ModelProfile", api_key_override: Optional[str] = None
) -> LLMClient:
    """
    Create an LLM client from a resolved model profile.

    Args:
        profile: The resolved ModelProfile to instantiate.
        api_key_override: Optional caller-supplied API key that takes
            precedence over the profile's credential_ref.

    Raises:
        MissingCredentialError: if no credential is available for the profile.
        ValueError: if the profile's provider is not supported.
    """
    from backend.config.model_profiles import create_provider_from_profile

    provider_instance = create_provider_from_profile(
        profile, api_key_override=api_key_override
    )
    return LLMClient(
        provider=profile.provider, model=profile.model, llm_provider=provider_instance
    )


def create_llm_client_from_settings(
    settings: "AgentsSettings", model_profile_id: Optional[str] = None
) -> LLMClient:
    """
    Create an LLM client from agent settings.

    When settings.model_profiles is non-empty, model_profile_id (or the
    application default profile, when None) is resolved and used. Otherwise
    falls back unchanged to the legacy single-provider settings fields.

    Args:
        settings: AgentsSettings instance
        model_profile_id: Optional explicit model profile id (usually an
            agent's AgentConfig.model_profile_id). None inherits the default
            profile when profiles are configured.

    Returns:
        Configured LLMClient

    Raises:
        ValueError: if model_profile_id (or the configured default) does not
            resolve to a usable profile.
    """
    resolution = settings.resolve_model_profile(model_profile_id)
    if resolution.error:
        raise ValueError(resolution.error)
    if resolution.profile is not None:
        return create_llm_client_from_profile(resolution.profile)

    return LLMClient(
        provider=settings.llm_provider,
        model=settings.llm_model,
        openai_api_key=settings.openai_api_key,
        anthropic_api_key=settings.anthropic_api_key,
    )
