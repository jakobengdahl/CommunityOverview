"""Authorization of the assistant's UI routes.

The assistant reads and writes the graph through its tools, so its routes must
enforce the same request authorization as ``POST /nodes`` and the MCP mount:
the HTTP auth middleware when auth is enabled, and the graph authorization hook
evaluated with the caller's identity for every tool call.
"""

import base64
import io
import json
import os
import tempfile
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api_host.config import AppConfig
from backend.api_host.server import create_app
from backend.runtime.authorization import (
    AUTHORIZATION_MODE_ENV,
    GRAPH_ACTION_MUTATE,
    GraphAuthorizationContext,
    GraphAuthorizationDecision,
    get_current_request_authorization_inputs,
    use_request_authorization,
)
from backend.runtime.request_context import ACTOR_ID_HEADER
from backend.service import GraphService
from backend.ui import ChatService, DocumentService, create_ui_router


class ActorRoleHook:
    """Header-driven hook: viewers may only read, writer-only actors may only
    write, outsiders may do nothing.

    An anonymous request is allowed, so a route that drops the caller's
    identity lets an under-privileged caller through — the regression guarded
    against here.
    """

    def __init__(self):
        self.seen_actor_ids = []

    def evaluate(
        self, context: GraphAuthorizationContext
    ) -> GraphAuthorizationDecision:
        actor_id = context.actor.get("actor_id", "")
        self.seen_actor_ids.append(actor_id)
        if (
            actor_id == "outsider"
            or (actor_id == "viewer" and context.action == GRAPH_ACTION_MUTATE)
            or (actor_id == "writer-only" and context.action != GRAPH_ACTION_MUTATE)
        ):
            return GraphAuthorizationDecision(
                allowed=False, reason="denied for this actor", source="test"
            )
        return GraphAuthorizationDecision(allowed=True, source="test")


_DOC = b"Project Alpha: a new software initiative for digitalization."

_NEW_NODE = {
    "id": "chat-authz-node",
    "name": "Chat Authz Node",
    "type": "Initiative",
    "description": "Written through the assistant",
}


@pytest.fixture
def hooked_client(graph_storage, mock_llm_provider):
    hook = ActorRoleHook()
    graph_service = GraphService(graph_storage, authorization_hook=hook)
    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            chat_svc = ChatService(graph_service)
            chat_svc._processor.provider_type = "mock"
            chat_svc._processor.default_api_key = "test-key"
            app = FastAPI()
            app.state.chat_service = chat_svc
            app.include_router(
                create_ui_router(chat_svc, DocumentService()), prefix="/ui"
            )
            yield TestClient(app), mock_llm_provider, graph_service, hook


def _chat(client, actor_id, path="/ui/chat"):
    body = (
        {"message": "do it"}
        if path == "/ui/chat/simple"
        else {"messages": [{"role": "user", "content": "do it"}]}
    )
    return client.post(path, json=body, headers={ACTOR_ID_HEADER: actor_id})


@pytest.mark.parametrize("path", ["/ui/chat", "/ui/chat/simple"])
def test_chat_write_by_read_only_actor_is_denied(hooked_client, path):
    client, mock_llm, graph_service, hook = hooked_client
    mock_llm.mock_tool_calls = [
        {"name": "add_nodes", "input": {"nodes": [_NEW_NODE], "edges": []}}
    ]

    response = _chat(client, "viewer", path)

    assert response.status_code == 200
    assert response.json()["toolResult"]["error_code"] == "access_denied"
    assert "viewer" in hook.seen_actor_ids
    assert graph_service.get_node_details(_NEW_NODE["id"]).get("success") is False


def test_chat_read_by_unauthorized_actor_is_denied(hooked_client, sample_nodes):
    client, mock_llm, _, hook = hooked_client
    mock_llm.mock_tool_calls = [
        {"name": "get_node_details", "input": {"node_id": "test-actor-1"}}
    ]

    response = _chat(client, "outsider")

    assert response.status_code == 200
    result = response.json()["toolResult"]
    assert result["error_code"] == "access_denied"
    assert "Test Agency" not in json.dumps(result)
    assert hook.seen_actor_ids == ["outsider"]


