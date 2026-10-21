"""
GraphService - thin facade over the service sub-modules.

All business logic lives in the four focused modules:
  access.py    — authorization and visibility helpers
  queries.py   — read-only operations
  mutations.py — write operations
  views.py     — saved views and export

Callers (rest_api.py, mcp_tools.py, …) continue to use this class unchanged;
public method signatures are preserved exactly.
"""

from contextlib import contextmanager
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Union,
)

from backend.core import GraphStorage, layer_store
from backend.core.session_manager import LayoutBusy
from backend.core.storage_search import MATCH_MODE_SUBSTRING
from backend.runtime.authorization import (
    GRAPH_ACTION_MUTATE,
    GRAPH_ACTION_READ,
    GRAPH_OPERATION_DISCARD,
    GRAPH_OPERATION_MERGE,
    DefaultGraphAuthorizationHook,
    GraphAuthorizationHook,
)
from backend.federation import FederationManager
from backend.runtime import session_context

from . import access, attachments, queries, mutations, views, import_service
from . import staged_review
from .attachments import AttachmentSettings
from .composed import annotate_staged_state
from .staging import SessionGate, StagedWrite, refusal_result

import logging

if TYPE_CHECKING:
    from backend.agents.execution import ExecutionStore
    from backend.core.session_manager import SessionManager

logger = logging.getLogger(__name__)

# A merge or discard named a direct session: it holds no staged changes.
SESSION_NOT_STAGED = "session_not_staged"
# A merge id already used by another session's merge (I9 holds per session).
MERGE_ID_TAKEN = "merge_id_taken"
# The origin of every graph event a session merge fires (§11.5).
MERGE_EVENT_ORIGIN = "session-merge"


