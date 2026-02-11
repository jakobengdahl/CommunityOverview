"""
Agent Worker for Event Processing.

Each agent has its own worker that:
- Consumes events from an in-memory queue
- Processes events using the configured LLM and tools
- Logs execution results
"""

import json
import logging
import queue
import threading
import time
from datetime import datetime
from typing import Dict, Any, Optional, List, Callable
from dataclasses import dataclass, field

from .config import AgentConfig, AgentsSettings
from .prompts import build_agent_system_prompt, build_event_user_message
from .llm_client import LLMClient
from .mcp_loader import MCPLoader

logger = logging.getLogger(__name__)


@dataclass
class EventItem:
    """An event queued for processing."""
    event_id: str
    payload: Dict[str, Any]
    enqueued_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ProcessingResult:
    """Result of processing an event."""
    event_id: str
    agent_id: str
    success: bool
    handled: bool  # Whether the agent decided to handle the event
    summary: str
    actions: List[Dict[str, Any]] = field(default_factory=list)
    graph_changes: List[str] = field(default_factory=list)
    error: Optional[str] = None
    processing_time_ms: float = 0
    turns_used: int = 0


class AgentWorker:
    """
    Background worker that processes events for a single agent.

    The worker:
    - Runs in its own thread
    - Consumes events from an in-memory queue
    - Processes each event using the LLM with available tools
    - Logs results and errors
    """

    def __init__(
        self,
        config: AgentConfig,
        settings: AgentsSettings,
        mcp_loader: MCPLoader,
        graph_service: Optional[Any] = None,
        on_result: Optional[Callable[[ProcessingResult], None]] = None,
    ):
        """
        Initialize the agent worker.

        Args:
            config: Agent configuration
            settings: Global agent settings
            mcp_loader: MCP loader for tool access
            graph_service: GraphService for graph operations
            on_result: Optional callback for processing results
        """
        self.config = config
        self.settings = settings
        self.mcp_loader = mcp_loader
        self.graph_service = graph_service
        self.on_result = on_result

        # Event queue
        self._queue: queue.Queue[Optional[EventItem]] = queue.Queue()

        # Worker thread
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()

        # LLM client (created on demand)
        self._llm_client: Optional[LLMClient] = None

        # Statistics
        self.events_processed = 0
        self.events_failed = 0
        self.last_event_at: Optional[datetime] = None

    @property
    def agent_id(self) -> str:
        """Get the agent ID."""
        return self.config.agent_id

    @property
    def is_running(self) -> bool:
        """Check if the worker is running."""
        return self._running

    @property
    def queue_size(self) -> int:
        """Get the current queue size."""
        return self._queue.qsize()

    def start(self) -> None:
        """Start the worker thread."""
        with self._lock:
            if self._running:
                logger.warning(f"Worker {self.agent_id} already running")
                return

            self._running = True
            self._thread = threading.Thread(
                target=self._process_loop,
                name=f"agent-worker-{self.agent_id}",
                daemon=True,
            )
            self._thread.start()
            logger.info(f"Agent worker started: {self.config.name} ({self.agent_id})")

    def stop(self, wait: bool = True, timeout: float = 5.0) -> None:
        """
        Stop the worker thread.

        Args:
            wait: Whether to wait for the thread to finish
            timeout: Maximum time to wait
        """
        with self._lock:
            if not self._running:
                return

            self._running = False
            # Send sentinel to unblock the queue
            self._queue.put(None)

        if wait and self._thread:
            self._thread.join(timeout=timeout)
            logger.info(f"Agent worker stopped: {self.config.name}")

    def enqueue(self, event_payload: Dict[str, Any]) -> None:
        """
        Add an event to the processing queue.

        Args:
            event_payload: The webhook event payload
        """
        event_id = event_payload.get("event_id", "unknown")
        item = EventItem(event_id=event_id, payload=event_payload)
        self._queue.put(item)
        logger.debug(f"Agent {self.agent_id}: Event {event_id} enqueued")

    def reload_config(self, new_config: AgentConfig) -> None:
        """
        Update the agent configuration.

        Args:
            new_config: New configuration to apply
        """
        with self._lock:
            self.config = new_config
            # Reset LLM client to pick up any changes
            self._llm_client = None
            logger.info(f"Agent {self.agent_id}: Configuration reloaded")

    def _process_loop(self) -> None:
        """Main processing loop."""
        while self._running:
            try:
                # Wait for next event with timeout
                item = self._queue.get(timeout=1.0)

                if item is None:
                    # Sentinel value, exit loop
                    break

                self._process_event(item)

            except queue.Empty:
                # Timeout, continue loop
                continue
            except Exception as e:
                logger.error(f"Agent {self.agent_id}: Error in processing loop: {e}")

    def _process_event(self, item: EventItem) -> None:
        """Process a single event."""
        start_time = time.time()
        event_payload = item.payload
        event_id = item.event_id

        logger.info(
            f"Agent {self.config.name}: Processing event {event_id} "
            f"(type: {event_payload.get('event_type', 'unknown')})"
        )

        try:
            # Ensure LLM client is ready
            if not self._llm_client:
                self._llm_client = self._create_llm_client()

            # Get tools for this agent's integrations
            tool_definitions = self.mcp_loader.get_tool_definitions(
                integration_ids=self.config.mcp_integration_ids
            )
            tool_names = [t["name"] for t in tool_definitions]

            # Get schema context from graph service (if available)
            schema = None
            if self.graph_service and hasattr(self.graph_service, "get_schema"):
                try:
                    schema = self.graph_service.get_schema()
                except Exception as e:
                    logger.warning(f"Agent {self.config.name}: Could not load schema: {e}")

            # Build system prompt
            system_prompt = build_agent_system_prompt(
                task_prompt=self.config.prompts.task_prompt,
                available_tools=tool_names,
                schema=schema,
            )

            # Build user message with event
            user_message = build_event_user_message(event_payload)

            # Create tool executor
            tool_executor = self.mcp_loader.create_tool_executor(
                graph_service=self.graph_service,
                agent_id=self.agent_id,
            )

            # Execute with tools
            result = self._llm_client.execute_with_tools(
                system_prompt=system_prompt,
                user_message=user_message,
                tools=tool_definitions,
                tool_executor=tool_executor,
                max_turns=self.settings.max_agent_turns,
            )

            # Log detailed trace (reasoning and tool calls)
            self._log_execution_trace(result)

            # Parse the agent's response
            processing_result = self._parse_agent_response(
                event_id=event_id,
                llm_result=result,
                start_time=start_time,
            )

            self.events_processed += 1
            self.last_event_at = datetime.utcnow()

            logger.info(
                f"Agent {self.config.name}: Event {event_id} processed - "
                f"handled={processing_result.handled}, "
                f"actions={len(processing_result.actions)}, "
                f"time={processing_result.processing_time_ms:.0f}ms"
            )

        except Exception as e:
            logger.error(f"Agent {self.config.name}: Event {event_id} failed: {e}")
            self.events_failed += 1

            processing_result = ProcessingResult(
                event_id=event_id,
                agent_id=self.agent_id,
                success=False,
                handled=False,
                summary=f"Processing failed: {e}",
                error=str(e),
                processing_time_ms=(time.time() - start_time) * 1000,
            )

        # Notify callback if set
        if self.on_result:
            try:
                self.on_result(processing_result)
            except Exception as e:
                logger.error(f"Result callback failed: {e}")

    def _log_execution_trace(self, result: Dict[str, Any]) -> None:
        """Log the LLM execution trace with reasoning and tool calls."""
        trace = result.get("trace", [])
        agent_name = self.config.name
        for turn in trace:
            turn_num = turn.get("turn", "?")
            text = turn.get("text_response")
            tool_calls = turn.get("tool_calls", [])

            if text:
                # Truncate long reasoning for console readability
                display_text = text[:500] + "..." if len(text) > 500 else text
                logger.info(f"Agent {agent_name} [turn {turn_num}] reasoning: {display_text}")

            for tc in tool_calls:
                tc_name = tc.get("name", "?")
                tc_input = tc.get("input", {})
                input_summary = json.dumps(tc_input, default=str, ensure_ascii=False)
                if len(input_summary) > 300:
                    input_summary = input_summary[:300] + "..."
                logger.info(f"Agent {agent_name} [turn {turn_num}] tool call: {tc_name}({input_summary})")

        final = result.get("final_response")
        if final:
            display_final = final[:500] + "..." if len(final) > 500 else final
            logger.info(f"Agent {agent_name} final response: {display_final}")

        if not result.get("success"):
            logger.warning(f"Agent {agent_name} execution failed: {result.get('error', 'unknown')}")

    def _create_llm_client(self) -> LLMClient:
        """Create the LLM client."""
        return LLMClient(
            provider=self.settings.llm_provider,
            model=self.settings.llm_model,
            openai_api_key=self.settings.openai_api_key,
            anthropic_api_key=self.settings.anthropic_api_key,
        )

    def _parse_agent_response(
        self,
        event_id: str,
        llm_result: Dict[str, Any],
        start_time: float,
    ) -> ProcessingResult:
        """Parse the LLM result into a ProcessingResult."""
        processing_time_ms = (time.time() - start_time) * 1000

        if not llm_result.get("success"):
            return ProcessingResult(
                event_id=event_id,
                agent_id=self.agent_id,
                success=False,
                handled=False,
                summary=llm_result.get("error", "Unknown error"),
                error=llm_result.get("error"),
                processing_time_ms=processing_time_ms,
                turns_used=llm_result.get("turns", 0),
            )

        # Try to parse the final response as JSON
        final_response = llm_result.get("final_response", "")
        handled = True
        summary = final_response
        actions = []
        graph_changes = []

        # Try to extract structured response
        try:
            # Look for JSON in the response
            if "{" in final_response:
                # Find JSON block
                start_idx = final_response.find("{")
                end_idx = final_response.rfind("}") + 1
                json_str = final_response[start_idx:end_idx]
                parsed = json.loads(json_str)

                handled = parsed.get("handled", True)
                summary = parsed.get("summary", final_response)
                actions = parsed.get("actions", [])
                graph_changes = parsed.get("graph_changes", [])

        except (json.JSONDecodeError, ValueError):
            # Not valid JSON, use raw response
            pass

        # Extract actions from trace
        trace = llm_result.get("trace", [])
        for turn in trace:
            for tc in turn.get("tool_calls", []):
                actions.append({
                    "tool": tc.get("name"),
                    "input": tc.get("input"),
                })

        return ProcessingResult(
            event_id=event_id,
            agent_id=self.agent_id,
            success=True,
            handled=handled,
            summary=summary,
            actions=actions,
            graph_changes=graph_changes,
            processing_time_ms=processing_time_ms,
            turns_used=llm_result.get("turns", 0),
        )

    def get_status(self) -> Dict[str, Any]:
        """Get worker status information."""
        return {
            "agent_id": self.agent_id,
            "agent_name": self.config.name,
            "enabled": self.config.enabled,
            "running": self._running,
            "queue_size": self.queue_size,
            "events_processed": self.events_processed,
            "events_failed": self.events_failed,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "mcp_integrations": self.config.mcp_integration_ids,
        }
