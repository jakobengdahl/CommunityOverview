# covers: backend/api_host/mcp_mount.py, backend/api_host/server.py
"""Protocol compatibility through the mounted MCP transports."""

import json
import socket
import threading
from unittest.mock import patch

import anyio
import httpx
import pytest
import uvicorn
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client

from backend.api_host import AppConfig, create_app

LEGACY = ("2024-11-05", "2025-03-26", "2025-06-18")
MODERN = "2026-07-28"
UNKNOWN = "2099-01-01"


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    root = tmp_path_factory.mktemp("mcp-protocol")
    graph = root / "graph.json"
    graph.write_text(json.dumps({"nodes": [], "edges": []}))
    with patch("backend.core.vector_store.VectorStore.preload_model"):
        app = create_app(
            AppConfig(graph_file=str(graph), sessions_dir=str(root / "sessions"))
        )
    ready = threading.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
    thread.start()
    try:
        assert ready.wait(30) and server.started
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        listener.close()
        assert not thread.is_alive()


def _rpc(method, request_id, params=None):
    message = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    return message


def _initialization(version):
    return {
        "protocolVersion": version,
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    }


def _result(response):
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        body = next(
            line[6:] for line in response.text.splitlines() if line.startswith("data: ")
        )
        payload = json.loads(body)
    else:
        payload = response.json()
    assert "error" not in payload
    return payload["result"]


def _snapshot(instructions, tools, resources, call):
    return {
        "instructions": instructions,
        "tools": {tool["name"]: tool["inputSchema"] for tool in tools},
        "resources": resources,
        "call": call,
    }


async def _http_snapshot(client, version):
    headers = {"Accept": "application/json, text/event-stream"}
    if version in LEGACY:
        response = await client.post(
            "/mcp",
            json=_rpc("initialize", 1, _initialization(version)),
            headers=headers,
        )
        initialized = _result(response)
        assert initialized["protocolVersion"] == version
        headers.update(
            {
                "Mcp-Session-Id": response.headers["mcp-session-id"],
                "Mcp-Protocol-Version": version,
            }
        )
        response = await client.post(
            "/mcp", json=_rpc("notifications/initialized", None), headers=headers
        )
        assert response.status_code == 202
        instructions = initialized["instructions"]
        meta = {}
    else:
        headers["Mcp-Protocol-Version"] = version
        headers["Mcp-Method"] = "server/discover"
        meta = {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": version,
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        }
        response = await client.post(
            "/mcp", json=_rpc("server/discover", 1, meta), headers=headers
        )
        instructions = _result(response)["instructions"]

    async def request(method, params=None):
        payload = {**meta, **(params or {})}
        if version == MODERN:
            headers["Mcp-Method"] = method
            if method == "tools/call":
                headers["Mcp-Name"] = params["name"]
        response = await client.post(
            "/mcp", json=_rpc(method, 2, payload), headers=headers
        )
        return _result(response)

    tools = await request("tools/list")
    resources = await request("resources/list")
    assert not tools.get("nextCursor") and not resources.get("nextCursor")
    call = await request("tools/call", {"name": "get_capabilities", "arguments": {}})
    assert not call.get("isError", False)
    if version == MODERN:
        call.pop("_meta", None)
        call.pop("resultType", None)
    return _snapshot(instructions, tools["tools"], resources["resources"], call)


async def _sse_snapshot(url, version):
    async with sse_client(url + "/mcp/sse") as streams:
        async with ClientSession(*streams) as session:
            result = await session.send_request(
                types.InitializeRequest(
                    params=types.InitializeRequestParams(**_initialization(version))
                ),
                types.InitializeResult,
            )
            if version == UNKNOWN:
                assert result.protocol_version in (*LEGACY, "2025-11-25")
                assert result.protocol_version != version
                return
            assert result.protocol_version == version
            session.adopt(result)
            await session.send_notification(types.InitializedNotification())
            tools = (await session.list_tools()).model_dump(
                by_alias=True, exclude_none=True, exclude_unset=True
            )
            resources = (await session.list_resources()).model_dump(
                by_alias=True, exclude_none=True, exclude_unset=True
            )
            call = (await session.call_tool("get_capabilities", {})).model_dump(
                by_alias=True, exclude_none=True, exclude_unset=True
            )
            return _snapshot(
                result.instructions, tools["tools"], resources["resources"], call
            )


def test_protocol_versions_share_the_same_server_contract(server_url):
    """Supported versions expose the same inventory, instructions and result."""

    async def exercise():
        async with httpx.AsyncClient(base_url=server_url, timeout=15) as client:
            snapshots = [
                await _http_snapshot(client, version) for version in (*LEGACY, MODERN)
            ]
            snapshots.extend(
                [await _sse_snapshot(server_url, version) for version in LEGACY]
            )
            assert snapshots[0]["tools"] and snapshots[0]["instructions"]
            for snapshot in snapshots[1:]:
                assert snapshot == snapshots[0]
            response = await client.post(
                "/mcp",
                headers={"Accept": "application/json, text/event-stream"},
                json=_rpc("initialize", 1, _initialization(UNKNOWN)),
            )
            assert _result(response)["protocolVersion"] in (*LEGACY, "2025-11-25")
            response = await client.post(
                "/mcp",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Mcp-Protocol-Version": UNKNOWN,
                    "Mcp-Method": "tools/list",
                },
                json=_rpc(
                    "tools/list",
                    2,
                    {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": UNKNOWN,
                            "io.modelcontextprotocol/clientCapabilities": {},
                        }
                    },
                ),
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == -32022
            assert response.json()["error"]["data"]["requested"] == UNKNOWN
            assert MODERN in response.json()["error"]["data"]["supported"]
            await _sse_snapshot(server_url, UNKNOWN)

    anyio.run(exercise)
