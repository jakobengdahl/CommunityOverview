"""Mutation policy covers every activation boundary, including linked batches."""

from unittest.mock import Mock
from threading import Event

import pytest

from backend.agents.execution import InMemoryExecutionStore
from backend.core import Edge, GraphStorage, Node
from backend.core.storage_backends import FileGraphPersistenceBackend
from backend.core.storage_merge import MergeWrites, NodeOverwrite
from backend.runtime.authorization import (
    DefaultGraphAuthorizationHook,
    use_request_authorization,
)
from backend.runtime.extensions import (
    MutationPolicyDecision,
    MutationPolicyDenied,
    RuntimeExtensions,
)
from backend.runtime.session_context import bind_session
from backend.service import import_service, views
from backend.service.system_nodes import make_system_create_tools
from backend.service.tests.test_staged_writes import World


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "graph.json"
    storage = GraphStorage(
        str(path),
        persistence_backend=FileGraphPersistenceBackend(path, staged_sessions=True),
    )
    storage.add_nodes(
        [Node(id="a", type="Actor", name="A"), Node(id="b", type="Actor", name="B")],
        [Edge(id="ab", source="a", target="b", type="RELATES_TO")],
    )
    world = World(storage)
    yield world
    storage.shutdown_events()


def deny(storage):
    hook = Mock()
    hook.evaluate.return_value = MutationPolicyDecision(False, "resource_blocked")
    storage.runtime_extensions = RuntimeExtensions(mutation_policy=hook)
    return hook


DIRECT_WRITES = [
    lambda s: s.add_nodes([Node(id="new", type="Actor", name="N")], []),
    lambda s: s.add_nodes(
        [], [Edge(id="new", source="a", target="b", type="RELATES_TO")]
    ),
    lambda s: s.add_edge(Edge(id="new", source="a", target="b", type="RELATES_TO")),
    lambda s: s.update_node("a", {"name": "Changed"}),
    lambda s: s.update_edge("ab", {"label": "Changed"}),
    lambda s: s.update_edge("ab", {"type": "DEPENDS_ON"}),
    lambda s: s.delete_nodes(["a"], confirmed=True),
    lambda s: s.delete_edge("ab"),
    lambda s: s.delete_edges(["ab"]),
    lambda s: s.set_nodes_archived(["a"], True),
    lambda s: s.set_edges_archived(["ab"], True),
    lambda s: s.replace_all_nodes_and_edges(
        [Node(id="a", type="Agent", name="Changed")], []
    ),
    lambda s: s.apply_import_merge(
        lambda: MergeWrites(
            node_creates=[Node(id="new", type="Actor", name="New")],
            node_overwrites=[
                NodeOverwrite("a", s.nodes["a"].revision, {"type": "Agent"}, False)
            ],
        )
    ),
]


@pytest.mark.parametrize("write", DIRECT_WRITES)
def test_policy_denial_undoes_the_entire_direct_write_without_events(world, write):
    before = world.main()
    vectors = {k: v.copy() for k, v in world.storage.vector_store.embeddings.items()}
    deny(world.storage)
    with pytest.raises(MutationPolicyDenied, match="resource_blocked"):
        write(world.storage)
    assert world.main() == before
    assert world.events == []
    for key, vector in vectors.items():
        assert (world.storage.vector_store.embeddings[key] == vector).all()


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize(
    "tool,config",
    [
        ("create_agent", {"name": "Review", "task_prompt": "Review changes"}),
        (
            "create_event_subscription",
            {"name": "Hook", "webhook_url": "https://8.8.8.8/hook"},
        ),
        (
            "create_skill",
            {"name": "Skill", "content": "Review", "when_to_use": "Always"},
        ),
        (
            "create_active_knowledge_collection",
            {"name": "More ideas", "short_name": "more-ideas"},
        ),
        ("create_saved_view", {"name": "View", "node_ids": ["a"]}),
        (
            "create_collection_response",
            {
                "collection_id": "collection",
                "answers": [{"field_id": "q", "value": "a"}],
            },
        ),
    ],
)
def test_dedicated_tools_reject_whole_linked_batch(world, staged, tool, config):
    world.storage.add_nodes(
        [
            Node(
                id="collection",
                type="ActiveKnowledgeCollection",
                name="Ideas",
                metadata={"short_name": "ideas"},
            )
        ],
        [],
    )
    sid = world.staged_session() if staged else None
    before = world.main()
    world.events.clear()
    hook = deny(world.storage)
    with bind_session(sid):
        result = make_system_create_tools(world.service)[tool](config)
    assert not result["success"]
    assert result["error"] == "resource_blocked"
    assert world.main() == before
    assert world.events == []
    if staged:
        assert world.layer(sid) == {}
    if tool == "create_agent":
        changes = hook.evaluate.call_args.args[1]
        assert {c.after["type"] for c in changes if c.kind == "node"} == {
            "Agent",
            "EventSubscription",
        }
        assert len(changes) == 3


