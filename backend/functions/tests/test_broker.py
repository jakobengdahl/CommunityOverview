"""Invariants of the user-function tool broker (USER_FUNCTION_CONTRACT §6.3–§7, §9)."""

from typing import Any, Dict, List

import pytest

from backend.functions.broker import (
    BROKER_ERROR_CODE,
    GRANT_GRAPH_READ,
    GRANT_GRAPH_WRITE,
    BrokerContext,
    FunctionBroker,
    function_origin,
)
from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GraphAccessNarrowing,
    GraphAuthorizationContext,
    GraphAuthorizationDecision,
)
from backend.service import GraphService

INVOKER = {"actor_id": "user-42", "actor_type": "human", "auth_source": "test"}


class RecordingHook:
    """Allows everything and records the actor each decision saw."""

    def __init__(self, deny_mutate: bool = False, narrowing=None):
        self.deny_mutate = deny_mutate
        self.narrowing = narrowing
        self.contexts: List[GraphAuthorizationContext] = []

    def evaluate(self, context):
        self.contexts.append(context)
        if self.deny_mutate and context.action == GRAPH_ACTION_MUTATE:
            return GraphAuthorizationDecision(
                allowed=False, reason="read-only actor", mode="custom", source="test"
            )
        kwargs = {"graph_access": self.narrowing} if self.narrowing else {}
        return GraphAuthorizationDecision(
            allowed=True, mode="custom", source="test", **kwargs
        )


