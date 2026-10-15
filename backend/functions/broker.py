"""
Tool broker for user functions (docs/USER_FUNCTION_CONTRACT.md §6.3–§7, §9).

The broker answers one function run's tool calls. It is the only path from
function code to the graph, so every rule that keeps a run inside its scope is
enforced here, per call:

- the tool must exist in this contract version (§6.3);
- the function must declare the tool's grant and the operator must still have
  it enabled — checked on every call so that revocation takes effect at the
  run's next call (§7.1);
- arguments are allow-listed per tool; anything that would change scope (graph,
  session, origin, correlation) is refused (§6.3);
- the invoking actor's access is evaluated through the authorization hook, with
  that actor's request context re-bound for the call (§7.2);
- writes carry ``function:<id>@<version>`` and the run's correlation id (§9);
- the tool-call budget and the per-message size limit apply (§8).

The broker is transport-agnostic: ``handle_request`` takes and returns one
JSON-RPC message as a dict. ``protocol.run_session`` drives it over a process's
standard streams.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Mapping

from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GRAPH_ACTION_READ,
    use_request_authorization,
)
from backend.service import access
from backend.service.attachments import FEDERATED_NODE_PREFIX
from backend.service.mutations import _FEDERATION_BOOKKEEPING_METADATA_KEYS

logger = logging.getLogger(__name__)

GRANT_GRAPH_READ = "graph:read"
GRANT_GRAPH_WRITE = "graph:write"
KNOWN_GRANTS = frozenset({GRANT_GRAPH_READ, GRANT_GRAPH_WRITE})

# JSON-RPC error code for every refusal the broker makes (§6.4).
BROKER_ERROR_CODE = -32000

# §6.4 data.code values
ERR_UNKNOWN_TOOL = "unknown_tool"
ERR_GRANT_DENIED = "grant_denied"
ERR_ACCESS_DENIED = "access_denied"
ERR_INVALID_ARGUMENTS = "invalid_arguments"
ERR_LIMIT_EXCEEDED = "limit_exceeded"
ERR_TOOL_FAILED = "tool_failed"
ERR_CONCURRENT_CALL = "concurrent_call"

# Contract maximums (§8).
MAX_TOOL_CALLS = 200
MAX_MESSAGE_BYTES = 1024 * 1024

# Arguments a function may never pass: scope is fixed by the host (§3, §6.3).
SCOPE_ARGUMENTS = frozenset(
    {
        "graph_id",
        "session_id",
        "visualization_session_id",
        "event_origin",
        "event_session_id",
        "event_correlation_id",
        "federation_depth",
        "action",
    }
)

_MAX_ERROR_MESSAGE_CHARS = 500


@dataclass(frozen=True)
class ToolSpec:
    method: str  # GraphService method, also the authorization target
    grant: str
    action: str  # GRAPH_ACTION_READ or GRAPH_ACTION_MUTATE
    required: FrozenSet[str]
    optional: FrozenSet[str] = frozenset()

    @property
    def allowed(self) -> FrozenSet[str]:
        return self.required | self.optional


TOOLS: Dict[str, ToolSpec] = {
    "graph.search": ToolSpec(
        "search_graph",
        GRANT_GRAPH_READ,
        GRAPH_ACTION_READ,
        required=frozenset({"query"}),
        optional=frozenset(
            {
                "node_types",
                "limit",
                "tags_any",
                "tags_all",
                "tags_none",
                "metadata_filters",
                "include_archived",
                "semantic",
                "match_mode",
            }
        ),
    ),
    "graph.get_node": ToolSpec(
        "get_node_details",
        GRANT_GRAPH_READ,
        GRAPH_ACTION_READ,
        required=frozenset({"node_id"}),
    ),
    "graph.get_related": ToolSpec(
        "get_related_nodes",
        GRANT_GRAPH_READ,
        GRAPH_ACTION_READ,
        required=frozenset({"node_id"}),
        optional=frozenset({"relationship_types", "depth", "include_archived"}),
    ),
    "graph.find_similar": ToolSpec(
        "find_similar_nodes",
        GRANT_GRAPH_READ,
        GRAPH_ACTION_READ,
        required=frozenset({"name"}),
        optional=frozenset({"node_type", "threshold", "limit"}),
    ),
    "graph.add": ToolSpec(
        "add_nodes",
        GRANT_GRAPH_WRITE,
        GRAPH_ACTION_MUTATE,
        required=frozenset({"nodes"}),
        optional=frozenset({"edges", "expect_absent"}),
    ),
    "graph.update_node": ToolSpec(
        "update_node",
        GRANT_GRAPH_WRITE,
        GRAPH_ACTION_MUTATE,
        required=frozenset({"node_id", "updates"}),
        optional=frozenset(
            {"metadata_merge", "expected_updated_at", "expected_revision"}
        ),
    ),
}


def function_origin(function_id: str, function_version: str) -> str:
    """The event origin every write of a run carries (§9)."""
    return f"function:{function_id}@{function_version}"


@dataclass(frozen=True)
class BrokerContext:
    """Everything the host fixes for one run before it starts."""

    run_id: str
    function_id: str
    function_version: str
    declared_grants: FrozenSet[str]
    correlation_id: str
    # The invoking actor's request authorization inputs, as captured by
    # backend.runtime.authorization.get_current_request_authorization_inputs()
    # when the invocation was accepted. Re-bound for every tool call.
    authorization_inputs: Mapping[str, Any]
    # Live operator enablement; consulted on every call so revocation applies
    # to a run in progress (§7.1). Required: a default would fail open.
    grant_enabled: Callable[[str], bool]
    max_tool_calls: int = MAX_TOOL_CALLS
    max_message_bytes: int = MAX_MESSAGE_BYTES


@dataclass
class ToolCallRecord:
    """One entry of the run record's tool_calls (§11): no arguments or results."""

    tool: str
    outcome: str  # "ok" or a §6.4 data.code
    duration_ms: int


class BrokerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def error_response(request_id: Any, code: str, message: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": BROKER_ERROR_CODE,
            "message": message,
            "data": {"code": code},
        },
    }


def wire_size(message: Any) -> int:
    """Bytes the message takes on the wire, as protocol.py writes it."""
    return len(json.dumps(message, separators=(",", ":")).encode("utf-8")) + 1


def _sanitize(message: Any) -> str:
    text = str(message or "").strip() or "The operation failed."
    return text[:_MAX_ERROR_MESSAGE_CHARS]


class FunctionBroker:
    """Answers the tool calls of one function run."""

    def __init__(self, graph_service: Any, context: BrokerContext):
        self._service = graph_service
        self._context = context
        self._calls_made = 0
        self.tool_calls: List[ToolCallRecord] = []
        # Requests beyond the budget are counted, not recorded, so a function
        # cannot grow host memory or the run record by repeating refused calls.
        self.calls_over_budget = 0

    @property
    def context(self) -> BrokerContext:
        return self._context

    def handle_request(self, message: Mapping[str, Any]) -> Dict[str, Any]:
        """Answer one JSON-RPC ``tool`` request; never raises."""
        request_id = message.get("id")
        params = message.get("params")
        name = params.get("name") if isinstance(params, Mapping) else None
        started = time.monotonic()
        self._calls_made += 1
        try:
            result = self._dispatch(params)
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            if wire_size(response) > self._context.max_message_bytes:
                raise BrokerError(
                    ERR_TOOL_FAILED, "The response exceeds the message size limit."
                )
        except BrokerError as exc:
            self._record(name, exc.code, started)
            return error_response(request_id, exc.code, exc.message)
        except Exception:
            # A failing hook or enablement check must still end in an answer,
            # never in an exception out of the session.
            logger.exception(
                "Function run %s: tool call failed in the broker", self._context.run_id
            )
            self._record(name, ERR_TOOL_FAILED, started)
            return error_response(request_id, ERR_TOOL_FAILED, "The operation failed.")
        self._record(name, "ok", started)
        return response

    def refuse(
        self, message: Mapping[str, Any], code: str, text: str
    ) -> Dict[str, Any]:
        """Answer a request the protocol layer refuses before dispatch."""
        params = message.get("params")
        name = params.get("name") if isinstance(params, Mapping) else None
        self._calls_made += 1
        self._record(name, code, time.monotonic())
        return error_response(message.get("id"), code, text)

    # -- internals ---------------------------------------------------------

    def _record(self, name: Any, outcome: str, started: float) -> None:
        if self._calls_made > self._context.max_tool_calls:
            self.calls_over_budget += 1
            return
        self.tool_calls.append(
            ToolCallRecord(
                # Only names the contract defines: the function chose this text.
                tool=name if isinstance(name, str) and name in TOOLS else "",
                outcome=outcome,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )

    def _dispatch(self, params: Any) -> Any:
        ctx = self._context
        if self._calls_made > ctx.max_tool_calls:
            raise BrokerError(
                ERR_LIMIT_EXCEEDED, "The run has used its tool-call budget."
            )

        if not isinstance(params, Mapping) or not isinstance(params.get("name"), str):
            raise BrokerError(ERR_INVALID_ARGUMENTS, "params.name must be a string.")
        name = params["name"]
        arguments = params.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise BrokerError(
                ERR_INVALID_ARGUMENTS, "params.arguments must be an object."
            )

        spec = TOOLS.get(name)
        if spec is None:
            raise BrokerError(ERR_UNKNOWN_TOOL, f"Unknown tool: {name[:100]}")

        if spec.grant not in ctx.declared_grants or not ctx.grant_enabled(spec.grant):
            raise BrokerError(
                ERR_GRANT_DENIED, f"The function's grants do not cover {name}."
            )

        self._validate_arguments(spec, arguments)
        if spec.method == "add_nodes":
            self._refuse_reference_endpoints(arguments)
        call_args = dict(arguments)
        if spec.action == GRAPH_ACTION_MUTATE:
            call_args["event_origin"] = function_origin(
                ctx.function_id, ctx.function_version
            )
            call_args["event_correlation_id"] = ctx.correlation_id
        if spec.method == "add_nodes":
            call_args.setdefault("edges", [])

        with use_request_authorization(
            **_authorization_kwargs(ctx.authorization_inputs)
        ):
            decision = access.evaluate_graph_access(
                self._service.authorization_hook,
                action=spec.action,
                target=spec.method,
            )
            if not decision.allowed:
                raise BrokerError(
                    ERR_ACCESS_DENIED,
                    _sanitize(decision.reason or "Graph access denied."),
                )
            try:
                result = getattr(self._service, spec.method)(**call_args)
            except Exception:
                # Never forward internal exception text to function code.
                logger.exception("Function run %s: tool %s raised", ctx.run_id, name)
                raise BrokerError(ERR_TOOL_FAILED, "The operation failed.")

        if spec.method == "find_similar_nodes":
            # The service applies no visibility narrowing to similarity search;
            # filter here so a function never sees more than the actor may.
            result = _narrow_similar_nodes(result, decision.graph_access)

        if isinstance(result, Mapping):
            if result.get("error_code") == ERR_ACCESS_DENIED:
                raise BrokerError(ERR_ACCESS_DENIED, _sanitize(result.get("message")))
            if result.get("success") is False:
                raise BrokerError(
                    ERR_TOOL_FAILED,
                    _sanitize(result.get("error") or result.get("message")),
                )

        # Round-trip so the function receives plain JSON (dates become strings);
        # handle_request then measures the whole response envelope.
        return json.loads(json.dumps(result, default=str))

    def _refuse_reference_endpoints(self, arguments: Mapping[str, Any]) -> None:
        """Refuse edge endpoints that name a federated reference node.

        Storage resolves an endpoint that is not a node id by name, across all
        nodes, so a name can land on a host-managed reference node. Names of
        nodes created in the same call are fine: storage resolves those to the
        new nodes. Any other name shared with a reference node is refused,
        whichever node the name would resolve to.
        """
        edges = arguments.get("edges") or []
        names = {
            edge.get(end)
            for edge in edges
            if isinstance(edge, Mapping)
            for end in ("source", "target")
            if isinstance(edge.get(end), str)
        }
        if not names:
            return
        nodes = self._service.storage.nodes
        new_names = {
            node.get("name")
            for node in arguments.get("nodes") or []
            if isinstance(node, Mapping)
        }
        candidates = {n for n in names if n not in nodes and n not in new_names}
        if not candidates:
            return
        for node_id, node in list(nodes.items()):
            if node_id.startswith(FEDERATED_NODE_PREFIX) and node.name in candidates:
                raise BrokerError(
                    ERR_INVALID_ARGUMENTS,
                    "An edge endpoint names a federated reference node; "
                    "use the id of a local node.",
                )

    @staticmethod
    def _validate_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> None:
        FunctionBroker._validate_keys(spec, arguments)
        for key, cap in _ARGUMENT_CAPS.items():
            value = arguments.get(key)
            if key in arguments and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= cap
            ):
                raise BrokerError(
                    ERR_INVALID_ARGUMENTS, f"{key} must be an integer from 0 to {cap}."
                )
        if spec.method == "add_nodes":
            nodes = arguments.get("nodes")
            if not isinstance(nodes, list):
                raise BrokerError(ERR_INVALID_ARGUMENTS, "nodes must be a list.")
            for node in nodes:
                _refuse_graph_scope(node)
                if isinstance(node, Mapping):
                    _refuse_reserved_id(node.get("id"))
            edges = arguments.get("edges", [])
            if not isinstance(edges, list):
                raise BrokerError(ERR_INVALID_ARGUMENTS, "edges must be a list.")
            for edge in edges:
                _refuse_graph_scope(edge)
                if isinstance(edge, Mapping):
                    for end in ("source", "target"):
                        _refuse_reserved_id(edge.get(end))
        if spec.method == "update_node":
            _refuse_reserved_id(arguments.get("node_id"))
            updates = arguments.get("updates")
            _refuse_graph_scope(updates)
            if (
                isinstance(updates, Mapping)
                and "metadata" in updates
                and arguments.get("metadata_merge") is not True
            ):
                # Replacing metadata wholesale would drop the host-set scope
                # keys this guard exists to protect.
                raise BrokerError(
                    ERR_INVALID_ARGUMENTS,
                    "Updating metadata requires metadata_merge: true.",
                )

    @staticmethod
    def _validate_keys(spec: ToolSpec, arguments: Mapping[str, Any]) -> None:
        keys = set(arguments)
        scope = keys & SCOPE_ARGUMENTS
        if scope:
            raise BrokerError(
                ERR_INVALID_ARGUMENTS,
                "Scope is fixed by the host; not allowed: " + ", ".join(sorted(scope)),
            )
        unknown = keys - spec.allowed
        if unknown:
            raise BrokerError(
                ERR_INVALID_ARGUMENTS,
                "Unknown arguments: " + ", ".join(sorted(unknown))[:200],
            )
        missing = spec.required - keys
        if missing:
            raise BrokerError(
                ERR_INVALID_ARGUMENTS,
                "Missing arguments: " + ", ".join(sorted(missing)),
            )