def test_chat_write_by_permitted_actor_is_authorized_as_that_actor(hooked_client):
    client, mock_llm, graph_service, hook = hooked_client
    mock_llm.mock_tool_calls = [
        {"name": "add_nodes", "input": {"nodes": [_NEW_NODE], "edges": []}}
    ]

    response = _chat(client, "editor")

    assert response.status_code == 200
    assert hook.seen_actor_ids and set(hook.seen_actor_ids) == {"editor"}
    assert (
        graph_service.get_node_details(_NEW_NODE["id"])["node"]["name"]
        == (_NEW_NODE["name"])
    )


def _add_kiosk_collection(graph_service):
    result = graph_service.add_nodes(
        nodes=[
            {
                "id": "akc-kiosk",
                "name": "Kiosk",
                "type": "ActiveKnowledgeCollection",
                "description": "Collects actors only",
                "metadata": {
                    "short_name": "kiosk",
                    "prompt": "Collect actors.",
                    "node_type_permissions": {
                        "Actor": {"create": True, "update": False, "delete": False}
                    },
                },
            }
        ],
        edges=[],
    )
    assert result.get("success", True), result


def test_collection_chat_fails_closed_when_caller_cannot_read_collection(
    hooked_client,
):
    client, mock_llm, graph_service, _ = hooked_client
    _add_kiosk_collection(graph_service)
    mock_llm.mock_tool_calls = [
        {"name": "add_nodes", "input": {"nodes": [_NEW_NODE], "edges": []}}
    ]

    response = client.post(
        "/ui/chat",
        json={
            "messages": [{"role": "user", "content": "add"}],
            "collection_short_name": "kiosk",
        },
        headers={ACTOR_ID_HEADER: "writer-only"},
    )

    assert response.status_code == 200
    assert graph_service.get_node_details(_NEW_NODE["id"]).get("success") is False


def test_collection_chat_cannot_save_responses_when_caller_cannot_read_collection(
    hooked_client,
):
    client, mock_llm, graph_service, _ = hooked_client
    _add_kiosk_collection(graph_service)
    mock_llm.mock_tool_calls = [
        {
            "name": "save_collection_response",
            "input": {"answers": [{"field_id": "q1", "value": "yes"}]},
        }
    ]

    response = client.post(
        "/ui/chat",
        json={
            "messages": [{"role": "user", "content": "save"}],
            "collection_short_name": "kiosk",
        },
        headers={ACTOR_ID_HEADER: "writer-only"},
    )

    assert response.status_code == 200
    saved = graph_service.search_graph(query="", node_types=["CollectionResponse"])
    assert saved["nodes"] == []


def test_cached_collection_is_not_served_to_a_caller_who_cannot_read_it(
    hooked_client,
):
    client, mock_llm, graph_service, _ = hooked_client
    _add_kiosk_collection(graph_service)

    def _collection_chat(actor_id):
        mock_llm.reset()
        return client.post(
            "/ui/chat",
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "collection_short_name": "kiosk",
            },
            headers={ACTOR_ID_HEADER: actor_id},
        )

    assert _collection_chat("editor").status_code == 200
    assert "Collect actors." in mock_llm.received_system_prompts[0]

    assert _collection_chat("writer-only").status_code == 200
    assert "Collect actors." not in mock_llm.received_system_prompts[0]

    assert _collection_chat("editor").status_code == 200
    assert "Collect actors." in mock_llm.received_system_prompts[0]


def test_upload_analysis_binds_each_actor_in_one_client(hooked_client):
    client, mock_llm, graph_service, hook = hooked_client

    def _upload(actor_id):
        return client.post(
            "/ui/upload",
            files={"file": ("doc.txt", io.BytesIO(_DOC), "text/plain")},
            data={"analyze": "true"},
            headers={ACTOR_ID_HEADER: actor_id},
        )

    editor_node = {**_NEW_NODE, "id": "editor-upload"}
    mock_llm.mock_tool_calls = [
        {"name": "add_nodes", "input": {"nodes": [editor_node], "edges": []}}
    ]
    assert _upload("editor").status_code == 200
    assert hook.seen_actor_ids and set(hook.seen_actor_ids) == {"editor"}
    assert (
        graph_service.get_node_details(editor_node["id"])["node"]["id"]
        == editor_node["id"]
    )
    hook.seen_actor_ids.clear()
    mock_llm.reset()
    mock_llm.mock_tool_calls = [
        {"name": "add_nodes", "input": {"nodes": [_NEW_NODE], "edges": []}}
    ]

    response = _upload("viewer")

    assert response.status_code == 200
    tool_result = response.json()["chat_response"]["toolResult"]
    assert tool_result["error_code"] == "access_denied"
    assert hook.seen_actor_ids and set(hook.seen_actor_ids) == {"viewer"}
    assert graph_service.get_node_details(_NEW_NODE["id"]).get("success") is False


