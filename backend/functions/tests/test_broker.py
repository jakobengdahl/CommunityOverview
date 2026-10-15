"""Invariants of the user-function tool broker (USER_FUNCTION_CONTRACT §6.3–§7, §9)."""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from backend.functions.broker import (
    BROKER_ERROR_CODE,
    MAX_MESSAGE_BYTES,
    MAX_TOOL_CALLS,
    wire_size,
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
from backend.core import Node, NodeType
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
    overrides.setdefault("grant_enabled", lambda grant: True)
    overrides.setdefault("authorization_inputs", INVOKER)
    context = BrokerContext(
        run_id="job-test",
        function_id="recompute",
        function_version="1.2.0",
        declared_grants=frozenset(grants),
        correlation_id="corr-1",
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
    storage.add_nodes(
        [
            Node(
                name="Similar Remote",
                type=NodeType.ACTOR,
                metadata={"origin_graph_id": "other-graph"},
            )
        ],
        [],
    )
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


@pytest.mark.parametrize(
    "arguments",
    [
        {"nodes": [{"name": "N", "type": "Actor", "origin_graph_id": "other"}]},
        {
            "nodes": [
                {"name": "N", "type": "Actor", "metadata": {"origin_graph_id": "o"}}
            ]
        },
    ],
)
def test_a_function_cannot_add_nodes_into_another_graph_scope(service, arguments):
    response = call(make_broker(service), "graph.add", arguments)
    assert error_code(response) == "invalid_arguments"
    assert service.calls == []


def test_a_function_cannot_move_a_node_into_another_graph_scope(service, storage):
    broker = make_broker(service)
    node_id = add_node(broker, "Local node")
    response = call(
        broker,
        "graph.update_node",
        {"node_id": node_id, "updates": {"metadata": {"origin_graph_id": "other"}}},
    )
    assert error_code(response) == "invalid_arguments"
    assert "origin_graph_id" not in (storage.get_node(node_id).metadata or {})


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("graph.search", {"query": "x", "limit": 501}),
        ("graph.search", {"query": "x", "limit": True}),
        ("graph.get_related", {"node_id": "n", "depth": 4}),
        ("graph.get_related", {"node_id": "n", "depth": -1}),
    ],
)
def test_work_bounds_on_a_single_call_are_enforced(service, tool, arguments):
    assert error_code(call(make_broker(service), tool, arguments)) == (
        "invalid_arguments"
    )


def test_an_access_denied_result_from_the_service_is_reported_as_access_denied(
    storage, hook
):
    class DenyingInside(GraphService):
        def search_graph(self, query, **kwargs):
            return {"success": False, "error_code": "access_denied", "message": "no"}

    broker = make_broker(DenyingInside(storage, authorization_hook=hook))
    assert error_code(call(broker, "graph.search", {"query": "x"})) == "access_denied"


def test_refused_requests_count_toward_the_budget(service):
    broker = make_broker(service, max_tool_calls=2)
    broker.refuse(
        {"id": 1, "params": {"name": "graph.search"}}, "concurrent_call", "busy"
    )
    assert "result" in call(broker, "graph.search", {"query": "a"})
    assert error_code(call(broker, "graph.search", {"query": "b"})) == "limit_exceeded"


def test_call_records_stay_bounded_and_never_keep_function_chosen_names(service):
    broker = make_broker(service, max_tool_calls=3)
    long_name = "x" * 100_000
    for i in range(50):
        call(broker, long_name, request_id=i)
    assert len(broker.tool_calls) == 3
    assert broker.calls_over_budget == 47
    assert all(record.tool == "" for record in broker.tool_calls)


def test_malformed_arguments_are_refused_and_handle_request_never_raises(service):
    broker = make_broker(service)
    for bad in ([1, 2], "query"):
        response = broker.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tool",
                "params": {"name": "graph.search", "arguments": bad},
            }
        )
        assert error_code(response) == "invalid_arguments"


def test_the_hook_sees_the_service_method_as_target(service, hook):
    call(make_broker(service), "graph.search", {"query": "x"})
    assert {c.target for c in hook.contexts} == {"search_graph"}


def test_every_captured_authorization_input_is_rebound_for_the_call(storage):
    from backend.runtime.authorization import get_current_request_authorization_inputs

    seen = []

    class CapturingHook(RecordingHook):
        def evaluate(self, context):
            seen.append(get_current_request_authorization_inputs())
            return super().evaluate(context)

    inputs = {
        "headers": {"x-communityoverview-actor-id": "h-actor"},
        "actor_id": "user-42",
        "actor_type": "human",
        "auth_source": "test",
        "workspace_id": "ws-1",
        "workspace_kind": "team",
        "graph_id": "graph-1",
    }
    broker = make_broker(
        SpyService(storage, authorization_hook=CapturingHook()),
        authorization_inputs=inputs,
    )
    call(broker, "graph.search", {"query": "x"})
    assert seen and all(captured == inputs for captured in seen)


