"""Tests for federation manager cache sync and search behavior."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from backend.federation.config import FederationFileConfig
from backend.federation.manager import FederationManager


class _Handler(BaseHTTPRequestHandler):
    payload = {
        "nodes": [
            {
                "id": "remote-1",
                "type": "Actor",
                "name": "eSam",
                "description": "External organization",
                "summary": "Federated node",
                "tags": ["external"],
            }
        ],
        "edges": [],
    }

    def do_GET(self):
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def _start_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_sync_and_search_remote_graph_json():
    server = _start_server()
    try:
        port = server.server_address[1]
        config = FederationFileConfig.model_validate({
            "federation": {
                "enabled": True,
                "max_traversal_depth": 1,
                "graphs": [
                    {
                        "graph_id": "esam-main",
                        "display_name": "eSam",
                        "enabled": True,
                        "endpoints": {
                            "graph_json_url": f"http://127.0.0.1:{port}/graph.json"
                        },
                    }
                ],
            }
        })

        manager = FederationManager(config)
        sync = manager.sync_all()

        assert sync["success"] is True
        result = manager.search_nodes(query="esam", node_types=None, limit=10)
        assert len(result["nodes"]) == 1
        node = result["nodes"][0]
        assert node.metadata["origin_graph_id"] == "esam-main"
        assert node.metadata["is_federated"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_sync_degrades_when_unreachable_url():
    config = FederationFileConfig.model_validate({
        "federation": {
            "enabled": True,
            "graphs": [
                {
                    "graph_id": "broken",
                    "display_name": "Broken",
                    "enabled": True,
                    "endpoints": {
                        "graph_json_url": "http://127.0.0.1:1/graph.json"
                    },
                }
            ],
        }
    })

    manager = FederationManager(config)
    sync = manager.sync_all()
    assert sync["success"] is False

    status = manager.get_status()
    assert status["graphs"][0]["status"] == "degraded"


def test_scheduler_starts_for_scheduled_graph():
    config = FederationFileConfig.model_validate({
        "federation": {
            "enabled": True,
            "graphs": [
                {
                    "graph_id": "sched",
                    "display_name": "Scheduled",
                    "enabled": True,
                    "sync": {
                        "mode": "scheduled",
                        "interval_seconds": 10,
                        "on_startup": False,
                        "on_demand": True
                    },
                    "endpoints": {
                        "graph_json_url": "http://127.0.0.1:1/graph.json"
                    },
                }
            ],
        }
    })

    manager = FederationManager(config)
    manager.start()
    try:
        status = manager.get_status()
        assert status["scheduler_running"] is True
    finally:
        manager.stop()


def test_sync_emits_node_events_for_cache_changes():
    events = []

    config = FederationFileConfig.model_validate({
        "federation": {
            "enabled": True,
            "graphs": [
                {
                    "graph_id": "esam-main",
                    "display_name": "eSam",
                    "enabled": True,
                    "endpoints": {
                        "graph_json_url": "https://example.invalid/graph.json"
                    },
                }
            ],
        }
    })

    manager = FederationManager(
        config,
        on_node_event=lambda op, before, after: events.append((op, before.id if before else None, after.id if after else None)),
    )

    graph = config.federation.graphs[0]

    first_nodes, _ = manager._build_cache(
        graph,
        [{"id": "remote-1", "type": "Actor", "name": "Version One"}],
        [],
    )
    second_nodes, _ = manager._build_cache(
        graph,
        [{"id": "remote-1", "type": "Actor", "name": "Version Two"}, {"id": "remote-2", "type": "Actor", "name": "New"}],
        [],
    )

    manager._emit_node_events({}, first_nodes)
    manager._emit_node_events(first_nodes, second_nodes)

    assert ("create", None, "federated::esam-main::remote-1") in events
    assert ("update", "federated::esam-main::remote-1", "federated::esam-main::remote-1") in events
    assert ("create", None, "federated::esam-main::remote-2") in events


def test_sync_emits_edge_events_for_cache_changes():
    edge_events = []

    config = FederationFileConfig.model_validate({
        "federation": {
            "enabled": True,
            "graphs": [
                {
                    "graph_id": "esam-main",
                    "display_name": "eSam",
                    "enabled": True,
                    "endpoints": {
                        "graph_json_url": "https://example.invalid/graph.json"
                    },
                }
            ],
        }
    })

    manager = FederationManager(
        config,
        on_edge_event=lambda op, before, after: edge_events.append((op, before.id if before else None, after.id if after else None)),
    )

    graph = config.federation.graphs[0]
    _, first_edges = manager._build_cache(
        graph,
        [{"id": "n1", "type": "Actor", "name": "A"}, {"id": "n2", "type": "Actor", "name": "B"}],
        [{"id": "e1", "source": "n1", "target": "n2", "type": "RELATES_TO"}],
    )

    _, second_edges = manager._build_cache(
        graph,
        [{"id": "n1", "type": "Actor", "name": "A"}, {"id": "n2", "type": "Actor", "name": "B"}],
        [{"id": "e1", "source": "n1", "target": "n2", "type": "PART_OF"}],
    )

    manager._emit_edge_events({}, first_edges)
    manager._emit_edge_events(first_edges, second_edges)

    # create on first sync + delete/create replace semantics on update
    assert any(evt[0] == "create" and evt[2] == "federated::esam-main::e1" for evt in edge_events)
    assert any(evt[0] == "delete" and evt[1] == "federated::esam-main::e1" for evt in edge_events)
