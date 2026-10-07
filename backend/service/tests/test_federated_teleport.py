"""Tests for resolving the teleport route through GraphService.

``backend/federation/tests/test_teleport.py`` covers the pure resolution
rules; this file covers the wiring: that a local node, a cached federated node
and an adopted node each resolve through the same entry point, and that the
caller's graph-access narrowing is the thing that decides visibility.
"""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.core import GraphStorage, Node, NodeType
from backend.federation import teleport
from backend.federation.config import FederationFileConfig
from backend.federation.manager import FederationManager
from backend.runtime.authorization import (
    GraphAccessNarrowing,
    GraphAuthorizationDecision,
)
from backend.service import GraphService

_GUI_URL = "https://esam.example/app"
_REMOTE_NODE = {"id": "remote-1", "type": "Actor", "name": "External Node"}


def _service(
    tmp_path,
    *,
    gui_url=_GUI_URL,
    enabled=True,
    cache_status="healthy",
    local_nodes=(),
):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    storage = GraphStorage(str(graph_file))
    if local_nodes:
        storage.add_nodes(list(local_nodes), [])

    endpoints = {"graph_json_url": "https://esam.example/graph.json"}
    if gui_url:
        endpoints["gui_url"] = gui_url

    config = FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "graphs": [
                    {
                        "graph_id": "esam-main",
                        "display_name": "eSam",
                        "enabled": enabled,
                        "trust_level": "partner",
                        "capabilities": {"allow_adopt": True},
                        "endpoints": endpoints,
                    }
                ],
            }
        }
    )

    manager = FederationManager(config)
    cache_nodes, _ = manager._build_cache(
        config.federation.graphs[0], [_REMOTE_NODE], []
    )
    manager._cache["esam-main"].nodes = cache_nodes
    manager._cache["esam-main"].status = cache_status

    return GraphService(storage, federation_manager=manager)


class _NarrowingHook:
    """Authorization hook that narrows graph visibility to ``include``."""

    def __init__(self, *include, allow_local=True, allowed=True):
        self._include = tuple(include)
        self._allow_local = allow_local
        self._allowed = allowed

    def evaluate(self, context):
        return GraphAuthorizationDecision(
            allowed=self._allowed,
            reason="" if self._allowed else "request refused",
            graph_access=GraphAccessNarrowing(
                enabled=True,
                allow_local_graph=self._allow_local,
                include_graph_ids=self._include,
            ),
        )


def _local_node():
    return Node(id="local-1", type=NodeType.ACTOR, name="Local Node")


# ---------------------------------------------------------------------------
# The federated path
# ---------------------------------------------------------------------------


def test_a_cached_federated_node_resolves_to_a_route_into_its_source_graph(tmp_path):
    result = _service(tmp_path).resolve_teleport("federated::esam-main::remote-1")

    assert result["success"] is True
    assert result["status"] == teleport.STATUS_OK
    assert result["origin_graph_id"] == "esam-main"
    assert result["origin_graph_name"] == "eSam"
    assert result["origin_node_id"] == "remote-1"
    assert result["trust_level"] == "partner"
    assert parse_qs(urlsplit(result["route"]).query)["node"] == ["remote-1"]


def test_the_route_carries_the_session_and_the_search_context(tmp_path):
    result = _service(tmp_path).resolve_teleport(
        "federated::esam-main::remote-1",
        session_id="1111-2222-3333-4444",
        search_query="external",
    )

    params = parse_qs(urlsplit(result["route"]).query)
    assert params["from_session"] == ["1111-2222-3333-4444"]
    assert params["q"] == ["external"]


def test_a_teleport_out_of_this_deployment_is_flagged_cross_deployment(tmp_path):
    result = _service(tmp_path).resolve_teleport(
        "federated::esam-main::remote-1",
        request_origin="https://here.example/",
    )

    assert result["cross_deployment"] is True


def test_a_teleport_within_one_deployment_is_not_flagged(tmp_path):
    result = _service(tmp_path, gui_url="https://here.example/other").resolve_teleport(
        "federated::esam-main::remote-1",
        request_origin="https://here.example/",
    )

    assert result["cross_deployment"] is False


def test_a_backlink_is_offered_when_the_public_base_url_is_known(tmp_path):
    result = _service(tmp_path).resolve_teleport(
        "federated::esam-main::remote-1",
        session_id="1111-2222-3333-4444",
        local_gui_url="https://here.example/app",
    )

    assert parse_qs(urlsplit(result["backlink"]).query)["from_session"] == [
        "1111-2222-3333-4444"
    ]


def test_no_backlink_is_offered_when_the_deployment_url_is_unknown(tmp_path):
    result = _service(tmp_path).resolve_teleport("federated::esam-main::remote-1")

    assert "backlink" not in result


# ---------------------------------------------------------------------------
# The local path — same entry point, same provenance field
# ---------------------------------------------------------------------------


def test_a_local_node_resolves_as_already_being_here(tmp_path):
    service = _service(tmp_path, local_nodes=[_local_node()])

    result = service.resolve_teleport("local-1")

    assert result["success"] is True
    assert result["status"] == teleport.STATUS_LOCAL
    assert "route" not in result


