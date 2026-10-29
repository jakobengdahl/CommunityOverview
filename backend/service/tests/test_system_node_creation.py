"""System objects created over either assistant transport must work immediately."""

from unittest.mock import Mock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from backend.agents.config import AgentConfig
from backend.core import GraphStorage, Node
from backend.core.events import EventDispatcher
from backend.core.session_manager import SessionManager
from backend.core.session_store import InMemorySessionPersistenceBackend, SessionStore
from backend.core.storage_backends import FileGraphPersistenceBackend
from backend.core.write_modes import WriteModes
from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GraphAccessNarrowing,
    GraphAuthorizationDecision,
)
from backend.service import GraphService, register_mcp_tools
from backend.service.system_nodes import SYSTEM_CREATE_MODELS
from backend.ui import ChatService


@pytest.fixture(params=["mcp", "assistant"])
def client(request, tmp_path):
    backend = FileGraphPersistenceBackend(tmp_path / "graph.json", staged_sessions=True)
    storage = GraphStorage(str(tmp_path / "graph.json"), persistence_backend=backend)
    service = GraphService(storage)
    if request.param == "mcp":
        mcp = Mock()
        mcp.tool.return_value = lambda tool: tool
        tools = register_mcp_tools(mcp, service)
        chat = None
    else:
        with patch("backend.ui.chat_logic.create_provider"):
            chat = ChatService(service)
        tools = chat._tools_map
    yield tools, service, storage, chat
    storage.shutdown_events()


def test_agent_is_enabled_and_subscription_routes_to_its_exact_id(client):
    tools, _, storage, _ = client
    result = tools["create_agent"](
        config={
            "name": "Triage",
            "task_prompt": "Review new initiatives",
            "filters": {"target": {"node_types": ["Initiative"]}},
        }
    )
    assert result["success"], result
    agent = storage.get_node(result["created_node_id"])
    config = AgentConfig.from_node(agent)
    assert config.enabled is True
    assert config.prompts.task_prompt == "Review new initiatives"
    assert config.mcp_integration_ids == ["GRAPH"]
    subscription = storage.get_node(config.subscription_id)
    delivery = subscription.metadata["delivery"]
    assert delivery["webhook_url"] == f"internal://agent/{agent.id}"
    assert delivery["ignore_origins"] == [f"agent:{agent.id}"]
    loaded = EventDispatcher(storage)._load_subscriptions()
    assert loaded[0]["id"] == subscription.id
    assert loaded[0]["filters"].target.node_types == ["Initiative"]
    assert any(
        e.source == agent.id and e.target == subscription.id
        for e in storage.edges.values()
    )


def test_agent_can_be_created_disabled(client):
    tools, _, storage, _ = client
    result = tools["create_agent"](
        config={"name": "Off", "task_prompt": "Review", "enabled": False}
    )
    assert result["success"]
    assert storage.get_node(result["created_node_id"]).metadata["enabled"] is False


def test_event_subscription_has_loadable_filters_and_delivery(client):
    tools, _, storage, _ = client
    result = tools["create_event_subscription"](
        config={
            "name": "Webhook",
            "webhook_url": "https://8.8.8.8/events",
            "filters": {"operations": ["update"], "keywords": {"any": ["review"]}},
            "ignore_origins": ["integration"],
        }
    )
    assert result["success"], result
    subscription = EventDispatcher(storage)._load_subscriptions()[0]
    assert subscription["filters"].operations == ["update"]
    assert subscription["filters"].keywords.any == ["review"]
    assert subscription["delivery"].ignore_origins == ["integration"]


def test_skill_persists_runtime_fields(client):
    tools, _, storage, _ = client
    result = tools["create_skill"](
        config={
            "name": "Review",
            "content": "# Review\nCheck consistency.",
            "when_to_use": "When reviewing",
            "allowed_tools": ["search_graph"],
            "version": "1",
        }
    )
    assert result["success"], result
    node = storage.get_node(result["created_node_id"])
    assert node.type_str == "Skill"
    assert node.metadata["content"] == "# Review\nCheck consistency."
    assert node.metadata["when_to_use"] == "When reviewing"
    assert node.metadata["allowed_tools"] == ["search_graph"]


