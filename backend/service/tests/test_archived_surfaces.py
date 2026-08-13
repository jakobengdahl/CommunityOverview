"""
Surface tests for the archived lifecycle: the MCP tools and the REST endpoints
expose archive/unarchive and the ``include_archived`` opt-in with matching
behaviour on the same graph.
"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from backend.api_host import create_app
from backend.api_host.config import AppConfig


@pytest.fixture
def env():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        graph_path = os.path.join(tmpdir, "graph.json")
        app = create_app(AppConfig(graph_file=graph_path))
        yield {
            "client": TestClient(app),
            "tools": app.state.tools_map,
        }


def _seed(tools):
    tools["add_nodes"](
        nodes=[
            {"id": "n1", "type": "Actor", "name": "Alpha"},
            {"id": "n2", "type": "Actor", "name": "Beta"},
        ],
        edges=[{"id": "e1", "source": "n1", "target": "n2"}],
    )


class TestMcpArchiveTools:
    def test_archive_tools_registered(self, env):
        for name in (
            "archive_nodes",
            "unarchive_nodes",
            "archive_edges",
            "unarchive_edges",
        ):
            assert name in env["tools"]

    def test_archive_hides_from_search_and_unarchive_restores(self, env):
        tools = env["tools"]
        _seed(tools)
        tools["archive_nodes"](node_ids=["n2"])

        names = {n["name"] for n in tools["search_graph"](query="")["nodes"]}
        assert names == {"Alpha"}

        names_incl = {
            n["name"]
            for n in tools["search_graph"](query="", include_archived=True)["nodes"]
        }
        assert names_incl == {"Alpha", "Beta"}

        tools["unarchive_nodes"](node_ids=["n2"])
        names_after = {n["name"] for n in tools["search_graph"](query="")["nodes"]}
        assert names_after == {"Alpha", "Beta"}

    def test_archive_edge_hides_from_related(self, env):
        tools = env["tools"]
        _seed(tools)
        tools["archive_edges"](edge_ids=["e1"])
        related = tools["get_related_nodes"](node_id="n1", depth=1)
        assert "Beta" not in {n["name"] for n in related["nodes"]}
        related_incl = tools["get_related_nodes"](
            node_id="n1", depth=1, include_archived=True
        )
        assert "Beta" in {n["name"] for n in related_incl["nodes"]}


class TestRestArchiveEndpoints:
    def test_archive_and_search_include_archived(self, env):
        client = env["client"]
        client.post(
            "/api/nodes",
            json={
                "nodes": [
                    {"id": "n1", "type": "Actor", "name": "Alpha"},
                    {"id": "n2", "type": "Actor", "name": "Beta"},
                ],
                "edges": [],
            },
        )

        resp = client.post("/api/nodes/archive", json={"node_ids": ["n2"]})
        assert resp.status_code == 200
        assert resp.json()["archived"] is True

        default = client.post("/api/search", json={"query": ""}).json()
        assert {n["name"] for n in default["nodes"]} == {"Alpha"}
        assert default["filters"]["include_archived"] is False

        incl = client.post(
            "/api/search", json={"query": "", "include_archived": True}
        ).json()
        assert {n["name"] for n in incl["nodes"]} == {"Alpha", "Beta"}

        # Unarchive via the same endpoint with archived=false.
        client.post("/api/nodes/archive", json={"node_ids": ["n2"], "archived": False})
        restored = client.post("/api/search", json={"query": ""}).json()
        assert {n["name"] for n in restored["nodes"]} == {"Alpha", "Beta"}

    def test_related_include_archived(self, env):
        client = env["client"]
        client.post(
            "/api/nodes",
            json={
                "nodes": [
                    {"id": "n1", "type": "Actor", "name": "Alpha"},
                    {"id": "n2", "type": "Actor", "name": "Beta"},
                ],
                "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
            },
        )
        client.post("/api/edges/archive", json={"edge_ids": ["e1"]})

        default = client.post("/api/nodes/n1/related", json={"depth": 1}).json()
        assert "Beta" not in {n["name"] for n in default["nodes"]}

        incl = client.post(
            "/api/nodes/n1/related", json={"depth": 1, "include_archived": True}
        ).json()
        assert "Beta" in {n["name"] for n in incl["nodes"]}