def test_request_authorization_restores_inputs_after_success_and_exception():
    """Request inputs are restored in the same context, including exceptional exits."""
    assert get_current_request_authorization_inputs() == {}
    with use_request_authorization(actor_id="editor"):
        editor_inputs = get_current_request_authorization_inputs()
        with use_request_authorization(actor_id="viewer"):
            assert get_current_request_authorization_inputs()["actor_id"] == "viewer"
        assert get_current_request_authorization_inputs() == editor_inputs
        with pytest.raises(RuntimeError, match="request failed"):
            with use_request_authorization(actor_id="outsider"):
                raise RuntimeError("request failed")
        assert get_current_request_authorization_inputs() == editor_inputs
    assert get_current_request_authorization_inputs() == {}


def test_propose_nodes_runs_with_the_callers_request_bound(hooked_client):
    # Assert the binding the service layer reads; the similarity lookup behind
    # /propose-nodes asks the hook with it (see the denial test below).
    client, _, _, _ = hooked_client
    chat_svc = client.app.state.chat_service
    seen = []

    def _record(**kwargs):
        seen.append(get_current_request_authorization_inputs())
        return {"success": True, "proposed_nodes": []}

    with patch.object(chat_svc, "propose_nodes_from_text", side_effect=_record):
        response = client.post(
            "/ui/propose-nodes",
            json={"text": "Example Agency runs a project."},
            headers={ACTOR_ID_HEADER: "outsider"},
        )

    assert response.status_code == 200
    assert seen and seen[0]["headers"][ACTOR_ID_HEADER] == "outsider"


def test_a_denied_duplicate_check_is_reported_not_read_as_no_duplicates(
    hooked_client,
):
    from types import SimpleNamespace

    from backend.runtime.authorization import use_request_authorization

    client, _, _, _ = hooked_client
    chat_svc = client.app.state.chat_service
    extracted = '[{"type": "Actor", "name": "Example Agency"}]'
    provider = SimpleNamespace(
        create_completion=lambda **_: SimpleNamespace(
            content=[{"type": "text", "text": extracted}]
        )
    )
    with patch.object(
        chat_svc._processor, "_resolve_llm_provider", return_value=(provider, None)
    ):
        with use_request_authorization(headers={ACTOR_ID_HEADER: "outsider"}):
            denied = chat_svc.propose_nodes_from_text("Example Agency runs it.")
        allowed = chat_svc.propose_nodes_from_text("Example Agency runs it.")
    assert denied["success"] is False and denied["error_code"] == "access_denied"
    assert denied["similar_existing"] == {}
    assert (
        allowed["success"] is True and "Example Agency" in allowed["similar_existing"]
    )


def test_info_graph_stats_are_evaluated_as_the_caller(hooked_client):
    client, _, _, hook = hooked_client

    client.get("/ui/info", headers={ACTOR_ID_HEADER: "outsider"})

    assert hook.seen_actor_ids and set(hook.seen_actor_ids) == {"outsider"}


def test_chat_write_is_denied_in_read_only_authorization_mode(fastapi_test_client):
    client, mock_llm, graph_service = fastapi_test_client
    mock_llm.mock_tool_calls = [
        {"name": "add_nodes", "input": {"nodes": [_NEW_NODE], "edges": []}}
    ]

    with patch.dict(os.environ, {AUTHORIZATION_MODE_ENV: "read-only"}):
        response = client.post(
            "/ui/chat", json={"messages": [{"role": "user", "content": "add"}]}
        )

    assert response.status_code == 200
    assert response.json()["toolResult"]["error_code"] == "access_denied"
    assert graph_service.get_node_details(_NEW_NODE["id"]).get("success") is False