def _long_message_service(storage, hook, message):
    class Failing(GraphService):
        def search_graph(self, query, **kwargs):
            return {"success": False, "error": message}

        def get_node_details(self, node_id):
            return {"success": False, "error_code": "access_denied", "message": message}

    return Failing(storage, authorization_hook=hook)


def test_error_messages_are_bounded_and_never_empty(storage, hook):
    broker = make_broker(_long_message_service(storage, hook, "e" * 5000))
    failed = call(broker, "graph.search", {"query": "x"})
    denied = call(broker, "graph.get_node", {"node_id": "n"})
    assert error_code(failed) == "tool_failed"
    assert error_code(denied) == "access_denied"
    for response in (failed, denied):
        assert 0 < len(response["error"]["message"]) <= 500

    blank = make_broker(_long_message_service(storage, hook, "   "))
    message = call(blank, "graph.search", {"query": "x"})["error"]["message"]
    assert message == "The operation failed."


def test_a_long_hook_reason_is_bounded(storage):
    class LongReasonHook(RecordingHook):
        def evaluate(self, context):
            return GraphAuthorizationDecision(
                allowed=False, reason="r" * 5000, mode="custom", source="test"
            )

    broker = make_broker(SpyService(storage, authorization_hook=LongReasonHook()))
    response = call(broker, "graph.search", {"query": "x"})
    assert error_code(response) == "access_denied"
    assert len(response["error"]["message"]) <= 500


def test_reflected_names_in_error_messages_are_bounded(service):
    broker = make_broker(service)
    unknown_tool = call(broker, "t" * 1000)
    unknown_args = call(broker, "graph.search", {"query": "x", "a" * 1000: 1})
    for response in (unknown_tool, unknown_args):
        assert len(response["error"]["message"]) <= 500


def test_the_response_limit_is_measured_in_bytes_and_is_inclusive(storage, hook):
    class Fixed(GraphService):
        payload = {}

        def search_graph(self, query, **kwargs):
            return dict(Fixed.payload)

    service = Fixed(storage, authorization_hook=hook)
    Fixed.payload = {"t": "a" * 100}
    exact = wire_size({"jsonrpc": "2.0", "id": 1, "result": Fixed.payload})
    assert "result" in call(
        make_broker(service, max_message_bytes=exact), "graph.search", {"query": "q"}
    )
    assert error_code(
        call(
            make_broker(service, max_message_bytes=exact - 1),
            "graph.search",
            {"query": "q"},
        )
    ) == ("tool_failed")
    # The id counts too: the whole line written to the function is measured.
    assert error_code(
        call(
            make_broker(service, max_message_bytes=exact),
            "graph.search",
            {"query": "q"},
            request_id="long-id",
        )
    ) == ("tool_failed")
    Fixed.payload = {"t": "é" * 60}  # 60 characters, far more bytes on the wire
    over = make_broker(service, max_message_bytes=150)
    assert error_code(call(over, "graph.search", {"query": "q"})) == "tool_failed"


def test_results_are_returned_as_plain_json(storage, hook):
    class WithDate(GraphService):
        def search_graph(self, query, **kwargs):
            return {"when": datetime(2026, 1, 2, tzinfo=timezone.utc)}

    response = call(
        make_broker(WithDate(storage, authorization_hook=hook)),
        "graph.search",
        {"query": "q"},
    )
    assert isinstance(response["result"]["when"], str)
    json.dumps(response)


def test_contract_limits_are_the_documented_maximums():
    assert MAX_TOOL_CALLS == 200
    assert MAX_MESSAGE_BYTES == 1024 * 1024


def test_a_refused_request_is_recorded(service):
    broker = make_broker(service)
    broker.refuse(
        {"id": 1, "params": {"name": "graph.search"}}, "concurrent_call", "busy"
    )
    assert (broker.tool_calls[-1].tool, broker.tool_calls[-1].outcome) == (
        "graph.search",
        "concurrent_call",
    )


def test_replacing_metadata_wholesale_is_refused_so_scope_keys_survive(
    service, storage
):
    storage.add_nodes(
        [
            Node(
                id="scoped",
                name="Scoped",
                type=NodeType.ACTOR,
                metadata={"origin_graph_id": "g1", "k": 1},
            )
        ],
        [],
    )
    broker = make_broker(service)
    replaced = call(
        broker,
        "graph.update_node",
        {"node_id": "scoped", "updates": {"metadata": {"k": 2}}},
    )
    assert error_code(replaced) == "invalid_arguments"
    merged = call(
        broker,
        "graph.update_node",
        {
            "node_id": "scoped",
            "updates": {"metadata": {"k": 2}},
            "metadata_merge": True,
        },
    )
    assert "result" in merged, merged
    assert storage.get_node("scoped").metadata == {"origin_graph_id": "g1", "k": 2}
    cleared = call(
        broker,
        "graph.update_node",
        {
            "node_id": "scoped",
            "updates": {"metadata": {"origin_graph_id": None}},
            "metadata_merge": True,
        },
    )
    assert error_code(cleared) == "invalid_arguments"


