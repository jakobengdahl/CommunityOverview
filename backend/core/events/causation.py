"""Server-owned causation context for synchronous agent graph writes."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Optional

from .models import EventContext

MAX_AGENT_CAUSATION_DEPTH = 8
AGENT_EVENT_RATE_LIMIT = 30
AGENT_EVENT_RATE_WINDOW_SECONDS = 60

_agent_mutation_context: ContextVar[Optional[EventContext]] = ContextVar(
    "agent_mutation_context", default=None
)


def current_agent_mutation_context() -> Optional[EventContext]:
    return _agent_mutation_context.get()


@contextmanager
def agent_mutation_context(
    agent_id: str, event_payload: Dict[str, Any]
) -> Iterator[None]:
    origin = event_payload.get("origin") or {}
    depth = origin.get("event_causation_depth", 0)
    if type(depth) is not int or not 0 <= depth < MAX_AGENT_CAUSATION_DEPTH:
        raise ValueError("Agent causation depth limit exceeded or invalid")
    token = _agent_mutation_context.set(
        EventContext(
            event_origin=f"agent:{agent_id}",
            event_correlation_id=origin.get("event_correlation_id")
            or event_payload.get("event_id"),
            event_causation_depth=depth + 1,
        )
    )
    try:
        yield
    finally:
        _agent_mutation_context.reset(token)