@pytest.mark.parametrize(
    "operation,args,kwargs",
    [
        ("add_nodes", ([{"id": "new", "type": "Actor", "name": "New"}], []), {}),
        ("update_node", ("a", {"name": "Changed"}), {}),
        ("delete_nodes", (["a"],), {"confirmed": True}),
        ("update_edge", ("ab", {"type": "DEPENDS_ON"}), {}),
    ],
)
@pytest.mark.parametrize("staged", [False, True])
def test_generic_service_writes_return_stable_denial(
    world, operation, args, kwargs, staged
):
    sid = world.staged_session() if staged else None
    deny(world.storage)
    before = world.main()
    with bind_session(sid):
        result = getattr(world.service, operation)(*args, **kwargs)
    assert not result["success"]
    assert result["error"] == "resource_blocked"
    assert world.main() == before
    if staged:
        assert world.layer(sid) == {}


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize(
    "operation,args,kwargs",
    [
        ("add_nodes", ([{"id": "new", "type": "Actor", "name": "New"}], []), {}),
        ("update_node", ("a", {"name": "Changed"}), {}),
        ("delete_nodes", (["a"],), {"confirmed": True}),
        ("archive_nodes", (["a"],), {}),
        ("add_edge", ("a", "b"), {}),
        ("update_edge", ("ab", {"label": "Changed"}), {}),
        ("delete_edge", ("ab",), {}),
        ("delete_edges", (["ab"],), {"confirmed": True}),
        ("archive_edges", (["ab"],), {}),
    ],
)
def test_service_policy_preserves_explicit_mutation_correlation(
    world, staged, operation, args, kwargs
):
    sid = world.staged_session() if staged else None
    hook = deny(world.storage)
    with (
        bind_session(sid),
        use_request_authorization(
            actor_id="editor", graph_id="graph", headers={"x-request-id": "request"}
        ),
    ):
        result = getattr(world.service, operation)(
            *args, **kwargs, event_correlation_id="mutation-correlation"
        )
    assert result["error"] == "resource_blocked"
    context = hook.evaluate.call_args.args[0]
    assert context.correlation_id == "mutation-correlation"
    assert context.request_id == "request"
    assert context.actor["actor_id"] == "editor"
    assert context.scope["graph_id"] == "graph"
    assert context.session_id == (sid or "")


def test_final_session_merge_is_rechecked_and_keeps_layer_on_denial(world):
    sid = world.staged_session()
    with bind_session(sid):
        assert make_system_create_tools(world.service)["create_agent"](
            {"name": "Review", "task_prompt": "Review"}
        )["success"]
    before, layer = world.main(), world.layer(sid)
    world.events.clear()
    hook = deny(world.storage)
    result = world.service.merge_staged_changes(sid, "merge-policy")
    assert not result["success"]
    assert result["error"] == "resource_blocked"
    assert world.main() == before and world.layer(sid) == layer
    assert world.events == []
    assert hook.evaluate.call_args.args[0].session_id == sid
    assert len(hook.evaluate.call_args.args[1]) == 3
    assert world.storage.layer_store.load_merge("merge-policy") is None


