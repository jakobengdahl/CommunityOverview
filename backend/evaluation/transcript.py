"""
Run transcripts: what a single evaluation case actually did.

Everything the scorers need is observable at the LLMProvider boundary, so the
harness records there instead of instrumenting the assistant:

- the tool calls the model asked for are the ``tool_use`` blocks the provider
  *returns*;
- the tool results it saw are the ``tool_result`` blocks the provider *receives*
  on the following turn (backend/ui/chat_logic.py re-sends the growing history);
- latency and token usage are measured around, and read off, each call.

RecordingProvider therefore wraps any LLMProvider — the real one built from a
model profile, or a mock in the tests — and the resulting RunTranscript is a
plain data structure the scorers read. No credential passes through here: the
key lives inside the wrapped provider's SDK client, and the transcript stores
neither the provider instance nor its raw responses.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.llm.llm_providers import LLMProvider, LLMResponse


@dataclass
class TokenUsage:
    """Prompt/completion token counts for one provider call."""

    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None

    @property
    def total_tokens(self) -> Optional[int]:
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)

    @property
    def reported(self) -> bool:
        """True when the provider reported at least one of the two counts."""
        return self.prompt_tokens is not None or self.completion_tokens is not None


def extract_usage(raw_response: Any) -> TokenUsage:
    """
    Read token counts off a provider's raw response, duck-typed.

    Anthropic reports ``usage.input_tokens``/``usage.output_tokens``; OpenAI
    reports ``usage.prompt_tokens``/``usage.completion_tokens``. An
    OpenAI-compatible endpoint is free to report neither, which is why this
    returns an unreported TokenUsage rather than raising — a self-hosted model
    that omits usage must show up as "not reported", not as zero tokens.
    """
    usage = getattr(raw_response, "usage", None)
    if usage is None and isinstance(raw_response, dict):
        usage = raw_response.get("usage")

    def _read(*names: str) -> Optional[int]:
        for name in names:
            value = (
                usage.get(name)
                if isinstance(usage, dict)
                else getattr(usage, name, None)
            )
            if isinstance(value, int):
                return value
        return None

    if usage is None:
        return TokenUsage()
    return TokenUsage(
        prompt_tokens=_read("prompt_tokens", "input_tokens"),
        completion_tokens=_read("completion_tokens", "output_tokens"),
    )


@dataclass
class ToolCall:
    """One tool the model asked for, in the order it was requested."""

    name: str
    input: Dict[str, Any]
    tool_use_id: Optional[str]
    turn: int
    """0-based index of the provider call that requested this tool."""


@dataclass
class ProviderCall:
    """One create_completion round trip."""

    turn: int
    latency_ms: float
    stop_reason: Optional[str]
    tools_advertised: List[str]
    usage: TokenUsage = field(default_factory=TokenUsage)
    error: Optional[str] = None
    system_prompt: str = ""
    """Kept so skill-injection assertions can be made; excluded from reports."""


@dataclass
class RunTranscript:
    """The observable record of one case run."""

    provider_calls: List[ProviderCall] = field(default_factory=list)
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_results: Dict[str, Any] = field(default_factory=dict)
    """tool_use_id -> decoded tool result payload, as the model received it."""
    final_text: str = ""
    final_graph: Dict[str, Any] = field(default_factory=dict)
    """{"nodes": [...], "edges": [...]} read back after the run."""
    run_error: Optional[str] = None

    @property
    def total_latency_ms(self) -> float:
        return sum(call.latency_ms for call in self.provider_calls)

    @property
    def total_usage(self) -> TokenUsage:
        """Summed usage, unreported when no call reported any count."""
        reported = [c.usage for c in self.provider_calls if c.usage.reported]
        if not reported:
            return TokenUsage()
        return TokenUsage(
            prompt_tokens=sum(u.prompt_tokens or 0 for u in reported),
            completion_tokens=sum(u.completion_tokens or 0 for u in reported),
        )

    @property
    def tool_call_names(self) -> List[str]:
        return [call.name for call in self.tool_calls]

    def result_for(self, tool_use_id: Optional[str]) -> Any:
        return self.tool_results.get(tool_use_id)

    def results_after_turn(self, turn: int) -> List[Any]:
        """Decoded results of every tool call requested after ``turn``."""
        return [
            self.tool_results[c.tool_use_id]
            for c in self.tool_calls
            if c.turn > turn and c.tool_use_id in self.tool_results
        ]


def _decode_tool_result(block: Dict[str, Any]) -> Any:
    """Decode a tool_result block's content, which chat_logic sends as JSON text."""
    content = block.get("content")
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (ValueError, TypeError):
            return content
    return content


class RecordingProvider(LLMProvider):
    """
    An LLMProvider that delegates to another and records what crossed the wire.

    Wrapping rather than subclassing the concrete providers keeps the harness
    provider-agnostic: anything satisfying the LLMProvider contract can be
    measured, including the mock providers used by the tests.
    """

    def __init__(self, inner: LLMProvider):
        self._inner = inner
        self.transcript = RunTranscript()

    @property
    def inner(self) -> LLMProvider:
        return self._inner

    def create_completion(
        self,
        messages: List[Dict],
        system_prompt: str,
        tools: List[Dict],
        max_tokens: int = 4096,
    ) -> LLMResponse:
        turn = len(self.transcript.provider_calls)
        self._harvest_tool_results(messages)

        started = time.perf_counter()
        try:
            response = self._inner.create_completion(
                messages=messages,
                system_prompt=system_prompt,
                tools=tools,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            self.transcript.provider_calls.append(
                ProviderCall(
                    turn=turn,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    stop_reason=None,
                    tools_advertised=[t.get("name", "") for t in tools or []],
                    error=f"{type(exc).__name__}: {exc}",
                    system_prompt=system_prompt,
                )
            )
            raise

        latency_ms = (time.perf_counter() - started) * 1000.0
        self.transcript.provider_calls.append(
            ProviderCall(
                turn=turn,
                latency_ms=latency_ms,
                stop_reason=response.stop_reason,
                tools_advertised=[t.get("name", "") for t in tools or []],
                usage=extract_usage(response.raw_response),
                system_prompt=system_prompt,
            )
        )

        for block in response.content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                self.transcript.tool_calls.append(
                    ToolCall(
                        name=block.get("name", ""),
                        input=block.get("input") or {},
                        tool_use_id=block.get("id"),
                        turn=turn,
                    )
                )
        return response

    def format_tool_definitions(self, tools: List[Dict]) -> Any:
        return self._inner.format_tool_definitions(tools)

    def _harvest_tool_results(self, messages: List[Dict]) -> None:
        """Record tool_result blocks the assistant is feeding back to the model."""
        for message in messages or []:
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_use_id = block.get("tool_use_id")
                if tool_use_id and tool_use_id not in self.transcript.tool_results:
                    self.transcript.tool_results[tool_use_id] = _decode_tool_result(
                        block
                    )