@pytest.mark.parametrize(
    "key",
    [
        "origin_graph_id",
        "origin_node_id",
        "federation_distance",
        "is_federated",
        "is_federated_reference",
        "read_only",
        "adopted_from",
    ],
)
def test_scope_and_provenance_keys_are_refused_on_nodes_edges_and_updates(service, key):
    broker = make_broker(service)
    node = {"name": "N", "type": "Actor"}
    edge = {"source": "a", "target": "b", "type": "RELATES_TO"}
    attempts = [
        ("graph.add", {"nodes": [{**node, key: "x"}]}),
        ("graph.add", {"nodes": [{**node, "metadata": {key: "x"}}]}),
        ("graph.add", {"nodes": [], "edges": [{**edge, "metadata": {key: "x"}}]}),
        ("graph.add", {"nodes": [], "edges": [{**edge, key: "x"}]}),
        (
            "graph.update_node",
            {
                "node_id": "n",
                "updates": {"metadata": {key: "x"}},
                "metadata_merge": True,
            },
        ),
    ]
    for tool, arguments in attempts:
        assert error_code(call(broker, tool, arguments)) == "invalid_arguments", (
            tool,
            arguments,
        )
    assert service.calls == []


def test_reserved_federated_node_ids_and_malformed_edges_are_refused(service):
    broker = make_broker(service)
    spoof = {"nodes": [{"id": "federated::g9::real", "name": "S", "type": "Actor"}]}
    assert error_code(call(broker, "graph.add", spoof)) == "invalid_arguments"
    assert error_code(call(broker, "graph.add", {"nodes": [], "edges": {"a": 1}})) == (
        "invalid_arguments"
    )
    assert service.calls == []


def test_a_failing_hook_still_produces_an_answer(storage):
    class BrokenHook(RecordingHook):
        def evaluate(self, context):
            raise TimeoutError("authorization service unreachable at 10.0.0.5")

    broker = make_broker(SpyService(storage, authorization_hook=BrokenHook()))
    response = call(broker, "graph.search", {"query": "x"})
    assert error_code(response) == "tool_failed"
    assert "10.0.0.5" not in response["error"]["message"]
    assert broker.tool_calls[-1].outcome == "tool_failed"


def test_a_failing_enablement_check_still_produces_an_answer(service):
    def broken(grant):
        raise RuntimeError("config store down")

    broker = make_broker(service, grant_enabled=broken)
    assert error_code(call(broker, "graph.search", {"query": "x"})) == "tool_failed"


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("graph.search", {"query": "x", "limit": 0}),
        ("graph.search", {"query": "x", "limit": 500}),
        ("graph.get_related", {"node_id": "n", "depth": 3}),
    ],
)
def test_work_bounds_accept_their_inclusive_limits(service, tool, arguments):
    response = call(make_broker(service), tool, arguments)
    # The node may not exist; what matters is that the bound itself is accepted.
    assert "result" in response or error_code(response) == "tool_failed"


@pytest.mark.parametrize(
    "arguments",
    [{"query": "x", "limit": None}, {"query": "x", "limit": "10"}],
)
def test_work_bounds_reject_non_integers(service, arguments):
    assert error_code(call(make_broker(service), "graph.search", arguments)) == (
        "invalid_arguments"
    )


def test_every_node_in_a_batch_is_scope_checked(service):
    nodes = [
        {"name": "Fine", "type": "Actor"},
        {"name": "Sneaky", "type": "Actor", "metadata": {"origin_graph_id": "g"}},
    ]
    assert error_code(call(make_broker(service), "graph.add", {"nodes": nodes})) == (
        "invalid_arguments"
    )
    assert service.calls == []


def test_grant_enablement_has_no_default():
    with pytest.raises(TypeError):
        BrokerContext(
            run_id="r",
            function_id="f",
            function_version="1",
            declared_grants=frozenset(),
            correlation_id="c",
            authorization_inputs={},
        )


def test_similarity_search_hides_local_nodes_when_the_local_graph_is_excluded(
    storage,
):
    narrowing = GraphAccessNarrowing(
        enabled=True, allow_local_graph=False, include_graph_ids=("g1",)
    )
    service = SpyService(storage, authorization_hook=RecordingHook(narrowing=narrowing))
    storage.add_nodes([Node(name="Local Similar", type=NodeType.ACTOR)], [])
    response = call(
        make_broker(service), "graph.find_similar", {"name": "Local", "threshold": 0.1}
    )
    assert response["result"]["similar_nodes"] == []


def test_only_an_explicit_false_success_is_a_failure(storage, hook):
    class Odd(GraphService):
        def search_graph(self, query, **kwargs):
            return {"success": None, "nodes": []}

    response = call(
        make_broker(Odd(storage, authorization_hook=hook)),
        "graph.search",
        {"query": "q"},
    )
    assert "result" in response