def test_collection_resolves_and_response_is_linked_by_id(client):
    tools, _, storage, _ = client
    created = tools["create_active_knowledge_collection"](
        config={
            "name": "Ideas",
            "short_name": "ideas",
            "prompt": "Collect ideas",
            "node_type_permissions": {"Initiative": {"create": True}},
            "tool_allowlist": ["add_nodes", "save_collection_response"],
        }
    )
    assert created["success"], created
    collection_id = created["created_node_id"]
    with patch("backend.ui.chat_logic.create_provider"):
        chat = ChatService(GraphService(storage))
    prefix, perms, collect = chat._resolve_collection("ideas")
    assert "Collect ideas" in prefix
    assert perms["Initiative"]["create"]
    assert collect
    result = tools["create_collection_response"](
        config={
            "collection_id": collection_id,
            "answers": [{"field_id": "idea", "value": "Make reviews easier"}],
        }
    )
    assert result["success"], result
    response = storage.get_node(result["created_node_id"])
    assert response.metadata["collection_id"] == collection_id
    assert response.metadata["collection_short_name"] == "ideas"
    assert response.metadata["submitted_at"].endswith("Z")
    assert response.metadata["answers"][0]["value"] == "Make reviews easier"
    assert any(
        e.source == response.id and e.target == collection_id
        for e in storage.edges.values()
    )


def test_saved_view_loads_positions_without_frontend(client):
    tools, service, storage, _ = client
    storage.add_nodes([Node(id="subject", name="Subject", type="Initiative")], [])
    result = tools["create_saved_view"](
        config={
            "name": "Headless view",
            "node_ids": ["subject"],
            "positions": {"subject": {"x": 120, "y": -20}},
        }
    )
    assert result["success"], result
    loaded = service.get_saved_view("Headless view")
    assert loaded["success"], loaded
    assert [node["id"] for node in loaded["nodes"]] == ["subject"]
    assert loaded["positions"] == {"subject": {"x": 120, "y": -20}}


@pytest.mark.parametrize(
    "name,config",
    [
        ("create_agent", {"name": "Bad", "task_prompt": " "}),
        ("create_agent", {"name": "Bad", "task_prompt": "Review", "enabled": "true"}),
        (
            "create_agent",
            {"name": "Bad", "task_prompt": "Review", "filters": {"operations": []}},
        ),
        (
            "create_agent",
            {
                "name": "Bad",
                "task_prompt": "Review",
                "filters": {"operations": ["typo"]},
            },
        ),
        (
            "create_event_subscription",
            {"name": "Bad", "webhook_url": "http://127.0.0.1/events"},
        ),
        (
            "create_event_subscription",
            {"name": "Bad", "webhook_url": "internal://agent/missing"},
        ),
        (
            "create_event_subscription",
            {"name": "Bad", "webhook_url": "https://8.8.8.8:99999/events"},
        ),
        (
            "create_event_subscription",
            {"name": "Bad", "webhook_url": "https://user:pass@8.8.8.8/events"},
        ),
        ("create_skill", {"name": "Bad", "content": "", "when_to_use": "Review"}),
        (
            "create_skill",
            {
                "name": "Bad",
                "content": "Review",
                "when_to_use": "Review",
                "contnet": "typo",
            },
        ),
        (
            "create_active_knowledge_collection",
            {"name": "Bad", "short_name": "Has spaces"},
        ),
        ("create_saved_view", {"name": "Bad", "node_ids": []}),
        ("create_saved_view", {"name": "Bad", "node_ids": ["missing"]}),
        (
            "create_collection_response",
            {"collection_id": "missing", "answers": [{"field_id": "a", "value": 1}]},
        ),
        ("create_collection_response", {"collection_id": "missing", "answers": []}),
        (
            "create_collection_response",
            {"collection_id": "missing", "answers": [{"field_id": "a"}]},
        ),
    ],
)
def test_invalid_creation_writes_nothing(client, name, config):
    tools, _, storage, _ = client
    result = tools[name](config=config)
    assert result["success"] is False, result
    assert result["error"] == "validation_error"
    assert not storage.nodes
    assert not storage.edges


def test_duplicate_collection_short_name_is_refused_including_legacy_ids(client):
    tools, _, storage, _ = client
    storage.add_nodes(
        [
            Node(
                id="legacy",
                name="Legacy",
                type="ActiveKnowledgeCollection",
                metadata={"short_name": "ideas"},
            )
        ],
        [],
    )
    result = tools["create_active_knowledge_collection"](
        config={"name": "New", "short_name": "ideas"}
    )
    assert result["success"] is False
    assert set(storage.nodes) == {"legacy"}


def test_saved_view_rejects_invalid_geometry_and_unsafe_annotations(client):
    tools, _, storage, _ = client
    storage.add_nodes([Node(id="subject", name="Subject", type="Initiative")], [])
    for extra in [
        {"positions": {"other": {"x": 1, "y": 2}}},
        {"positions": {"subject": {"x": float("inf"), "y": 2}}},
        {"hidden_nodes": ["other"]},
        {
            "annotation_document": {
                "annotations": [
                    {
                        "id": "unsafe",
                        "type": "image",
                        "image": {"url": "https://example.org/image.png"},
                    }
                ]
            }
        },
    ]:
        result = tools["create_saved_view"](
            config={"name": "Bad", "node_ids": ["subject"], **extra}
        )
        assert result["success"] is False, result
    assert set(storage.nodes) == {"subject"}