class GraphService:
    """
    Central service class for all graph operations.

    Wraps GraphStorage and provides a clean API for:
    - Searching and querying
    - CRUD operations
    - Similarity detection
    - Statistics
    - Saved views management

    This class does NOT make any LLM calls - it only handles
    requests from clients and LLMs through various protocols.
    """

    # How often a staged write is decided again when another op changed an
    # entry it read before it could land (``layer_store.LayerRaced``).
    STAGING_ATTEMPTS = 3

    def __init__(
        self,
        storage: GraphStorage,
        federation_manager: Optional[FederationManager] = None,
        authorization_hook: Optional[GraphAuthorizationHook] = None,
        attachment_settings: Optional[AttachmentSettings] = None,
    ):
        self._storage = storage
        self._federation_manager = federation_manager
        self._authorization_hook = authorization_hook or DefaultGraphAuthorizationHook()
        # None leaves the node attachment routes answering
        # ``attachments_unavailable``; see backend/api_host/attachments.py.
        self._attachment_settings = attachment_settings
        # Which session a call acts in, and whether its writes stage
        # (docs/SESSION_OVERLAY_CONTRACT.md §9). Until sessions are attached,
        # a call naming a session is refused, never handled as a direct one.
        self._session_gate = SessionGate(storage)

    def attach_sessions(
        self,
        session_manager: "SessionManager",
        *,
        push_target_known: Optional[Callable[[str], bool]] = None,
    ) -> None:
        """Resolve the sessions requests name through ``session_manager``.

        ``push_target_known`` says whether a session id has a live legacy push
        channel, which makes a session known without stored state.
        """
        self._session_gate = SessionGate(
            self._storage, session_manager, push_target_known=push_target_known
        )

    @property
    def session_gate(self) -> SessionGate:
        return self._session_gate

    @contextmanager
    def reading_in(self, session_id: Optional[str]) -> Iterator[None]:
        """Resolve node ids for ``session_id``'s own canvas in its composed
        view, when it is a staged session (§8.2); otherwise as before. For the
        session tools and routes, which name their session rather than act in
        one; a request already acting in another session was refused before."""
        if (
            session_id
            and session_context.current_session_id() is None
            and self._session_gate.is_staged(session_id)
        ):
            with session_context.bind_session(session_id):
                yield
        else:
            yield

    def _write(
        self,
        target: str,
        expected_session_seq: Optional[int],
        run: Callable[[Optional[StagedWrite]], Dict[str, Any]],
        **defaults: Any,
    ) -> Dict[str, Any]:
        """Run a write in the graph, or in the request's staged session.

        A refusal - the session does not resolve, the layer is full, another
        participant staged first - comes back as a result carrying
        ``defaults``, the keys the direct write's refusals carry.
        """
        try:
            staging = self._session_gate.for_write(target, expected_session_seq)
        except Exception as exc:
            refused = refusal_result(exc, **defaults)
            if refused is None:
                raise
            return refused
        if staging is None:
            return run(None)
        for attempt in range(self.STAGING_ATTEMPTS):
            try:
                return run(staging)
            except layer_store.LayerRaced:
                # Another op changed an entry this one read: decided again on
                # the layer as it is now, so neither change is lost. Nothing
                # was staged or fanned out by the refused attempt.
                if attempt + 1 == self.STAGING_ATTEMPTS:
                    return refusal_result(LayoutBusy(), **defaults)
            except Exception as exc:
                refused = refusal_result(exc, **defaults)
                if refused is None:
                    raise
                return refused
        raise AssertionError("unreachable")  # pragma: no cover

    def _read(
        self,
        target: str,
        run: Callable[[Any], Dict[str, Any]],
        *,
        action: str = GRAPH_ACTION_READ,
        **defaults: Any,
    ) -> Dict[str, Any]:
        """Run a read against the graph, or, in a staged session, against the
        session's composed view (§8.2), with every entity it returns marked
        with its ``staged_state``.

        ``action`` and ``target`` are what the read asks the hook: the view
        leaves out every layer entry that narrowing does not wholly cover. The
        read still asks the hook itself, and is refused as it would be on the
        graph.
        """
        try:
            session_id = self._session_gate.read_session()
        except Exception as exc:
            refused = refusal_result(exc, **defaults)
            if refused is None:
                raise
            return refused
        if session_id is None:
            return run(self._storage)
        decision = access.evaluate_graph_access(
            self._authorization_hook, action=action, target=target
        )
        if not decision.allowed:
            # Refused by the read itself, which reads nothing first.
            return run(self._storage)
        try:
            composed = self._session_gate.compose(session_id, decision.graph_access)
        except Exception as exc:
            refused = refusal_result(exc, **defaults)
            if refused is None:
                raise
            return refused
        return annotate_staged_state(run(composed), composed)

    # ==================== Staged changes (§10-§12) ====================

    def _layer_session(
        self, session_id: str, *, action: str, target: str, operation: str = ""
    ):
        """Ask the hook, then resolve ``session_id``: returns the hook's
        decision and the session's write mode, or a refusal result. The hook
        comes first, so a refused caller learns nothing about the session."""
        decision = access.evaluate_graph_access(
            self._authorization_hook,
            action=action,
            target=target,
            graph_operation=operation,
            session_id=session_id,
        )
        if not decision.allowed:
            return None, access.build_access_denied_result(
                action=action, target=target, decision=decision
            )
        try:
            mode = self._session_gate.named_mode(session_id)
        except Exception as exc:
            refused = refusal_result(exc)
            if refused is None:
                raise
            return None, refused
        return decision, mode

    def _load_review(self, session_id: str, graph_access) -> staged_review.Review:
        manager = self._session_gate.session_manager
        entries = manager.write_modes.layer_store.load_layer(session_id)
        if manager.write_modes.staged_status(session_id) != layer_store.STATUS_ACTIVE:
            # Retired since it was resolved: refused, never read as empty.
            raise session_context.not_found(session_id)
        return staged_review.Review(self._storage, entries, graph_access)

    @staticmethod
    def _not_staged(session_id: str) -> Dict[str, Any]:
        return {
            "success": False,
            "error": SESSION_NOT_STAGED,
            "message": f"session {session_id!r} is direct; it holds no staged changes",
        }

    def get_staged_changes(self, session_id: str) -> Dict[str, Any]:
        """The diff of a staged session's layer (§10), one item per entry the
        caller's view wholly covers. A direct session has none."""
        decision, mode = self._layer_session(
            session_id, action=GRAPH_ACTION_READ, target="get_staged_changes"
        )
        if decision is None:
            return mode
        if mode != "staged":
            return {
                "success": True,
                "session_id": session_id,
                "write_mode": mode,
                "items": [],
                "count": 0,
            }
        try:
            review = self._load_review(session_id, decision.graph_access)
        except Exception as exc:
            refused = refusal_result(exc)
            if refused is None:
                raise
            return refused
        items = review.items()
        return {
            "success": True,
            "session_id": session_id,
            "write_mode": mode,
            "items": items,
            "count": len(items),
        }

    def merge_staged_changes(
        self,
        session_id: str,
        merge_id: Any,
        entries: Any = "all",
        resolutions: Any = None,
    ) -> Dict[str, Any]:
        """Merge staged entries into the graph as one unit (§11).

        ``entries`` is ``all`` or a list of ``{kind, entity_id}``;
        ``resolutions`` a list of ``{kind, entity_id, choice, values?}``;
        ``merge_id`` the caller's id for the merge, which makes a repeat
        return the first result (I9). Graph events fire as for direct
        writes, with ``event_origin`` ``session-merge`` and the merge id as
        correlation id."""
        decision, mode = self._layer_session(
            session_id,
            action=GRAPH_ACTION_MUTATE,
            target="merge_staged_changes",
            operation=GRAPH_OPERATION_MERGE,
        )
        if decision is None:
            return mode
        try:
            layer_store.check_merge_id(merge_id)
        except layer_store.InvalidLayerRequest as exc:
            return {
                "success": False,
                "error": staged_review.INVALID_MERGE_REQUEST,
                "message": f"merge_id: {exc}",
            }
        if mode != "staged":
            return self._not_staged(session_id)
        manager = self._session_gate.session_manager
        graph_access = decision.graph_access
        planned: Dict[str, Any] = {}

        def prepare():
            review = self._load_review(session_id, graph_access)
            selected = staged_review.normalise_selection(review, entries)
            chosen = staged_review.normalise_resolutions(resolutions, selected)
            plan, results = staged_review.plan_merge(
                review,
                selected,
                chosen,
                actor=access.build_mutation_actor_id(target="merge_staged_changes"),
                merge_id=merge_id,
            )
            planned["results"] = results
            planned["plan"] = plan
            planned["leftovers"] = attachments.leftover_attachments_of_absent_nodes(
                self._storage,
                self._attachment_settings,
                [
                    w.entity_id
                    for w in plan.writes
                    if w.kind == "node" and w.expect_absent
                ],
            )
            return plan

        event_context = access.build_event_context(
            target="merge_staged_changes",
            event_origin=MERGE_EVENT_ORIGIN,
            event_correlation_id=merge_id,
        )
        try:
            manager.check_layer_op(session_id)
            landed = self._storage.apply_session_merge(
                session_id, merge_id, prepare, event_context
            )
        except staged_review.ReviewRefused as exc:
            return exc.to_result()
        except layer_store.MergeRaced as exc:
            return {
                "success": False,
                "error": exc.code,
                "message": "the graph or the layer changed since the merge was "
                "decided; read the diff again",
            }
        except layer_store.MergeIdTaken:
            return {
                "success": False,
                "error": MERGE_ID_TAKEN,
                "message": "this merge id is already used; choose another",
            }
        except mutations._WRITE_ERRORS as exc:
            return mutations.write_error_result(exc)
        except ValueError as exc:
            if isinstance(exc, layer_store.InvalidLayerRequest):
                refused = refusal_result(exc)
                if refused is not None:
                    return refused
            return {
                "success": False,
                "error": "validation_error",
                "message": f"Error validating input: {exc}",
            }
        except Exception as exc:
            refused = refusal_result(exc)
            if refused is None:
                raise
            return refused

        if landed.replayed:
            return {
                "success": True,
                "session_id": session_id,
                "merge_id": merge_id,
                "replayed": True,
                "merged": self._replayed_items(landed.record, graph_access),
            }
        plan = planned["plan"]
        results = planned["results"]
        for item in results:
            target = item.get("new_entity_id") or item["entity_id"]
            item["revision"] = landed.revisions.get((item["kind"], target))
        deleted_nodes = [
            w.entity_id
            for w in plan.writes
            if w.kind == "node" and w.action == "delete"
        ]
        created_nodes = [
            w.entity_id for w in plan.writes if w.kind == "node" and w.expect_absent
        ]
        attachments.quarantine_leftover_attachments(
            self._attachment_settings, planned["leftovers"], created_nodes
        )
        attachments.delete_attachments_of_nodes(
            self._attachment_settings, deleted_nodes
        )
        applied = None
        if plan.removals:
            applied = manager.announce_layer_change(
                session_id,
                {
                    "op": "graph_changes_merged",
                    "merge_id": merge_id,
                    "removals": [list(key) for key in plan.removals],
                },
            )
        return access.attach_mutation_attribution(
            {
                "success": True,
                "session_id": session_id,
                "merge_id": merge_id,
                "replayed": False,
                "merged": results,
                "removed_entries": [list(key) for key in plan.removals],
                "session_seq": None if applied is None else applied["seq"],
            },
            event_context,
        )

    def _replayed_items(self, record: Dict[str, Any], graph_access) -> List[Dict]:
        """A replayed merge's items, as far as the caller's view covers them
        now: an entity it cannot see, or one that no longer exists, is left
        out rather than named."""
        revisions = {
            (a.get("kind"), a.get("entity_id")): a.get("revision")
            for a in record.get("applied") or []
        }
        items = []
        for item in record.get("entries") or []:
            item = dict(item)
            target = item.get("new_entity_id") or item.get("entity_id")
            item["revision"] = revisions.get((item.get("kind"), target))
            if graph_access.enabled:
                ids = [target] if item.get("kind") == "node" else []
                if item.get("kind") == "edge":
                    edge = self._storage.edges.get(target)
                    ids = [] if edge is None else [edge.source, edge.target]
                if not ids or not all(
                    access.is_node_visible(self._storage.get_node(nid), graph_access)
                    for nid in ids
                ):
                    continue
            items.append(item)
        return items

    def discard_staged_changes(
        self, session_id: str, entries: Any = "all"
    ) -> Dict[str, Any]:
        """Remove staged entries from the layer (§12): ``all`` - every entry
        the caller's view covers - or a list of ``{kind, entity_id}``. The
        graph does not change and no graph event fires (I10)."""
        decision, mode = self._layer_session(
            session_id,
            action=GRAPH_ACTION_MUTATE,
            target="discard_staged_changes",
            operation=GRAPH_OPERATION_DISCARD,
        )
        if decision is None:
            return mode
        if mode != "staged":
            return self._not_staged(session_id)
        manager = self._session_gate.session_manager
        try:
            manager.check_layer_op(session_id)
            removed = self._discard_in_view(session_id, entries, decision.graph_access)
        except staged_review.ReviewRefused as exc:
            return exc.to_result()
        except Exception as exc:
            refused = refusal_result(exc)
            if refused is None:
                raise
            return refused
        applied = None
        if removed:
            applied = manager.announce_layer_change(
                session_id,
                {
                    "op": "graph_changes_discarded",
                    "removals": [list(key) for key in removed],
                },
            )
        return {
            "success": True,
            "session_id": session_id,
            "removed_entries": [list(key) for key in removed],
            "session_seq": None if applied is None else applied["seq"],
        }

    def _discard_in_view(self, session_id: str, entries: Any, graph_access):
        """Remove the selected entries the caller's view covers, each only
        while it is still the entry the caller's review read: one restaged
        since - possibly out of that view - refuses the discard in the store,
        and it is decided again on the layer as it is then."""
        store = self._session_gate.session_manager.write_modes.layer_store
        for attempt in range(self.STAGING_ATTEMPTS):
            review = self._load_review(session_id, graph_access)
            if entries == "all":
                keys = sorted(review.in_view)
            else:
                try:
                    keys = staged_review.normalise_selection(review, entries)
                except staged_review.ReviewRefused as exc:
                    if exc.code != staged_review.MERGE_RACED:
                        raise
                    # Gone already, or never the caller's to see: either way
                    # there is nothing of it to discard.
                    keys = [
                        key
                        for key in staged_review.requested_keys(entries)
                        if key in review.in_view
                    ]
            if not keys:
                return []
            expected = {key: review.entries[key].get("session_seq") for key in keys}
            try:
                return store.discard(session_id, keys, expected=expected)
            except layer_store.LayerRaced:
                if attempt + 1 == self.STAGING_ATTEMPTS:
                    raise LayoutBusy()
        raise AssertionError("unreachable")  # pragma: no cover

    @property
    def storage(self) -> GraphStorage:
        """Access the underlying storage (for advanced use cases)."""
        return self._storage

    @property
    def authorization_hook(self) -> GraphAuthorizationHook:
        """The active authorization hook.

        Exposed so surfaces that authorize outside the node/edge CRUD path
        (e.g. the MCP visualization-session tools) evaluate against the *same*
        hook instance the hosted layer swaps in, rather than a fresh default.
        """
        return self._authorization_hook

    @property
    def attachment_settings(self) -> Optional[AttachmentSettings]:
        return self._attachment_settings

    # ==================== Node Attachments ====================
    # See backend/service/attachments.py and ADR 0008.

    def list_node_attachments(self, node_id: str) -> Dict[str, Any]:
        return attachments.list_node_attachments(
            self._storage, self._authorization_hook, self._attachment_settings, node_id
        )

    def get_node_attachment_record(
        self, node_id: str, attachment_id: str
    ) -> Dict[str, Any]:
        return attachments.get_node_attachment_record(
            self._storage,
            self._authorization_hook,
            self._attachment_settings,
            node_id,
            attachment_id,
        )

    def open_node_attachment(self, node_id: str, attachment_id: str) -> Dict[str, Any]:
        return attachments.open_node_attachment(
            self._storage,
            self._authorization_hook,
            self._attachment_settings,
            node_id,
            attachment_id,
        )

    async def add_node_attachment(
        self,
        node_id: str,
        *,
        filename: Optional[str],
        declared_content_type: Optional[str],
        content: Union[bytes, AsyncIterator[bytes]],
        content_length: Optional[int] = None,
    ) -> Dict[str, Any]:
        return await attachments.add_node_attachment(
            self._storage,
            self._authorization_hook,
            self._attachment_settings,
            node_id,
            filename=filename,
            declared_content_type=declared_content_type,
            content=content,
            content_length=content_length,
        )

    def delete_node_attachment(
        self, node_id: str, attachment_id: str
    ) -> Dict[str, Any]:
        return attachments.delete_node_attachment(
            self._storage,
            self._authorization_hook,
            self._attachment_settings,
            node_id,
            attachment_id,
        )

    # ==================== Search Operations ====================

    def search_graph(
        self,
        query: str,
        node_types: Optional[List[str]] = None,
        limit: int = 50,
        action: Optional[str] = None,
        federation_depth: Optional[int] = None,
        tags_any: Optional[List[str]] = None,
        tags_all: Optional[List[str]] = None,
        tags_none: Optional[List[str]] = None,
        metadata_filters: Optional[List[Dict[str, Any]]] = None,
        include_archived: bool = False,
        semantic: bool = False,
        match_mode: str = MATCH_MODE_SUBSTRING,
    ) -> Dict[str, Any]:
        return self._read(
            "search_graph",
            lambda storage: queries.search_graph(
                storage,
                self._federation_manager,
                self._authorization_hook,
                query,
                node_types=node_types,
                limit=limit,
                action=action,
                federation_depth=federation_depth,
                tags_any=tags_any,
                tags_all=tags_all,
                tags_none=tags_none,
                metadata_filters=metadata_filters,
                include_archived=include_archived,
                semantic=semantic,
                match_mode=match_mode,
            ),
            nodes=[],
            edges=[],
        )

    def get_node_details(self, node_id: str) -> Dict[str, Any]:
        return self._read(
            "get_node_details",
            lambda storage: queries.get_node_details(
                storage, self._authorization_hook, node_id
            ),
        )

    def get_related_nodes(
        self,
        node_id: str,
        relationship_types: Optional[List[str]] = None,
        depth: int = 1,
        include_archived: bool = False,
    ) -> Dict[str, Any]:
        return self._read(
            "get_related_nodes",
            lambda storage: queries.get_related_nodes(
                storage,
                self._authorization_hook,
                node_id,
                relationship_types=relationship_types,
                depth=depth,
                include_archived=include_archived,
            ),
            nodes=[],
            edges=[],
        )

    def list_typed_nodes(
        self,
        node_type: str,
        tags_all: Optional[List[str]] = None,
        tags_any: Optional[List[str]] = None,
        subtypes_any: Optional[List[str]] = None,
        limit: int = 500,
        include_archived: bool = False,
    ) -> Dict[str, Any]:
        return self._read(
            f"list_typed_nodes:{node_type}",
            lambda storage: queries.list_typed_nodes(
                storage,
                self._authorization_hook,
                node_type=node_type,
                tags_all=tags_all,
                tags_any=tags_any,
                subtypes_any=subtypes_any,
                limit=limit,
                include_archived=include_archived,
            ),
            nodes=[],
        )

    def list_typed_edges(
        self,
        edge_type: str,
        tags_all: Optional[List[str]] = None,
        tags_any: Optional[List[str]] = None,
        limit: int = 500,
        include_archived: bool = False,
    ) -> Dict[str, Any]:
        return self._read(
            f"list_typed_edges:{edge_type}",
            lambda storage: queries.list_typed_edges(
                storage,
                self._authorization_hook,
                edge_type=edge_type,
                tags_all=tags_all,
                tags_any=tags_any,
                limit=limit,
                include_archived=include_archived,
            ),
            edges=[],
        )

    # ==================== Similarity Operations ====================

    def find_similar_nodes(
        self,
        name: str,
        node_type: Optional[str] = None,
        threshold: float = 0.7,
        limit: int = 5,
    ) -> Dict[str, Any]:
        return self._read(
            "find_similar_nodes",
            lambda storage: queries.find_similar_nodes(
                storage,
                self._authorization_hook,
                name,
                node_type=node_type,
                threshold=threshold,
                limit=limit,
            ),
        )

    def find_similar_nodes_batch(
        self,
        names: List[str],
        node_type: Optional[str] = None,
        threshold: float = 0.7,
        limit: int = 5,
    ) -> Dict[str, Any]:
        return self._read(
            "find_similar_nodes_batch",
            lambda storage: queries.find_similar_nodes_batch(
                storage,
                self._authorization_hook,
                names,
                node_type=node_type,
                threshold=threshold,
                limit=limit,
            ),
        )

    # ==================== CRUD Operations ====================

    def add_nodes(
        self,
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
        expect_absent: bool = False,
        expected_session_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        def run(staging: Optional[StagedWrite]) -> Dict[str, Any]:
            if staging is not None:
                # Nothing lands in the graph, so no attachment changes hands.
                return mutations.add_nodes(
                    self._storage,
                    self._authorization_hook,
                    nodes,
                    edges,
                    expect_absent=expect_absent,
                    staging=staging,
                )
            # Files left under an id whose node was deleted must not become the
            # new node's (ADR 0008 sec 8): note them now, quarantine them once
            # the node is in the graph.
            leftovers = attachments.leftover_attachments_of_absent_nodes(
                self._storage,
                self._attachment_settings,
                [
                    n["id"]
                    for n in nodes
                    if isinstance(n, dict) and isinstance(n.get("id"), str)
                ],
            )
            return mutations.add_nodes(
                self._storage,
                self._authorization_hook,
                nodes,
                edges,
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
                expect_absent=expect_absent,
                on_nodes_landed=lambda landed: (
                    attachments.quarantine_leftover_attachments(
                        self._attachment_settings, leftovers, landed
                    )
                ),
            )

        return self._write(
            "add_nodes",
            expected_session_seq,
            run,
            added_node_ids=[],
            added_edge_ids=[],
        )

    def resolve_teleport(
        self,
        node_id: str,
        session_id: str = "",
        search_query: str = "",
        local_gui_url: str = "",
    ) -> Dict[str, Any]:
        return queries.resolve_teleport(
            self._storage,
            self._authorization_hook,
            self._federation_manager,
            node_id,
            session_id=session_id,
            search_query=search_query,
            local_gui_url=local_gui_url,
        )

    def adopt_federated_node(
        self,
        federated_node_id: str,
        local_name: Optional[str] = None,
        relationship_type: str = "ADOPTED_FROM",
        create_new_copy: bool = False,
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
        expected_session_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._write(
            "adopt_federated_node",
            expected_session_seq,
            lambda staging: mutations.adopt_federated_node(
                self._storage,
                self._federation_manager,
                self._authorization_hook,
                federated_node_id,
                local_name=local_name,
                relationship_type=relationship_type,
                create_new_copy=create_new_copy,
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
                staging=staging,
            ),
            added_node_ids=[],
            added_edge_ids=[],
        )

    def update_node(
        self,
        node_id: str,
        updates: Dict[str, Any],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
        metadata_merge: bool = False,
        expected_updated_at: Optional[str] = None,
        expected_revision: Optional[int] = None,
        expected_session_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._write(
            "update_node",
            expected_session_seq,
            lambda staging: mutations.update_node(
                self._storage,
                self._authorization_hook,
                node_id,
                updates,
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
                metadata_merge=metadata_merge,
                expected_updated_at=expected_updated_at,
                expected_revision=expected_revision,
                staging=staging,
            ),
        )

    def delete_nodes(
        self,
        node_ids: List[str],
        confirmed: bool = False,
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
        expected_revisions: Optional[Dict[str, int]] = None,
        expected_session_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        def run(staging: Optional[StagedWrite]) -> Dict[str, Any]:
            result = mutations.delete_nodes(
                self._storage,
                self._authorization_hook,
                node_ids,
                confirmed=confirmed,
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
                expected_revisions=expected_revisions,
                staging=staging,
            )
            if staging is None:
                # After the graph delete, never before: a crash in between
                # leaves an orphan attachment directory, not a live node
                # without its files. A staged delete removes nothing (I1).
                attachments.delete_attachments_of_nodes(
                    self._attachment_settings, result.get("deleted_node_ids") or []
                )
            return result

        return self._write(
            "delete_nodes",
            expected_session_seq,
            run,
            deleted_node_ids=[],
            affected_edge_ids=[],
        )

    def archive_nodes(
        self,
        node_ids: List[str],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
        expected_session_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._write(
            "archive_nodes",
            expected_session_seq,
            lambda staging: mutations.set_nodes_archived(
                self._storage,
                self._authorization_hook,
                node_ids,
                True,
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
                staging=staging,
            ),
            node_ids=[],
        )

    def unarchive_nodes(
        self,
        node_ids: List[str],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
        expected_session_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._write(
            "archive_nodes",
            expected_session_seq,
            lambda staging: mutations.set_nodes_archived(
                self._storage,
                self._authorization_hook,
                node_ids,
                False,
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
                staging=staging,
            ),
            node_ids=[],
        )

    # ==================== Edge CRUD Operations ====================

    def add_edge(
        self,
        source: str,
        target: str,
        type: Optional[str] = None,
        label: Optional[str] = None,
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
        expected_session_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._write(
            "add_edge",
            expected_session_seq,
            lambda staging: mutations.add_edge(
                self._storage,
                self._authorization_hook,
                source,
                target,
                type=type,
                label=label,
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
                staging=staging,
            ),
        )

    def update_edge(
        self,
        edge_id: str,
        updates: Dict[str, Any],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
        expected_session_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._write(
            "update_edge",
            expected_session_seq,
            lambda staging: mutations.update_edge(
                self._storage,
                self._authorization_hook,
                edge_id,
                updates,
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
                expected_revision=expected_revision,
                staging=staging,
            ),
        )

    def delete_edge(
        self,
        edge_id: str,
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
        expected_revision: Optional[int] = None,
        expected_session_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._write(
            "delete_edge",
            expected_session_seq,
            lambda staging: mutations.delete_edge(
                self._storage,
                self._authorization_hook,
                edge_id,
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
                expected_revision=expected_revision,
                staging=staging,
            ),
        )

    def delete_edges(
        self,
        edge_ids: List[str],
        confirmed: bool = False,
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
        expected_session_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._write(
            "delete_edges",
            expected_session_seq,
            lambda staging: mutations.delete_edges(
                self._storage,
                self._authorization_hook,
                edge_ids,
                confirmed=confirmed,
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
                staging=staging,
            ),
            deleted_edge_ids=[],
        )

    def archive_edges(
        self,
        edge_ids: List[str],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
        expected_session_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._write(
            "archive_edges",
            expected_session_seq,
            lambda staging: mutations.set_edges_archived(
                self._storage,
                self._authorization_hook,
                edge_ids,
                True,
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
                staging=staging,
            ),
            edge_ids=[],
        )

    def unarchive_edges(
        self,
        edge_ids: List[str],
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
        expected_session_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._write(
            "archive_edges",
            expected_session_seq,
            lambda staging: mutations.set_edges_archived(
                self._storage,
                self._authorization_hook,
                edge_ids,
                False,
                event_origin=event_origin,
                event_session_id=event_session_id,
                event_correlation_id=event_correlation_id,
                staging=staging,
            ),
            edge_ids=[],
        )

    # ==================== Statistics & Metadata ====================

    def get_graph_stats(self) -> Dict[str, Any]:
        return self._read(
            "get_graph_stats",
            lambda storage: queries.get_graph_stats(
                storage, self._federation_manager, self._authorization_hook
            ),
        )

    # ==================== History Operations ====================

    def get_graph_history(self, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        return queries.get_graph_history(
            self._storage, self._authorization_hook, limit=limit, offset=offset
        )

    def get_node_history(
        self, node_id: str, limit: int = 50, offset: int = 0
    ) -> Dict[str, Any]:
        return queries.get_node_history(
            self._storage, self._authorization_hook, node_id, limit=limit, offset=offset
        )

    def get_edge_history(
        self, edge_id: str, limit: int = 50, offset: int = 0
    ) -> Dict[str, Any]:
        return queries.get_edge_history(
            self._storage, self._authorization_hook, edge_id, limit=limit, offset=offset
        )

    def list_node_types(self) -> Dict[str, Any]:
        return queries.list_node_types()

    def get_subtypes(self, node_type: str = None) -> Dict[str, Any]:
        return self._read(
            "get_subtypes",
            lambda storage: queries.get_subtypes(
                storage, self._authorization_hook, node_type=node_type
            ),
        )

    def list_relationship_types(self) -> Dict[str, Any]:
        return queries.list_relationship_types()

    def audit_relationship_applicability(self) -> Dict[str, Any]:
        return self._read(
            "audit_relationship_applicability",
            lambda storage: queries.audit_relationship_applicability(
                storage, self._authorization_hook
            ),
        )

    def get_schema(self) -> Dict[str, Any]:
        return queries.get_schema()

    def get_presentation(self) -> Dict[str, Any]:
        return queries.get_presentation()

    def get_capabilities(self) -> Dict[str, Any]:
        return queries.get_capabilities()

    def get_runtime_info(self) -> Dict[str, Any]:
        return queries.get_runtime_info()

    def get_tenant_context(self) -> Dict[str, Any]:
        return queries.get_tenant_context()

    def get_config_context(self) -> Dict[str, Any]:
        return queries.get_config_context()

    def get_request_actor_info(
        self,
        *,
        headers: Optional[Dict[str, Any]] = None,
        actor_id: Optional[str] = None,
        actor_type: Optional[str] = None,
        auth_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        return queries.get_request_actor_info(
            headers=headers,
            actor_id=actor_id,
            actor_type=actor_type,
            auth_source=auth_source,
        )

    def get_request_scope_info(
        self,
        *,
        headers: Optional[Dict[str, Any]] = None,
        workspace_id: Optional[str] = None,
        workspace_kind: Optional[str] = None,
        graph_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return queries.get_request_scope_info(
            headers=headers,
            workspace_id=workspace_id,
            workspace_kind=workspace_kind,
            graph_id=graph_id,
        )

    def get_request_graph_selection_info(
        self,
        *,
        headers: Optional[Dict[str, Any]] = None,
        workspace_id: Optional[str] = None,
        workspace_kind: Optional[str] = None,
        graph_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return queries.get_request_graph_selection_info(
            headers=headers,
            workspace_id=workspace_id,
            workspace_kind=workspace_kind,
            graph_id=graph_id,
        )

    # ==================== Saved Views ====================

    def save_view(self, name: str) -> Dict[str, Any]:
        return views.save_view(name)

    def resolve_session_nodes(self, node_ids: List[str]) -> Dict[str, Any]:
        return self._read(
            "resolve_session_nodes",
            lambda storage: views.resolve_session_nodes(
                storage, self._authorization_hook, node_ids
            ),
            nodes=[],
            edges=[],
        )

    def resolve_session_node_semantics(
        self,
        node_ids: List[str],
        *,
        action: str = GRAPH_ACTION_READ,
        target: str = "resolve_session_node_semantics",
    ) -> Dict[str, Any]:
        """Resolve session node references to their type and status.

        ``action``/``target`` are required on the underlying helper, so that it
        can never pick a caller's authorization scope for it. They keep defaults
        *here* on purpose: this is a public method, and these two values are
        exactly what it evaluated before they were parameters, so an existing
        caller outside this repo is unaffected. Every caller in this repo passes
        both explicitly, and a new one should too.
        """
        return self._read(
            target,
            action=action,
            run=lambda storage: views.resolve_session_node_semantics(
                storage,
                self._authorization_hook,
                node_ids,
                action=action,
                target=target,
            ),
        )

    def get_saved_view(self, name: str) -> Dict[str, Any]:
        return self._read(
            "get_saved_view",
            lambda storage: views.get_saved_view(
                storage, self._authorization_hook, name
            ),
        )

    def list_saved_views(self) -> Dict[str, Any]:
        return self._read(
            "list_saved_views",
            lambda storage: views.list_saved_views(storage, self._authorization_hook),
        )

    # ==================== Export ====================

    def export_graph(self) -> Dict[str, Any]:
        return self._read(
            "export_graph",
            lambda storage: views.export_graph(storage, self._authorization_hook),
        )

    def export_graph_archive(self) -> Dict[str, Any]:
        """Build the vector-aware export archive (ZIP). See
        ``backend.service.graph_archive`` and
        docs/adr/0007-vector-aware-export-archive.md."""
        return views.export_graph_archive(self._storage, self._authorization_hook)

    # ==================== Import ====================

    def import_graph(
        self,
        document: Any,
        execution_store: "ExecutionStore",
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Whole-graph REPLACE import. See ``backend.service.import_service`` for
        the full validate -> backup -> replace -> enqueue-embeddings sequence.

        ``execution_store`` is passed explicitly rather than owned by
        ``GraphService`` — the durable job queue backing import embeddings is
        wired up by the host application (see ``backend/api_host/server.py``),
        the same way ``session_manager`` is passed to ``create_rest_router``
        rather than constructed here.
        """
        return import_service.import_graph(
            self._storage,
            self._authorization_hook,
            execution_store,
            document,
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    def import_graph_archive(
        self,
        archive_bytes: bytes,
        execution_store: "ExecutionStore",
        event_origin: Optional[str] = None,
        event_session_id: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Whole-graph REPLACE import from a vector-aware export archive (ZIP).
        See ``backend.service.import_service.import_graph_archive`` and
        docs/adr/0007-vector-aware-export-archive.md.
        """
        return import_service.import_graph_archive(
            self._storage,
            self._authorization_hook,
            execution_store,
            archive_bytes,
            event_origin=event_origin,
            event_session_id=event_session_id,
            event_correlation_id=event_correlation_id,
        )

    def preview_merge_import(
        self,
        body: Any,
        max_nodes: Optional[int] = None,
        max_edges: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Read-only MERGE import plan and digest. See
        ``backend.service.import_service.preview_merge_import`` and
        docs/adr/0013-graph-import-merge-mode.md.
        """
        return import_service.preview_merge_import(
            self._storage,
            self._authorization_hook,
            body,
            attachment_settings=self._attachment_settings,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )

    def commit_merge_import(
        self,
        body: Any,
        execution_store: "ExecutionStore",
        event_origin: Optional[str] = None,
        event_correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Commit a MERGE import bound to a previewed plan. See
        ``backend.service.import_service.commit_merge_import`` and
        docs/adr/0013-graph-import-merge-mode.md.
        """
        return import_service.commit_merge_import(
            self._storage,
            self._authorization_hook,
            execution_store,
            body,
            attachment_settings=self._attachment_settings,
            event_origin=event_origin,
            event_correlation_id=event_correlation_id,
        )

    def get_import_job(
        self, execution_store: "ExecutionStore", job_id: str
    ) -> Optional[Dict[str, Any]]:
        return import_service.get_import_job(execution_store, job_id)

    def list_import_jobs(
        self, execution_store: "ExecutionStore", limit: int = 100
    ) -> List[Dict[str, Any]]:
        return import_service.list_import_jobs(execution_store, limit=limit)