# Bounds on work a single call can ask of the host (the session cannot be
# interrupted while a service call runs).
_ARGUMENT_CAPS = {"limit": 500, "depth": 3}

# Keys through which the storage and federation layers record a node's or
# edge's graph scope and provenance. A function may neither set nor clear them,
# at the top level (folded into metadata on add) or inside metadata.
_SCOPE_KEYS = frozenset(_FEDERATION_BOOKKEEPING_METADATA_KEYS) | {
    "is_federated_reference",
    "read_only",
    "adopted_from",
    "is_federated_lineage",
}


def _refuse_reserved_id(node_id: Any) -> None:
    """Federated reference nodes are host-managed mirrors of remote nodes."""
    if isinstance(node_id, str) and node_id.startswith(FEDERATED_NODE_PREFIX):
        raise BrokerError(
            ERR_INVALID_ARGUMENTS,
            f"Node ids starting with {FEDERATED_NODE_PREFIX} are reserved.",
        )


def _refuse_graph_scope(payload: Any) -> None:
    if not isinstance(payload, Mapping):
        return
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, Mapping):
        # The service coerces some non-mapping shapes (a list of pairs) into a
        # dict after this check would have run; accept only a mapping.
        raise BrokerError(ERR_INVALID_ARGUMENTS, "metadata must be an object.")
    found = set(payload) & _SCOPE_KEYS
    if isinstance(metadata, Mapping):
        found |= set(metadata) & _SCOPE_KEYS
    if found:
        raise BrokerError(
            ERR_INVALID_ARGUMENTS,
            "Scope is fixed by the host; not allowed: " + ", ".join(sorted(found)),
        )


_AUTHORIZATION_KEYS = (
    "headers",
    "actor_id",
    "actor_type",
    "auth_source",
    "workspace_id",
    "workspace_kind",
    "graph_id",
)


def _authorization_kwargs(inputs: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: inputs.get(key) for key in _AUTHORIZATION_KEYS}


def _narrow_similar_nodes(result: Any, graph_access: Any) -> Any:
    if not isinstance(result, Mapping) or not isinstance(
        result.get("similar_nodes"), list
    ):
        return result
    visible = []
    for item in result["similar_nodes"]:
        node = item.get("node") if isinstance(item, Mapping) else None
        metadata = node.get("metadata") if isinstance(node, Mapping) else None
        graph_id = str((metadata or {}).get("origin_graph_id") or "").strip()
        if graph_access.matches(graph_id=graph_id):
            visible.append(item)
    narrowed = dict(result)
    narrowed["similar_nodes"] = visible
    narrowed["total"] = len(visible)
    return narrowed