def test_all_tools_use_ui_create_authorization_before_validation(client):
    tools, service, storage, _ = client
    contexts = []

    class DenyCreate:
        def evaluate(self, context):
            contexts.append(context)
            return GraphAuthorizationDecision(
                allowed=context.action != GRAPH_ACTION_MUTATE
            )

    service._authorization_hook = DenyCreate()
    for name in SYSTEM_CREATE_MODELS:
        result = tools[name](config={})
        assert result["error_code"] == "access_denied", result
    assert len(contexts) == len(SYSTEM_CREATE_MODELS)
    assert all(
        c.action == GRAPH_ACTION_MUTATE and c.target == "add_nodes" for c in contexts
    )
    assert not storage.nodes


def test_references_outside_mutate_scope_are_refused(client):
    tools, service, storage, _ = client
    storage.add_nodes(
        [
            Node(
                id="hidden",
                name="Hidden",
                type="ActiveKnowledgeCollection",
                metadata={"short_name": "hidden"},
            )
        ],
        [],
    )

    class NarrowMutate:
        def evaluate(self, context):
            return GraphAuthorizationDecision(
                allowed=True,
                graph_access=GraphAccessNarrowing(
                    enabled=context.action == GRAPH_ACTION_MUTATE,
                    allow_local_graph=False,
                    include_graph_ids=("allowed",),
                ),
            )

    service._authorization_hook = NarrowMutate()
    for name, config in [
        ("create_saved_view", {"name": "View", "node_ids": ["hidden"]}),
        (
            "create_collection_response",
            {"collection_id": "hidden", "answers": [{"field_id": "a", "value": True}]},
        ),
    ]:
        result = tools[name](config=config)
        assert not result["success"]
    assert set(storage.nodes) == {"hidden"}


def test_dedicated_tools_obey_staging_and_can_reference_staged_nodes(client):
    tools, service, storage, _ = client
    modes = WriteModes(storage.layer_store)
    manager = SessionManager(
        SessionStore(
            InMemorySessionPersistenceBackend(), staged_status=modes.staged_status
        ),
        write_modes=modes,
    )
    service.attach_sessions(manager)
    session_id = manager.create_session("Draft", "staged").id
    result = tools["create_active_knowledge_collection"](
        config={"name": "Ideas", "short_name": "ideas"},
        visualization_session_id=session_id,
    )
    assert result["success"] and result["staged"], result
    response = tools["create_collection_response"](
        config={
            "collection_id": result["created_node_id"],
            "answers": [{"field_id": "a", "value": 1}],
        },
        visualization_session_id=session_id,
    )
    assert response["success"] and response["staged"], response
    duplicate = tools["create_active_knowledge_collection"](
        config={"name": "Duplicate", "short_name": "ideas"},
        visualization_session_id=session_id,
    )
    assert not duplicate["success"]
    assert not storage.nodes
    assert len(storage.layer_store.load_layer(session_id)) == 3


def test_system_creates_cannot_bypass_collection_mode_permissions(client):
    _, service, storage, _ = client
    with patch("backend.ui.chat_logic.create_provider"):
        chat = ChatService(service)
    overlay = chat._make_enforced_tools({"Initiative": {"create": True}})
    for name in SYSTEM_CREATE_MODELS:
        result = overlay[name](config={})
        assert not result["success"]
        assert "collection mode" in result["error"]
    assert not storage.nodes


def test_real_mcp_and_assistant_advertise_matching_creation_models(empty_service):
    mcp = FastMCP("system-node-tests")
    register_mcp_tools(mcp, empty_service)
    advertised = {tool.name: tool.parameters for tool in mcp._tool_manager.list_tools()}
    with patch("backend.ui.chat_logic.create_provider"):
        chat = ChatService(empty_service)
    assistant = {
        tool["name"]: tool["input_schema"] for tool in chat._processor.tool_definitions
    }
    for name in SYSTEM_CREATE_MODELS:
        assert advertised[name]["required"] == ["config"]
        assert assistant[name]["required"] == ["config"]
        assert advertised[name]["properties"]["config"]["$ref"] in {
            f"#/$defs/{SYSTEM_CREATE_MODELS[name].__name__}"
        }
        model_schema = assistant[name]["properties"]["config"]
        assert (
            model_schema["properties"]
            == advertised[name]["$defs"][SYSTEM_CREATE_MODELS[name].__name__][
                "properties"
            ]
        )
