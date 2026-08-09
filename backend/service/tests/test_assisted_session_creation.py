"""
End-to-end verification of the *assistant-created* visualization workflow: the
full "prepare → organize → link → hand over → clean up" story an MCP-connected
assistant runs, driven only through the registered MCP tools.

Where ``test_mcp_session_crud_tools.py`` unit-tests each CRUD tool and
``test_agent_layout_workflow.py`` proves the layout/geometry contract, this
module stitches both seams into one cohesive session lifecycle and then checks
the failure paths a real assistant hits.

Open-core vs. hosted-layer boundary (why some task scenarios map the way they
do here):

* ``owner``/``workspace`` are reserved in the projection (null in open core) and
  ``lifecycle_state`` is always "active" — per-tenant ownership and *time-based*
  expiry are enforced by the hosted layer, which swaps in the service
  authorization hook. So "isolation from other tenants" is verified at the two
  guarantees the open core actually provides: (1) two sessions never bleed state
  into each other, and (2) the authorization hook is the single seam that denies
  access — the exact seam the hosted layer scopes per tenant.
* There is no time-based expiry in the open core, so an "expired session" is
  exercised as its observable equivalent: a link whose session no longer exists
  (deleted, or never present) resolves to not-found — the state the deep-link UI
  renders as "not found / expired".
* Session names are intentionally non-unique (contract §2), so a duplicate name
  is *not* an error; the test pins that invariant (two same-named sessions stay
  distinct and independently addressable) rather than asserting a failure the
  system deliberately does not raise.
"""

import os
from unittest.mock import MagicMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest

from backend.config.config_loader import PUBLIC_BASE_URL_ENV
from backend.core import GraphStorage
from backend.core.session_store import (
    InMemorySessionPersistenceBackend,
    SessionStore,
)
from backend.core.session_manager import SessionManager
from backend.runtime.authorization import AUTHORIZATION_MODE_ENV
from backend.service import GraphService, register_mcp_tools


@pytest.fixture
def tools(tmp_path):
    """tools_map wired to an in-memory shared-session manager, plus that manager."""
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))

    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(mock_mcp, service, session_manager=manager)
    return tools_map, manager


def _populate(manager, session_id, node_ids):
    """Add nodes server-side.

    Stands in for the browser echoing back the ``nodes_added`` op after the
    assistant pushes nodes via ``search_graph``/``get_related_nodes`` with a
    ``visualization_session_id`` — the same seam both sibling test modules use to
    populate a session without a live canvas.
    """
    session = manager.get_session(session_id)
    manager.store.apply_state_op(session, {"op": "nodes_added", "node_ids": node_ids})
    manager.store.persist(session)


class AssistantSessionClient:
    """A minimal stand-in for an MCP-connected assistant running the workflow.

    It talks only through the registered tools (plus ``_populate`` for the
    browser-echo seam), so the test exercises the same surface a real assistant
    sees — never reaching into the manager to shortcut a step.
    """

    def __init__(self, tools_map, manager):
        self.tools = tools_map
        self.manager = manager

    def create(self, name=None):
        return self.tools["create_visualization_session"](name=name)["session"]

    def populate(self, session_id, node_ids):
        _populate(self.manager, session_id, node_ids)

    def read_geometry(self, session_id):
        return self.tools["get_visualization_layout"](session_id=session_id)

    def arrange(self, session_id, positions, expected_revision=None):
        return self.tools["apply_visualization_layout"](
            session_id=session_id,
            positions=positions,
            expected_revision=expected_revision,
        )

    def rename(self, session_id, name):
        return self.tools["rename_visualization_session"](
            session_id=session_id, name=name
        )

    def open_link(self, session_url):
        """Resolve a canonical link the way opening it does: look up its session.

        The deep link is ``<base>/?session=<id>``; "opening it as an authorized
        user" is, server-side, resolving that id back to its session resource.
        """
        session_id = parse_qs(urlparse(session_url).query)["session"][0]
        return self.tools["get_visualization_session"](session_id=session_id)

    def delete(self, session_id, confirm=False):
        return self.tools["delete_visualization_session"](
            session_id=session_id, confirm=confirm
        )


