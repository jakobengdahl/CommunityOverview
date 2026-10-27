"""
Tests for event dispatcher.
"""

from typing import List, Dict

from backend.core.events.models import (
    Event,
    EventType,
    EntityKind,
    EventContext,
    EntityData,
)
from backend.core.events.dispatcher import (
    EventDispatcher,
    _copy_payload,
    _repair_legacy_agent_delivery,
)


class MockNode:
    """Mock node for testing."""

    def __init__(self, id: str, name: str, node_type: str, metadata: Dict = None):
        self.id = id
        self.name = name
        self.type = node_type
        self.metadata = metadata or {}


class MockStorage:
    """Mock storage for testing."""

    def __init__(self, nodes: List[MockNode] = None):
        self.nodes = {n.id: n for n in (nodes or [])}


def create_subscription_node(
    id: str,
    name: str,
    node_types: List[str] = None,
    operations: List[str] = None,
    keywords: List[str] = None,
    webhook_url: str = "https://example.com/hook",
    ignore_origins: List[str] = None,
    ignore_session_ids: List[str] = None,
    federation_scope: str = "local_only",
    include_graph_ids: List[str] = None,
    max_distance: int = None,
) -> MockNode:
    """Helper to create a subscription node for testing."""
    metadata = {
        "filters": {
            "target": {
                "entity_kind": "node",
                "node_types": node_types or [],
            },
            "operations": operations or ["create", "update", "delete"],
            "keywords": {
                "any": keywords or [],
            },
            "federation": {
                "scope": federation_scope,
                "include_graph_ids": include_graph_ids or [],
                "max_distance": max_distance,
            },
        },
        "delivery": {
            "webhook_url": webhook_url,
            "ignore_origins": ignore_origins or [],
            "ignore_session_ids": ignore_session_ids or [],
        },
    }
    return MockNode(id, name, "EventSubscription", metadata)


def create_event(
    event_type: EventType = EventType.NODE_CREATE,
    entity_id: str = "node-1",
    entity_type: str = "Actor",
    event_origin: str = None,
    event_session_id: str = None,
    before: Dict = None,
    after: Dict = None,
) -> Event:
    """Helper to create an event for testing."""
    return Event(
        event_type=event_type,
        origin=EventContext(
            event_origin=event_origin,
            event_session_id=event_session_id,
        ),
        entity=EntityData(
            kind=EntityKind.NODE,
            id=entity_id,
            type=entity_type,
            before=before,
            after=after or {"name": "Test", "type": entity_type},
        ),
    )