class SpyService(GraphService):
    """Records the keyword arguments each graph method received."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: List[Dict[str, Any]] = []

    def add_nodes(self, nodes, edges, **kwargs):
        self.calls.append({"method": "add_nodes", **kwargs})
        return super().add_nodes(nodes, edges, **kwargs)

    def update_node(self, node_id, updates, **kwargs):
        self.calls.append({"method": "update_node", **kwargs})
        return super().update_node(node_id, updates, **kwargs)

    def search_graph(self, query, **kwargs):
        self.calls.append({"method": "search_graph", **kwargs})
        return super().search_graph(query, **kwargs)


def make_broker(service, grants=(GRANT_GRAPH_READ, GRANT_GRAPH_WRITE), **overrides):
    context = BrokerContext(
        run_id="job-test",
        function_id="recompute",
        function_version="1.2.0",
        declared_grants=frozenset(grants),
        correlation_id="corr-1",
        authorization_inputs=INVOKER,
        **overrides,
    )
    return FunctionBroker(service, context)


def call(broker, name, arguments=None, request_id=1):
    return broker.handle_request(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tool",
            "params": {"name": name, "arguments": arguments or {}},
        }
    )


def error_code(response):
    assert "error" in response, response
    assert response["error"]["code"] == BROKER_ERROR_CODE
    return response["error"]["data"]["code"]


@pytest.fixture
def hook():
    return RecordingHook()


@pytest.fixture
def service(storage, hook):
    return SpyService(storage, authorization_hook=hook)


def add_node(broker, name="Threshold A", **extra):
    response = call(
        broker, "graph.add", {"nodes": [{"name": name, "type": "Actor", **extra}]}
    )
    assert "result" in response, response
    return response["result"]["added_node_ids"][0]


def test_read_tool_returns_the_service_result_with_the_request_id(service):
    broker = make_broker(service)
    add_node(broker, "Threshold A")
    response = call(broker, "graph.search", {"query": "Threshold"}, request_id="r7")
    assert response["id"] == "r7"
    names = [n["name"] for n in response["result"]["nodes"]]
    assert "Threshold A" in names


def test_unknown_tool_is_refused(service):
    assert error_code(call(make_broker(service), "graph.delete")) == "unknown_tool"


def test_tool_outside_declared_grants_is_refused_without_reaching_the_service(service):
    broker = make_broker(service, grants=(GRANT_GRAPH_READ,))
    response = call(broker, "graph.add", {"nodes": [{"name": "X", "type": "Actor"}]})
    assert error_code(response) == "grant_denied"
    assert service.calls == []


def test_revoking_a_grant_takes_effect_at_the_next_call_of_a_running_run(service):
    enabled = {GRANT_GRAPH_READ, GRANT_GRAPH_WRITE}
    broker = make_broker(service, grant_enabled=lambda grant: grant in enabled)
    add_node(broker, "Before revocation")
    enabled.discard(GRANT_GRAPH_WRITE)
    response = call(
        broker, "graph.add", {"nodes": [{"name": "After", "type": "Actor"}]}
    )
    assert error_code(response) == "grant_denied"
    assert "result" in call(broker, "graph.search", {"query": "Before"})


@pytest.mark.parametrize(
    "scope_argument",
    [
        "graph_id",
        "session_id",
        "event_origin",
        "event_session_id",
        "event_correlation_id",
        "federation_depth",
        "action",
    ],
)
def test_scope_changing_arguments_are_refused(service, scope_argument):
    broker = make_broker(service)
    response = call(broker, "graph.search", {"query": "x", scope_argument: "other"})
    assert error_code(response) == "invalid_arguments"
    assert "fixed by the host" in response["error"]["message"]
    assert service.calls == []


def test_unknown_and_missing_arguments_are_refused(service):
    broker = make_broker(service)
    assert error_code(call(broker, "graph.search", {"query": "x", "bogus": 1})) == (
        "invalid_arguments"
    )
    assert error_code(call(broker, "graph.update_node", {"node_id": "n"})) == (
        "invalid_arguments"
    )
    assert error_code(
        broker.handle_request(
            {"jsonrpc": "2.0", "id": 3, "method": "tool", "params": {"name": 5}}
        )
    ) == ("invalid_arguments")


def test_writes_carry_the_function_origin_and_run_correlation_id(service, storage):
    broker = make_broker(service)
    node_id = add_node(broker, "Origin check")
    response = call(
        broker,
        "graph.update_node",
        {"node_id": node_id, "updates": {"description": "changed"}},
    )
    assert "result" in response, response
    writes = [c for c in service.calls if c["method"] in ("add_nodes", "update_node")]
    assert len(writes) == 2
    for write in writes:
        assert write["event_origin"] == function_origin("recompute", "1.2.0")
        assert write["event_origin"] == "function:recompute@1.2.0"
        assert write["event_correlation_id"] == "corr-1"
    assert storage.get_node(node_id).description == "changed"


def test_reads_carry_no_origin_or_correlation(service):
    broker = make_broker(service)
    call(broker, "graph.search", {"query": "x"})
    (search,) = [c for c in service.calls if c["method"] == "search_graph"]
    assert "event_origin" not in search
    assert "event_correlation_id" not in search


def test_each_call_is_authorized_as_the_invoking_actor(service, hook):
    broker = make_broker(service)
    call(broker, "graph.search", {"query": "x"})
    assert hook.contexts, "the hook was never consulted"
    assert {c.actor["actor_id"] for c in hook.contexts} == {"user-42"}


def test_a_function_never_writes_more_than_its_invoking_actor_may(storage):
    hook = RecordingHook(deny_mutate=True)
    service = SpyService(storage, authorization_hook=hook)
    broker = make_broker(service)
    response = call(
        broker, "graph.add", {"nodes": [{"name": "Denied", "type": "Actor"}]}
    )
    assert error_code(response) == "access_denied"
    assert service.calls == []
    assert storage.search_nodes("Denied") == []


def test_similarity_search_is_narrowed_to_graphs_the_actor_may_see(storage):
    narrowing = GraphAccessNarrowing(
        enabled=True, allow_local_graph=True, include_graph_ids=()
    )
    hook = RecordingHook(narrowing=narrowing)
    service = SpyService(storage, authorization_hook=hook)
    broker = make_broker(service)
    add_node(broker, "Similar Local")
    add_node(broker, "Similar Remote", metadata={"origin_graph_id": "other-graph"})
    response = call(broker, "graph.find_similar", {"name": "Similar", "threshold": 0.1})
    names = [item["node"]["name"] for item in response["result"]["similar_nodes"]]
    assert "Similar Local" in names
    assert "Similar Remote" not in names
    assert response["result"]["total"] == len(names)


def test_the_tool_call_budget_is_enforced(service):
    broker = make_broker(service, max_tool_calls=2)
    assert "result" in call(broker, "graph.search", {"query": "a"})
    assert "result" in call(broker, "graph.search", {"query": "b"})
    assert error_code(call(broker, "graph.search", {"query": "c"})) == "limit_exceeded"


def test_an_oversized_response_becomes_tool_failed(service):
    broker = make_broker(service, max_message_bytes=64)
    assert error_code(call(broker, "graph.search", {"query": "x"})) == "tool_failed"


def test_service_exceptions_never_leak_their_text(storage, hook):
    class Exploding(GraphService):
        def search_graph(self, query, **kwargs):
            raise RuntimeError("secret internal path /srv/app/config")

    broker = make_broker(Exploding(storage, authorization_hook=hook))
    response = call(broker, "graph.search", {"query": "x"})
    assert error_code(response) == "tool_failed"
    assert "secret" not in response["error"]["message"]


def test_a_failed_operation_is_reported_as_tool_failed(service):
    broker = make_broker(service)
    response = call(
        broker,
        "graph.update_node",
        {"node_id": "does-not-exist", "updates": {"description": "x"}},
    )
    assert error_code(response) == "tool_failed"


def test_tool_call_records_hold_outcomes_but_no_arguments_or_results(service):
    broker = make_broker(service, grants=(GRANT_GRAPH_READ,))
    call(broker, "graph.search", {"query": "secret-query"})
    call(broker, "graph.add", {"nodes": []})
    assert [(r.tool, r.outcome) for r in broker.tool_calls] == [
        ("graph.search", "ok"),
        ("graph.add", "grant_denied"),
    ]
    assert "secret-query" not in repr(broker.tool_calls)