class TestFullAssistantWorkflow:
    def test_prepare_organize_link_handover_cleanup(self, tools, monkeypatch):
        # A public base URL is configured so the canonical link resolves (contract
        # §5); without it session_url is null, covered separately below.
        monkeypatch.setenv(PUBLIC_BASE_URL_ENV, "https://app.example.test")
        tools_map, manager = tools
        agent = AssistantSessionClient(tools_map, manager)

        # 1. Create a named session.
        session = agent.create(name="Roadmap review")
        sid = session["session_id"]
        assert session["name"] == "Roadmap review"
        assert session["lifecycle_state"] == "active"
        assert session["revision"] == 0
        assert session["node_count"] == 0
        assert session["capabilities"] == ["read", "rename", "delete", "layout"]

        # 2. Populate it.
        agent.populate(sid, ["a", "b", "c", "d"])

        # 3. Obtain geometry — freshly added nodes have no position yet (§2: null,
        #    not origin).
        geometry = agent.read_geometry(sid)
        assert geometry["node_count"] == 4
        assert all(n["x"] is None and n["y"] is None for n in geometry["nodes"])
        size = geometry["assumed_node_size"]

        # 4. Batch-arrange into a collision-free row using the advertised node size
        #    (never a hard-coded box), threading the read revision for optimistic
        #    concurrency.
        step = size["width"] + 60
        targets = {
            node["id"]: {"x": i * step, "y": 0}
            for i, node in enumerate(geometry["nodes"])
        }
        arranged = agent.arrange(sid, targets, expected_revision=geometry["revision"])
        assert arranged["success"] is True
        assert arranged["moved"] == 4
        assert arranged["revision"] == geometry["revision"] + 1

        # Geometry now reflects the arrangement.
        after = {n["id"]: n for n in agent.read_geometry(sid)["nodes"]}
        assert after["a"]["x"] == 0.0
        assert after["d"]["x"] == 3 * step

        # 5. Rename it (bumps the revision; op-routed so a reconnecting client sees
        #    it).
        renamed = agent.rename(sid, "Roadmap review — Q3")
        assert renamed["success"] is True
        assert renamed["session"]["name"] == "Roadmap review — Q3"

        # 6. Retrieve the canonical URL — server-owned, on the configured origin,
        #    in the established ?session=<id> form.
        got = tools_map["get_visualization_session"](session_id=sid)["session"]
        assert got["session_url"] == f"https://app.example.test/?session={sid}"

        # 7. Open the link as an authorized user — it resolves to the same session,
        #    with full capabilities under the permissive open-core hook.
        opened = agent.open_link(got["session_url"])
        assert opened["success"] is True
        assert opened["session"]["session_id"] == sid
        assert opened["session"]["name"] == "Roadmap review — Q3"
        assert "read" in opened["session"]["capabilities"]

        # 8. Delete a disposable session with confirmation: unconfirmed is a no-op,
        #    confirmed removes it.
        assert agent.delete(sid)["error"] == "confirmation_required"
        assert manager.get_session(sid) is not None  # still there
        deleted = agent.delete(sid, confirm=True)
        assert deleted["success"] is True and deleted["deleted"] is True
        assert manager.get_session(sid) is None

    def test_session_url_null_when_base_unconfigured(self, tools, monkeypatch):
        # The assistant must hand over the server's link verbatim; with no public
        # base URL there is nothing to hand over, so it is null (never fabricated).
        monkeypatch.delenv(PUBLIC_BASE_URL_ENV, raising=False)
        tools_map, manager = tools
        agent = AssistantSessionClient(tools_map, manager)
        session = agent.create(name="No link yet")
        assert session["session_url"] is None


class TestCrossSessionIsolation:
    def test_two_sessions_do_not_share_state(self, tools, monkeypatch):
        # Open-core isolation guarantee: distinct sessions are independent — the
        # substrate the hosted layer scopes per tenant. A change to one is never
        # observable through the other.
        monkeypatch.setenv(PUBLIC_BASE_URL_ENV, "https://app.example.test")
        tools_map, manager = tools
        agent = AssistantSessionClient(tools_map, manager)

        a = agent.create(name="Team A view")
        b = agent.create(name="Team B view")
        assert a["session_id"] != b["session_id"]

        agent.populate(a["session_id"], ["a1", "a2"])
        agent.arrange(a["session_id"], {"a1": {"x": 10, "y": 20}})
        agent.rename(a["session_id"], "Team A — arranged")

        # B is untouched: no nodes, no positions, its own name and link.
        b_geom = agent.read_geometry(b["session_id"])
        assert b_geom["node_count"] == 0
        b_meta = tools_map["get_visualization_session"](session_id=b["session_id"])[
            "session"
        ]
        assert b_meta["name"] == "Team B view"
        assert b_meta["revision"] == 0
        assert b_meta["session_url"] == (
            f"https://app.example.test/?session={b['session_id']}"
        )

        # B's link resolves to B, never to A.
        assert (
            agent.open_link(b_meta["session_url"])["session"]["session_id"]
            == b["session_id"]
        )

    def test_link_targets_only_its_own_session(self, tools, monkeypatch):
        monkeypatch.setenv(PUBLIC_BASE_URL_ENV, "https://app.example.test")
        tools_map, manager = tools
        agent = AssistantSessionClient(tools_map, manager)
        a = agent.create(name="A")
        b = agent.create(name="B")
        a_url = tools_map["get_visualization_session"](session_id=a["session_id"])[
            "session"
        ]["session_url"]
        opened = agent.open_link(a_url)
        assert opened["session"]["session_id"] == a["session_id"]
        assert opened["session"]["session_id"] != b["session_id"]