@pytest.mark.parametrize("archive", [False, True])
def test_import_and_archive_restore_reject_policy_without_replacing(world, archive):
    auth = DefaultGraphAuthorizationHook()
    document = {"nodes": [{"id": "new", "type": "Actor", "name": "New"}], "edges": []}
    if archive:
        payload = views.export_graph_archive(world.storage, auth)["archive_bytes"]
    deny(world.storage)
    before = world.main()
    if archive:
        result = import_service.import_graph_archive(
            world.storage, auth, InMemoryExecutionStore(), payload
        )
    else:
        result = import_service.import_graph(
            world.storage, auth, InMemoryExecutionStore(), document
        )
    assert not result["success"]
    assert result["error_code"] == "resource_blocked"
    assert world.main() == before


def test_merge_import_service_rejects_policy_without_audit_or_activation(world):
    before = world.main()
    deny(world.storage)
    jobs = InMemoryExecutionStore()
    result = import_service.commit_merge_import(
        world.storage,
        DefaultGraphAuthorizationHook(),
        jobs,
        {
            "document": {
                "nodes": [{"id": "new", "type": "Actor", "name": "New"}],
                "edges": [],
            },
            "policy": {"default": "skip"},
        },
    )
    assert result["error_code"] == "resource_blocked"
    assert world.main() == before and world.events == []
    assert jobs.list_jobs() == []


def test_policy_gets_detached_immutable_revisions_links_and_request_context(world):
    seen = []

    class Policy:
        def evaluate(self, context, changes):
            seen.append((context, changes))
            with pytest.raises(TypeError):
                changes[0].after["metadata"]["values"][0]["x"] = 9
            with pytest.raises(TypeError):
                context.actor["actor_id"] = "other"
            return MutationPolicyDecision()

    world.storage.runtime_extensions = RuntimeExtensions(mutation_policy=Policy())
    before_revision = world.storage.nodes["a"].revision
    with use_request_authorization(
        actor_id="editor", graph_id="graph", headers={"x-request-id": "request"}
    ):
        result = world.service.update_node(
            "a", {"metadata": {"values": [{"x": 1}]}}, event_correlation_id="corr"
        )
    assert result["success"]
    context, changes = seen[0]
    assert (
        context.actor["actor_id"] == "editor" and context.scope["graph_id"] == "graph"
    )
    assert context.correlation_id == "corr" and context.request_id == "request"
    assert changes[0].before["revision"] == before_revision
    assert changes[0].after["type"] == "Actor"
    world.storage.nodes["a"].metadata["values"][0]["x"] = 2
    assert changes[0].after["metadata"]["values"][0]["x"] == 1


def test_a_failing_policy_fails_closed_and_restores_the_graph(world):
    policy = Mock()
    policy.evaluate.side_effect = RuntimeError("broken")
    world.storage.runtime_extensions = RuntimeExtensions(mutation_policy=policy)
    before = world.main()
    result = world.service.update_node("a", {"name": "changed"})
    assert result["error"] == "mutation_policy_error"
    assert world.main() == before


def test_policy_outcomes_are_observed_without_becoming_graph_events(world):
    received, done = [], Event()

    class Sink:
        def emit(self, event):
            received.append(event)
            if event.event_type == "mutation_policy.denied":
                done.set()

    policy = Mock()
    policy.evaluate.side_effect = [
        MutationPolicyDecision(),
        MutationPolicyDecision(False, "blocked"),
    ]
    world.storage.runtime_extensions = RuntimeExtensions(
        mutation_policy=policy, event_sink=Sink()
    )
    assert world.service.update_node("a", {"name": "Allowed"})["success"]
    assert world.service.update_node("a", {"name": "Denied"})["error"] == "blocked"
    assert done.wait(1)
    assert [e.event_type for e in received] == [
        "mutation_policy.allowed",
        "mutation_policy.denied",
    ]
    assert all(e.resource_ids == ("a",) for e in received)
    assert len(world.events) == 1
    assert world.events[0].event_type.value == "node.update"