class TestDispatcherFiltering:
    """Tests for event filtering logic."""

    def test_matches_all_events_with_empty_filters(self):
        """Test that empty filters match all events."""
        sub = create_subscription_node("sub-1", "All Events")
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(EventType.NODE_CREATE, entity_type="Actor")
        dispatcher.dispatch(event)

        assert len(delivered) == 1

    def test_matches_specific_node_type(self):
        """Test filtering by node type."""
        sub = create_subscription_node("sub-1", "Actor Events", node_types=["Actor"])
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        # Should match
        event1 = create_event(EventType.NODE_CREATE, entity_type="Actor")
        dispatcher.dispatch(event1)
        assert len(delivered) == 1

        # Should not match
        event2 = create_event(EventType.NODE_CREATE, entity_type="Initiative")
        dispatcher.dispatch(event2)
        assert len(delivered) == 1  # Still 1

    def test_matches_multiple_node_types(self):
        """Test filtering by multiple node types."""
        sub = create_subscription_node(
            "sub-1", "Actor/Initiative Events", node_types=["Actor", "Initiative"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event1 = create_event(entity_type="Actor")
        event2 = create_event(entity_type="Initiative")
        event3 = create_event(entity_type="Resource")

        dispatcher.dispatch(event1)
        dispatcher.dispatch(event2)
        dispatcher.dispatch(event3)

        assert len(delivered) == 2  # Actor and Initiative only

    def test_matches_specific_operations(self):
        """Test filtering by operation type."""
        sub = create_subscription_node("sub-1", "Create Only", operations=["create"])
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event_create = create_event(EventType.NODE_CREATE)
        event_update = create_event(EventType.NODE_UPDATE)
        event_delete = create_event(EventType.NODE_DELETE)

        dispatcher.dispatch(event_create)
        dispatcher.dispatch(event_update)
        dispatcher.dispatch(event_delete)

        assert len(delivered) == 1  # Only create


class TestKeywordMatching:
    """Tests for keyword matching in events."""

    def test_matches_keyword_in_name(self):
        """Test keyword matching in name field."""
        sub = create_subscription_node("sub-1", "AI Events", keywords=["AI"])
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(after={"name": "AI Strategy", "type": "Initiative"})
        dispatcher.dispatch(event)

        assert len(delivered) == 1

    def test_matches_keyword_in_description(self):
        """Test keyword matching in description field."""
        sub = create_subscription_node("sub-1", "AI Events", keywords=["artificiell"])
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(
            after={
                "name": "Tech Project",
                "description": "Projekt om artificiell intelligens",
                "type": "Initiative",
            }
        )
        dispatcher.dispatch(event)

        assert len(delivered) == 1

    def test_keyword_matching_is_case_insensitive(self):
        """Test that keyword matching is case insensitive."""
        sub = create_subscription_node(
            "sub-1", "AI Events", keywords=["digitalisering"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(
            after={"name": "DIGITALISERING Initiativ", "type": "Initiative"}
        )
        dispatcher.dispatch(event)

        assert len(delivered) == 1

    def test_no_match_without_keyword(self):
        """Test that events without keywords don't match."""
        sub = create_subscription_node("sub-1", "AI Events", keywords=["blockchain"])
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(after={"name": "AI Project", "type": "Initiative"})
        dispatcher.dispatch(event)

        assert len(delivered) == 0


class TestLoopPrevention:
    """Tests for loop prevention logic."""

    def test_blocks_ignored_origin(self):
        """Test that events from ignored origins are blocked."""
        sub = create_subscription_node(
            "sub-1", "Subscription", ignore_origins=["agent:my-agent"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(event_origin="agent:my-agent")
        dispatcher.dispatch(event)

        assert len(delivered) == 0

    def test_allows_non_ignored_origin(self):
        """Test that events from non-ignored origins pass."""
        sub = create_subscription_node(
            "sub-1", "Subscription", ignore_origins=["agent:my-agent"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(event_origin="web-ui")
        dispatcher.dispatch(event)

        assert len(delivered) == 1

    def test_blocks_ignored_session_id(self):
        """Test that events with ignored session IDs are blocked."""
        sub = create_subscription_node(
            "sub-1", "Subscription", ignore_session_ids=["session-123"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(event_session_id="session-123")
        dispatcher.dispatch(event)

        assert len(delivered) == 0

    def test_allows_events_without_origin(self):
        """Test that events without origin info are allowed (PoC default)."""
        sub = create_subscription_node(
            "sub-1", "Subscription", ignore_origins=["agent:my-agent"]
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event()  # No origin
        dispatcher.dispatch(event)

        assert len(delivered) == 1


def _agent_node(agent_id: str, subscription_id: str) -> MockNode:
    return MockNode(agent_id, "Agent", "Agent", {"subscription_id": subscription_id})


def _agent_deliveries(dispatcher: EventDispatcher) -> list:
    delivered = []
    dispatcher.set_delivery_callback(lambda e, u: None)
    dispatcher.set_agent_delivery_callback(
        lambda e, sub_id: delivered.append((e.origin.event_origin, sub_id)) or True
    )
    return delivered


class TestLegacyAgentLoopGuardRepair:
    """A UI-created agent stored before its loop guard used the agent node id
    (``agent:<prefix>`` instead of ``agent:<prefix>-node``) is repaired when
    the dispatcher reads its subscription."""

    LEGACY = {
        "webhook_url": "internal://agent/p",
        "ignore_origins": ["agent:p"],
        "ignore_session_ids": [],
    }

    def test_a_legacy_agent_is_not_woken_by_its_own_write(self):
        sub = create_subscription_node(
            "p-subscription",
            "P - Subscription",
            webhook_url="internal://agent/p",
            ignore_origins=["agent:p"],
        )
        storage = MockStorage([sub, _agent_node("p-node", "p-subscription")])
        dispatcher = EventDispatcher(storage)
        delivered = _agent_deliveries(dispatcher)

        dispatcher.dispatch(create_event(event_origin="agent:p-node"))
        dispatcher.dispatch(create_event(event_origin="web-ui"))

        assert delivered == [("web-ui", "p-subscription")]
        # Repaired on read only: the stored subscription is not rewritten.
        assert sub.metadata["delivery"]["ignore_origins"] == ["agent:p"]
        assert sub.metadata["delivery"]["webhook_url"] == "internal://agent/p"

    def test_the_legacy_guard_and_internal_url_take_the_node_id_form(self):
        repaired = _repair_legacy_agent_delivery(
            "p-subscription", "p-node", self.LEGACY
        )

        assert repaired == {
            "webhook_url": "internal://agent/p-node",
            "ignore_origins": ["agent:p", "agent:p-node"],
            "ignore_session_ids": [],
        }
        assert (
            _repair_legacy_agent_delivery("p-subscription", "p-node", repaired)
            == repaired
        )
        assert self.LEGACY["ignore_origins"] == ["agent:p"]

    def test_an_origin_sharing_the_legacy_prefix_is_kept_and_does_not_count(self):
        sibling = {**self.LEGACY, "ignore_origins": ["agent:p", "agent:p-other"]}
        assert _repair_legacy_agent_delivery("p-subscription", "p-node", sibling)[
            "ignore_origins"
        ] == ["agent:p", "agent:p-other", "agent:p-node"]

        only_sibling = {**self.LEGACY, "ignore_origins": ["agent:p-other"]}
        assert _repair_legacy_agent_delivery("p-subscription", "p-node", only_sibling)[
            "ignore_origins"
        ] == ["agent:p-other"]

    def test_a_correctly_formed_agent_delivery_is_unchanged(self):
        delivery = {
            "webhook_url": "internal://agent/p-node",
            "ignore_origins": ["agent:p-node"],
            "ignore_session_ids": ["s"],
        }
        assert (
            _repair_legacy_agent_delivery("p-subscription", "p-node", delivery)
            == delivery
        )

    def test_an_external_webhook_url_is_kept_while_the_guard_is_repaired(self):
        delivery = {
            **self.LEGACY,
            "webhook_url": "https://example.com/internal/agent/p",
        }

        repaired = _repair_legacy_agent_delivery("p-subscription", "p-node", delivery)

        assert repaired["webhook_url"] == "https://example.com/internal/agent/p"
        assert repaired["ignore_origins"] == ["agent:p", "agent:p-node"]

    def test_subscriptions_outside_the_ui_agent_pairing_are_unchanged(self):
        # No linked agent; agent ids without the "-node" suffix, including one
        # that still leaves "p" once five characters are cut; and a UI-style
        # agent linked to a subscription of another prefix.
        for subscription_id, agent_id in [
            ("p-subscription", None),
            ("p-subscription", "p"),
            ("p-subscription", "pxnode"),
            ("q-subscription", "p-node"),
        ]:
            assert (
                _repair_legacy_agent_delivery(subscription_id, agent_id, self.LEGACY)
                == self.LEGACY
            )

    def test_a_paired_agent_without_the_legacy_origin_is_unchanged(self):
        for origins in ([], ["agent:other"], None):
            delivery = {"webhook_url": "internal://agent/p-node"}
            if origins is not None:
                delivery["ignore_origins"] = origins
            assert (
                _repair_legacy_agent_delivery("p-subscription", "p-node", delivery)
                == delivery
            )
        # Malformed: a non-list value is left for the delivery model to judge.
        odd = {"webhook_url": "internal://agent/p-node", "ignore_origins": "agent:p"}
        assert _repair_legacy_agent_delivery("p-subscription", "p-node", odd) == odd

    def test_an_agent_with_a_non_string_subscription_id_does_not_stop_loading(self):
        subs = [
            create_subscription_node(
                "p-subscription",
                "P",
                webhook_url="internal://agent/p",
                ignore_origins=["agent:p"],
            ),
            create_subscription_node("q-subscription", "Q"),
        ]
        agents = [
            _agent_node("p-node", "p-subscription"),
            MockNode("x-node", "X", "Agent", {"subscription_id": ["q-subscription"]}),
            MockNode("y-node", "Y", "Agent", {"subscription_id": {"id": "q"}}),
        ]
        dispatcher = EventDispatcher(MockStorage([*subs, *agents]))

        loaded = {s["id"]: s["delivery"] for s in dispatcher._load_subscriptions()}

        assert loaded["p-subscription"].ignore_origins == ["agent:p", "agent:p-node"]
        assert loaded["q-subscription"].ignore_origins == []

    def test_a_non_agent_subscription_keeps_its_origins_when_dispatched(self):
        sub = create_subscription_node(
            "p-subscription", "Webhook", ignore_origins=["agent:p"]
        )
        dispatcher = EventDispatcher(MockStorage([sub]))
        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append(u))

        dispatcher.dispatch(create_event(event_origin="agent:p-node"))

        assert delivered == ["https://example.com/hook"]


class TestMultipleSubscriptions:
    """Tests for dispatching to multiple subscriptions."""

    def test_dispatches_to_multiple_matching_subscriptions(self):
        """Test that events are dispatched to all matching subscriptions."""
        sub1 = create_subscription_node(
            "sub-1", "All Events", webhook_url="https://hook1.com"
        )
        sub2 = create_subscription_node(
            "sub-2", "All Events Too", webhook_url="https://hook2.com"
        )
        storage = MockStorage([sub1, sub2])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event()
        count = dispatcher.dispatch(event)

        assert count == 2
        assert len(delivered) == 2
        urls = [u for _, u in delivered]
        assert "https://hook1.com" in urls
        assert "https://hook2.com" in urls

    def test_subscription_info_included_in_event(self):
        """Test that subscription info is added to dispatched events."""
        sub = create_subscription_node("sub-1", "My Subscription")
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event()
        dispatcher.dispatch(event)

        delivered_event = delivered[0][0]
        assert delivered_event.subscription is not None
        assert delivered_event.subscription.id == "sub-1"
        assert delivered_event.subscription.name == "My Subscription"

    def test_internal_urls_skipped_for_webhook_delivery(self):
        """Test that internal:// URLs are not sent to webhook delivery callback."""
        sub = create_subscription_node(
            "sub-1", "Internal Agent", webhook_url="internal://agent/agent-1"
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        # Simulate agent delivery failing or not being handled
        dispatcher.set_agent_delivery_callback(lambda e, s: False)

        event = create_event()
        dispatcher.dispatch(event)

        # Should be skipped because URL starts with internal://
        assert len(delivered) == 0


class TestFederationFilterMatching:
    """Tests for federation-specific subscription filters."""

    def test_default_local_only_excludes_federated_events(self):
        sub = create_subscription_node("sub-1", "Local only")
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(
            after={
                "name": "Remote Actor",
                "type": "Actor",
                "metadata": {"origin_graph_id": "esam-main", "federation_distance": 1},
            }
        )
        dispatcher.dispatch(event)

        assert len(delivered) == 0

    def test_local_and_federated_includes_federated_events(self):
        sub = create_subscription_node(
            "sub-1",
            "Local and federated",
            federation_scope="local_and_federated",
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event = create_event(
            after={
                "name": "Remote Actor",
                "type": "Actor",
                "metadata": {"origin_graph_id": "esam-main", "federation_distance": 1},
            }
        )
        dispatcher.dispatch(event)

        assert len(delivered) == 1

    def test_graph_id_and_distance_filters_apply(self):
        sub = create_subscription_node(
            "sub-1",
            "Scoped federated",
            federation_scope="local_and_federated",
            include_graph_ids=["esam-main"],
            max_distance=1,
        )
        storage = MockStorage([sub])
        dispatcher = EventDispatcher(storage)

        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append((e, u)))

        event_ok = create_event(
            after={
                "name": "Remote Actor",
                "type": "Actor",
                "metadata": {"origin_graph_id": "esam-main", "federation_distance": 1},
            }
        )
        event_wrong_graph = create_event(
            after={
                "name": "Remote Actor",
                "type": "Actor",
                "metadata": {
                    "origin_graph_id": "other-graph",
                    "federation_distance": 1,
                },
            }
        )
        event_too_far = create_event(
            after={
                "name": "Remote Actor",
                "type": "Actor",
                "metadata": {"origin_graph_id": "esam-main", "federation_distance": 2},
            }
        )

        dispatcher.dispatch(event_ok)
        dispatcher.dispatch(event_wrong_graph)
        dispatcher.dispatch(event_too_far)

        assert len(delivered) == 1


def _nested(levels: int) -> dict:
    root: dict = {}
    current = root
    for _ in range(levels):
        current["a"] = {}
        current = current["a"]
    return root


class TestDeepMetadataDelivery:
    """Writes accept metadata nested up to MAX_METADATA_DEPTH levels."""

    def test_an_event_at_the_write_depth_cap_reaches_the_subscriber(self):
        from backend.core.models import MAX_METADATA_DEPTH

        storage = MockStorage([create_subscription_node("sub-1", "All")])
        dispatcher = EventDispatcher(storage)
        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append(e))
        deep = _nested(MAX_METADATA_DEPTH)

        event = create_event(
            after={"name": "Deep", "metadata": deep}, before={"metadata": deep}
        )

        assert dispatcher.dispatch(event) == 1
        assert delivered[0].entity.after["metadata"] == deep
        assert delivered[0].entity.before["metadata"] == deep

    def test_deeply_nested_tuple_metadata_is_delivered_as_tuples(self):
        storage = MockStorage([create_subscription_node("sub-1", "All")])
        dispatcher = EventDispatcher(storage)
        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append(e))
        deep = ()
        for _ in range(800):
            deep = (deep,)

        event = create_event(after={"metadata": {"t": deep, "l": [deep, "x"]}})

        assert dispatcher.dispatch(event) == 1
        copied = delivered[0].entity.after["metadata"]
        assert copied["t"] == deep
        assert type(copied["t"]) is tuple
        assert type(copied["l"][0]) is tuple
        assert copied["l"][0] is not deep

    def test_a_subscriber_mutating_its_copy_does_not_affect_another(self):
        storage = MockStorage(
            [
                create_subscription_node("sub-1", "One", webhook_url="https://a.com"),
                create_subscription_node("sub-2", "Two", webhook_url="https://b.com"),
            ]
        )
        dispatcher = EventDispatcher(storage)
        delivered = []

        def deliver(event, url):
            event.entity.after["metadata"]["a"]["a"].append("mutated")
            event.entity.after["tags"].append("mutated")
            delivered.append(event)

        dispatcher.set_delivery_callback(deliver)
        original = {"metadata": {"a": {"a": []}}, "tags": []}
        event = create_event(after=original)

        dispatcher.dispatch(event)

        assert len(delivered) == 2
        assert delivered[0].entity.after is not delivered[1].entity.after
        assert delivered[0].entity.after["metadata"]["a"]["a"] == ["mutated"]
        assert delivered[1].entity.after["tags"] == ["mutated"]
        assert event.entity.after == {"metadata": {"a": {"a": []}}, "tags": []}

    def test_list_nesting_and_scalars_are_preserved(self):
        storage = MockStorage([create_subscription_node("sub-1", "All")])
        dispatcher = EventDispatcher(storage)
        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append(e))
        after = {"m": {"l": [1, "x", None, [True, {"k": 2.5}]]}}

        dispatcher.dispatch(create_event(after=after))

        assert delivered[0].entity.after == after

    def test_before_patch_and_origin_are_isolated_per_subscriber(self):
        storage = MockStorage(
            [
                create_subscription_node("sub-1", "One", webhook_url="https://a.com"),
                create_subscription_node("sub-2", "Two", webhook_url="https://b.com"),
            ]
        )
        dispatcher = EventDispatcher(storage)
        delivered = []

        def deliver(event, url):
            event.entity.before["k"]["x"].append("mutated")
            event.entity.patch["k"]["x"].append("mutated")
            event.origin.event_origin = f"changed-by-{url}"
            delivered.append(event)

        dispatcher.set_delivery_callback(deliver)
        event = create_event(
            event_type=EventType.NODE_UPDATE,
            event_origin="original",
            before={"k": {"x": []}},
            after={"k": {"x": [1]}},
        )
        event.entity.patch = {"k": {"x": []}}

        dispatcher.dispatch(event)

        assert delivered[1].entity.before == {"k": {"x": ["mutated"]}}
        assert delivered[1].entity.patch == {"k": {"x": ["mutated"]}}
        assert delivered[1].origin.event_origin == "changed-by-https://b.com"
        assert event.entity.before == {"k": {"x": []}}
        assert event.entity.patch == {"k": {"x": []}}
        assert event.origin.event_origin == "original"

    def test_absent_payloads_stay_absent(self):
        storage = MockStorage([create_subscription_node("sub-1", "All")])
        dispatcher = EventDispatcher(storage)
        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append(e))

        dispatcher.dispatch(create_event(EventType.NODE_CREATE, before=None))
        delete = create_event(EventType.NODE_DELETE, before={"a": 1})
        delete.entity.after = None
        dispatcher.dispatch(delete)

        assert delivered[0].entity.before is None and delivered[0].entity.patch is None
        assert delivered[1].entity.after is None

    def test_dict_key_order_is_preserved(self):
        storage = MockStorage([create_subscription_node("sub-1", "All")])
        dispatcher = EventDispatcher(storage)
        delivered = []
        dispatcher.set_delivery_callback(lambda e, u: delivered.append(e))

        dispatcher.dispatch(create_event(after={"z": {"b": 1, "a": 2}, "y": 0}))

        after = delivered[0].entity.after
        assert list(after) == ["z", "y"] and list(after["z"]) == ["b", "a"]


class TestCopyPayloadTuples:
    def test_tuples_keep_order_type_and_share_nothing_with_the_source(self):
        source = ((1, 2), [3], {"k": [4]})
        for root in (source, {"t": source}, [source]):
            copied = _copy_payload(root)
            assert copied == root
            inner = (
                copied
                if isinstance(root, tuple)
                else next(iter(copied.values() if isinstance(copied, dict) else copied))
            )
            assert type(inner) is tuple
            assert inner[1] is not source[1]
            assert inner[2] is not source[2]
            assert inner[2]["k"] is not source[2]["k"]

    def test_tuple_copy_does_not_recurse_per_level(self):
        import sys

        deep = ()
        for _ in range(800):
            deep = (deep,)
        previous = sys.getrecursionlimit()
        sys.setrecursionlimit(200)
        try:
            copied = _copy_payload({"t": deep})
        finally:
            sys.setrecursionlimit(previous)
        assert copied == {"t": deep}