class TestDuplicateNames:
    def test_duplicate_names_do_not_collide_or_fail(self, tools):
        # Contract §2: names are non-unique. A duplicate name is not rejected, and
        # the two sessions stay distinct and independently addressable — isolation
        # is by id, not by name.
        tools_map, manager = tools
        agent = AssistantSessionClient(tools_map, manager)

        first = agent.create(name="Shared name")
        second = agent.create(name="Shared name")
        assert first["session_id"] != second["session_id"]
        assert first["name"] == second["name"] == "Shared name"

        # Populate only the first; the second must not inherit its nodes.
        agent.populate(first["session_id"], ["n1", "n2", "n3"])
        assert (
            tools_map["get_visualization_session"](session_id=first["session_id"])[
                "session"
            ]["node_count"]
            == 3
        )
        assert (
            tools_map["get_visualization_session"](session_id=second["session_id"])[
                "session"
            ]["node_count"]
            == 0
        )


class TestExpiredOrStaleLink:
    def test_deleted_session_link_resolves_to_not_found(self, tools, monkeypatch):
        # A link outlives its session: once deleted, opening the URL resolves to
        # not-found — the observable equivalent of an expired session (the state
        # the deep-link UI renders as "not found / expired").
        monkeypatch.setenv(PUBLIC_BASE_URL_ENV, "https://app.example.test")
        tools_map, manager = tools
        agent = AssistantSessionClient(tools_map, manager)

        session = agent.create(name="Ephemeral")
        url = session["session_url"]
        assert agent.open_link(url)["success"] is True  # resolves while it exists

        agent.delete(session["session_id"], confirm=True)

        stale = agent.open_link(url)
        assert stale["success"] is False
        assert "not found" in stale["error"]

    def test_unknown_wellformed_id_is_not_found(self, tools):
        tools_map, _ = tools
        result = tools_map["get_visualization_session"](
            session_id="1111-2222-3333-4444"
        )
        assert result["success"] is False
        assert "not found" in result["error"]


class TestUnauthorizedAccess:
    def test_read_only_denies_mutation_and_narrows_capabilities(
        self, tools, monkeypatch
    ):
        # The authorization hook is the tenant-enforcement seam for the session
        # *resource* tools. Under a restricted mode an assistant may still read a
        # session, but the resource mutations the workflow performs (create,
        # rename, delete) are denied, and the capability list on a read drops the
        # mutate verbs so the assistant sees the boundary up front. (The geometry
        # read/write tools are not gated by this hook — see SMALL_FIXES.md — so
        # they are deliberately not asserted here.)
        tools_map, manager = tools
        agent = AssistantSessionClient(tools_map, manager)
        session = agent.create(name="Locked later")
        sid = session["session_id"]
        agent.populate(sid, ["x"])

        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        got = tools_map["get_visualization_session"](session_id=sid)
        assert got["success"] is True
        assert got["session"]["capabilities"] == ["read"]

        for denied in (
            tools_map["create_visualization_session"](),
            agent.rename(sid, "nope"),
            agent.delete(sid, confirm=True),
        ):
            assert denied["success"] is False
            assert denied.get("error_code") == "access_denied"

    def test_deny_all_blocks_reads(self, tools, monkeypatch):
        tools_map, manager = tools
        agent = AssistantSessionClient(tools_map, manager)
        session = agent.create(name="Hidden")
        sid = session["session_id"]

        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")
        opened = tools_map["get_visualization_session"](session_id=sid)
        assert opened["success"] is False
        assert opened.get("error_code") == "access_denied"
        listed = tools_map["list_visualization_sessions"]()
        assert listed["success"] is False
        assert listed.get("error_code") == "access_denied"