# ==================== HTTP auth middleware (full app) ====================


@pytest.fixture
def auth_enabled_app():
    fd, graph_path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"nodes": [], "edges": []}, f)
    config = AppConfig(
        graph_file=graph_path,
        web_static_path="/nonexistent/web",
        widget_static_path="/nonexistent/widget",
        auth_enabled=True,
        auth_username="admin",
        auth_password="secret",
        sessions_dir=f"{graph_path}.sessions",
    )
    yield TestClient(create_app(config)), graph_path
    os.unlink(graph_path)


_UI_GRAPH_ROUTES = [
    ("post", "/ui/chat", {"json": {"messages": [{"role": "user", "content": "x"}]}}),
    ("post", "/ui/chat/simple", {"json": {"message": "x"}}),
    ("post", "/ui/propose-nodes", {"json": {"text": "x"}}),
    ("post", "/ui/upload", {"files": {"file": ("a.txt", b"x", "text/plain")}}),
    ("get", "/ui/info", {}),
]


@pytest.mark.parametrize("method,path,kwargs", _UI_GRAPH_ROUTES)
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()}],
    ids=["anonymous", "bad-credentials"],
)
def test_ui_graph_routes_require_login_when_auth_enabled(
    auth_enabled_app, method, path, kwargs, headers
):
    client, graph_path = auth_enabled_app

    response = getattr(client, method)(path, headers=headers, **kwargs)

    assert response.status_code == 401
    with open(graph_path) as f:
        assert json.load(f)["nodes"] == []


def test_ui_chat_accepts_valid_credentials_when_auth_enabled(auth_enabled_app):
    client, _ = auth_enabled_app
    creds = base64.b64encode(b"admin:secret").decode()

    response = client.get("/ui/info", headers={"Authorization": f"Basic {creds}"})

    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/chat", "/chat/simple", "/upload"])
@pytest.mark.parametrize(
    "name",
    [
        "list_node_attachments",
        "read_node_attachment",
        "search_node_attachments",
        "add_node_attachment",
        "delete_node_attachment",
    ],
)
async def test_chat_attachment_hook_sees_request_actor(
    chat_service, graph_storage, path, name
):
    """Each chat route binds the user before an attachment tool checks the hook."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from starlette.requests import Request
    from backend.ui.rest_api import ChatRequest, SimpleChatRequest

    _, llm = chat_service
    hook = MagicMock()
    hook.evaluate.return_value = GraphAuthorizationDecision(allowed=False)
    # The hook reaches the processor only through the service's own wiring.
    service = ChatService(
        GraphService(graph_storage, authorization_hook=hook),
        assistant_attachment_access="read_write",
    )
    service._processor.provider_type = "mock"
    service._processor.default_api_key = "test-key"
    raw = MagicMock()
    service._processor.tools_map[name] = raw
    service._processor.tool_definitions.append({"name": name})
    llm.mock_tool_calls = [{"name": name, "input": {}}]
    documents = SimpleNamespace(
        process_upload=AsyncMock(return_value={"success": True, "text": "text"})
    )
    router = create_ui_router(service, documents)
    endpoint = next(route.endpoint for route in router.routes if route.path == path)
    request = Request(
        {"type": "http", "headers": [(ACTOR_ID_HEADER.encode(), b"outsider")]}
    )
    if path == "/chat":
        await endpoint(
            ChatRequest(messages=[{"role": "user", "content": "read"}]), request
        )
    elif path == "/chat/simple":
        await endpoint(SimpleChatRequest(message="read"), request)
    else:
        upload = SimpleNamespace(read=AsyncMock(return_value=b"text"), filename="a.txt")
        await endpoint(
            request,
            upload,
            message="read",
            analyze=True,
            api_key=None,
            provider=None,
            model_profile_id=None,
        )
    raw.assert_not_called()
    hook.evaluate.assert_called_once()
    assert hook.evaluate.call_args.args[0].actor["actor_id"] == "outsider"
    assert "access_denied" in json.dumps(llm.received_messages[-1])