def test_an_unknown_id_is_neither_local_nor_routed(tmp_path):
    result = _service(tmp_path).resolve_teleport("no-such-node")

    assert result["success"] is False
    assert result["status"] == teleport.STATUS_UNKNOWN_NODE
    assert "route" not in result


def test_an_adopted_node_resolves_as_local_because_adoption_strips_provenance(tmp_path):
    # adopt_federated_node removes the federation bookkeeping keys and re-nests
    # lineage under metadata["adopted_from"], so the adopted copy is owned here.
    # Teleport must agree with that rather than routing away from the local copy.
    service = _service(tmp_path)
    adopted = service.adopt_federated_node("federated::esam-main::remote-1")
    assert adopted["success"] is True, adopted

    result = service.resolve_teleport(adopted["adopted_node"]["id"])

    assert result["status"] == teleport.STATUS_LOCAL


# ---------------------------------------------------------------------------
# Behaviour 1: permission denial comes from the existing narrowing
# ---------------------------------------------------------------------------


def test_a_caller_narrowed_away_from_the_source_graph_gets_no_route(tmp_path):
    service = _service(tmp_path)
    service._authorization_hook = _NarrowingHook("other-graph")

    result = service.resolve_teleport("federated::esam-main::remote-1")

    assert result["status"] == teleport.STATUS_PERMISSION_DENIED
    assert "route" not in result


def test_a_denied_caller_is_told_nothing_about_the_graph(tmp_path):
    service = _service(tmp_path)
    service._authorization_hook = _NarrowingHook("other-graph")

    result = service.resolve_teleport("federated::esam-main::remote-1")

    assert result["origin_graph_id"] == ""
    assert result["origin_graph_name"] == ""
    assert "esam.example" not in repr(result)
    assert "eSam" not in repr(result)


def test_a_caller_narrowed_to_the_source_graph_still_gets_its_route(tmp_path):
    service = _service(tmp_path)
    service._authorization_hook = _NarrowingHook("esam-main")

    result = service.resolve_teleport("federated::esam-main::remote-1")

    assert result["status"] == teleport.STATUS_OK


def test_a_local_node_is_withheld_from_a_caller_narrowed_off_the_local_graph(tmp_path):
    service = _service(tmp_path, local_nodes=[_local_node()])
    service._authorization_hook = _NarrowingHook("esam-main", allow_local=False)

    result = service.resolve_teleport("local-1")

    assert result["status"] == teleport.STATUS_PERMISSION_DENIED


def test_a_refused_request_is_an_access_denied_error_not_a_teleport_status(tmp_path):
    # A request-level refusal must stay the shape the REST layer turns into a
    # 403; only the per-graph case is a teleport status the UI renders.
    service = _service(tmp_path)
    service._authorization_hook = _NarrowingHook(allowed=False)

    result = service.resolve_teleport("federated::esam-main::remote-1")

    assert result["error_code"] == "access_denied"
    assert "status" not in result


# ---------------------------------------------------------------------------
# Behaviour 2: unavailable graph
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cache_status", ["degraded", "offline"])
def test_an_unreachable_source_graph_yields_no_route(tmp_path, cache_status):
    result = _service(tmp_path, cache_status=cache_status).resolve_teleport(
        "federated::esam-main::remote-1"
    )

    assert result["status"] == teleport.STATUS_GRAPH_UNAVAILABLE
    assert result["reason"] == "graph_unreachable"
    assert "route" not in result


def test_a_source_graph_without_a_gui_url_yields_no_route(tmp_path):
    result = _service(tmp_path, gui_url=None).resolve_teleport(
        "federated::esam-main::remote-1"
    )

    assert result["status"] == teleport.STATUS_GRAPH_UNAVAILABLE
    assert result["reason"] == "no_gui_url_configured"


def test_an_unavailable_graph_is_still_named_for_an_entitled_caller(tmp_path):
    result = _service(tmp_path, cache_status="offline").resolve_teleport(
        "federated::esam-main::remote-1"
    )

    assert result["origin_graph_name"] == "eSam"


def test_the_cache_status_the_manager_reports_is_the_one_teleport_reads(tmp_path):
    service = _service(tmp_path, cache_status="degraded")

    assert service._federation_manager.get_cache_status("esam-main") == "degraded"
    assert service.resolve_teleport("federated::esam-main::remote-1")["status"] == (
        teleport.STATUS_GRAPH_UNAVAILABLE
    )


def test_cache_status_is_empty_for_a_graph_that_is_not_configured(tmp_path):
    service = _service(tmp_path)

    assert service._federation_manager.get_cache_status("no-such-graph") == ""


# ---------------------------------------------------------------------------
# Federation disabled / absent
# ---------------------------------------------------------------------------


def test_a_local_node_still_resolves_with_no_federation_manager_at_all(tmp_path):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    storage = GraphStorage(str(graph_file))
    storage.add_nodes([_local_node()], [])
    service = GraphService(storage)

    assert service.resolve_teleport("local-1")["status"] == teleport.STATUS_LOCAL


def test_a_federated_id_is_unknown_with_no_federation_manager(tmp_path):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    service = GraphService(GraphStorage(str(graph_file)))

    assert service.resolve_teleport("federated::esam-main::remote-1")["status"] == (
        teleport.STATUS_UNKNOWN_NODE
    )
