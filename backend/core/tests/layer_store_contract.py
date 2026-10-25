"""The executable contract of the layer store (`staged_sessions`).

docs/SESSION_OVERLAY_CONTRACT.md §7, held against every backend that
declares the capability: the in-memory reference, the file backend and the
PostgreSQL backend. Subclass `LayerStoreContract` and provide `factory`, a
zero-argument callable returning a backend constructed with staged sessions
on ONE store, fresh for the test; calling it again opens the same store, so
a clause checks what landed rather than what an instance remembers.

What a backend may differ in is only who assigns revisions. A single-writer
store keeps the revision an upsert carries, and a store declaring
`revision_enforcement` assigns its own; the clauses read revisions back
rather than assume either.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytest

from backend.core import layer_store as ls
from backend.core.layer_store import MergeCheck
from backend.core.storage_backends import EntityOperation, capabilities_of
from backend.core.tests.persistence_contract import (
    by_id,
    edge_payload,
    node_payload,
    snapshot,
)

A = "1111-2222-3333-4444"
B = "5555-6666-7777-8888"

_fresh = itertools.count(1_000)


def entry(kind: str, entity_id: str, action: str = "update", **extra: Any):
    """A layer entry shaped as §6.1 has it. The store reads only the key."""
    value = {
        "session_id": A,
        "kind": kind,
        "entity_id": entity_id,
        "action": action,
        "base_revision": 1,
        "base": None,
        "staged": {"name": entity_id.upper() + "!"},
        "staged_by": "tester",
        "staged_at": "2026-10-20T00:00:00Z",
        "session_seq": 1,
    }
    value.update(extra)
    return value


def fresh(payload: Dict[str, Any]) -> Dict[str, Any]:
    """`payload` carrying a revision no write has used, as the instance hands
    every merge write one (§4.2). A revision-enforcing store replaces it."""
    return {**payload, "revision": next(_fresh)}


class LayerStoreContract:
    """Subclass this and provide a `factory` fixture; see the module docstring."""

    # -- helpers --------------------------------------------------------------

    @pytest.fixture
    def seeded(self, factory):
        """A store holding nodes a, b, c and edge e1 (a -> b), and session A
        recorded as staged."""
        backend = factory()
        backend.save_graph_data(
            snapshot(
                [
                    node_payload("a", revision=1),
                    node_payload("b", revision=2),
                    node_payload("c", revision=3),
                ],
                [edge_payload("e1", "a", "b", revision=4)],
            )
        )
        backend.record_staged(A)
        return backend

    @staticmethod
    def revision_of(backend, kind: str, entity_id: str):
        data = backend.load_graph_data()
        found = by_id(data, "nodes" if kind == "node" else "edges").get(entity_id)
        return None if found is None else found.get("revision", 0)

    @staticmethod
    def graph(backend) -> Dict[str, Dict[str, Any]]:
        data = backend.load_graph_data()
        return {"nodes": by_id(data, "nodes"), "edges": by_id(data, "edges")}

    @staticmethod
    def keys(entries: List[Dict[str, Any]]):
        return sorted((e["kind"], e["entity_id"]) for e in entries)

    # -- the declaration ------------------------------------------------------

    def test_it_declares_staged_sessions_backed_by_the_layer_store(self, factory):
        backend = factory()
        caps = capabilities_of(backend)
        assert caps.staged_sessions
        assert caps.incremental_writes and caps.transactions
        assert isinstance(backend, ls.LayerStoreBackend)

    # -- the staged-session record --------------------------------------------

    def test_a_session_has_no_record_until_one_is_made(self, seeded):
        assert seeded.staged_status(B) == ls.STATUS_NONE
        seeded.record_staged(B)
        assert seeded.staged_status(B) == ls.STATUS_ACTIVE

    def test_recording_an_active_session_again_changes_nothing(self, seeded, factory):
        seeded.stage(A, [entry("node", "a")])
        seeded.record_staged(A)
        assert factory().staged_status(A) == ls.STATUS_ACTIVE
        assert self.keys(factory().load_layer(A)) == [("node", "a")]

    def test_a_retired_record_never_becomes_active_again(self, seeded, factory):
        seeded.retire(A)
        assert factory().staged_status(A) == ls.STATUS_RETIRED
        with pytest.raises(ls.StagedSessionRetired):
            factory().record_staged(A)
        assert factory().staged_status(A) == ls.STATUS_RETIRED

    def test_retiring_an_unknown_id_records_it_retired(self, seeded):
        seeded.retire(B)
        assert seeded.staged_status(B) == ls.STATUS_RETIRED
        with pytest.raises(ls.StagedSessionRetired):
            seeded.record_staged(B)

    def test_retire_drops_the_layer_in_the_same_unit(self, seeded, factory):
        seeded.stage(A, [entry("node", "a"), entry("node", "b")])
        seeded.retire(A)
        reopened = factory()
        assert reopened.load_layer(A) == []
        assert A not in reopened.layers_in_use()

    def test_retire_requiring_an_empty_layer_refuses_one_with_entries(
        self, seeded, factory
    ):
        seeded.stage(A, [entry("node", "a")])
        with pytest.raises(ls.LayerNotEmpty) as raised:
            seeded.retire(A, require_empty=True)
        assert raised.value.entries == 1
        reopened = factory()
        assert reopened.staged_status(A) == ls.STATUS_ACTIVE
        assert self.keys(reopened.load_layer(A)) == [("node", "a")]

    def test_retire_requiring_an_empty_layer_retires_an_empty_one(
        self, seeded, factory
    ):
        seeded.retire(A, require_empty=True)
        assert factory().staged_status(A) == ls.STATUS_RETIRED
        seeded.retire(A, require_empty=True)
        assert factory().staged_status(A) == ls.STATUS_RETIRED

    # -- staging ----------------------------------------------------------------

    def test_an_unknown_session_has_an_empty_layer(self, seeded):
        assert seeded.load_layer(B) == []

    @pytest.mark.parametrize("retired", [False, True])
    def test_staging_needs_an_active_record_and_writes_nothing_without_one(
        self, seeded, factory, retired
    ):
        if retired:
            seeded.retire(B)
        with pytest.raises(ls.StagedSessionNotActive) as raised:
            seeded.stage(B, [entry("node", "a", session_id=B)])
        assert raised.value.status == (ls.STATUS_RETIRED if retired else ls.STATUS_NONE)
        assert factory().load_layer(B) == []

    def test_staged_entries_land_whole_and_are_shared_by_every_instance(
        self, seeded, factory
    ):
        staged = entry("node", "a", staged={"tags": ["x"], "metadata": {"k": None}})
        seeded.stage(A, [staged, entry("edge", "e1", action="delete")])
        assert sorted(
            factory().load_layer(A), key=lambda e: (e["kind"], e["entity_id"])
        ) == [entry("edge", "e1", action="delete"), staged]

    def test_a_stored_entry_is_a_copy_of_what_was_passed(self, seeded, factory):
        staged = entry("node", "a")
        seeded.stage(A, [staged])
        staged["staged"]["name"] = "changed after staging"
        read = factory().load_layer(A)
        assert read[0]["staged"]["name"] == "A!"
        read[0]["staged"]["name"] = "changed after reading"
        assert factory().load_layer(A)[0]["staged"]["name"] == "A!"

    def test_one_op_upserts_and_removes_atomically(self, seeded, factory):
        seeded.stage(A, [entry("node", "a"), entry("node", "b")])
        seeded.stage(
            A, [entry("node", "c"), entry("node", "a", session_seq=2)], [("node", "b")]
        )
        layer = {(e["kind"], e["entity_id"]): e for e in factory().load_layer(A)}
        assert sorted(layer) == [("node", "a"), ("node", "c")]
        assert layer[("node", "a")]["session_seq"] == 2

    def test_an_op_whose_expectations_hold_lands(self, seeded, factory):
        seeded.stage(A, [entry("node", "a", session_seq=1)])
        seeded.stage(
            A,
            [entry("node", "a", session_seq=2), entry("node", "b", session_seq=2)],
            expected={("node", "a"): 1, ("node", "b"): None},
        )
        layer = {(e["kind"], e["entity_id"]): e for e in factory().load_layer(A)}
        assert {k: e["session_seq"] for k, e in layer.items()} == {
            ("node", "a"): 2,
            ("node", "b"): 2,
        }

    @pytest.mark.parametrize(
        "expected",
        [
            {("node", "a"): 7},
            {("node", "a"): None},
            {("node", "b"): 1},
        ],
        ids=["restaged-since", "staged-since", "gone-since"],
    )
    def test_an_op_decided_on_a_layer_that_changed_since_lands_nothing(
        self, seeded, factory, expected
    ):
        """Two ops reading the same entry and each writing its own successor
        would lose the first one's change (§6.2's net change per entity)."""
        seeded.stage(A, [entry("node", "a", session_seq=1)])
        before = factory().load_layer(A)
        with pytest.raises(ls.LayerRaced) as raised:
            seeded.stage(
                A,
                [entry("node", "a", session_seq=9), entry("node", "c")],
                [("node", "b")],
                expected=expected,
            )
        assert raised.value.key == next(iter(expected))
        assert factory().load_layer(A) == before

    @pytest.mark.parametrize(
        "expected", [{("node", "a"): 1.5}, {("node", "a"): True}, {("graph", "a"): 1}]
    )
    def test_a_malformed_expectation_is_refused_before_anything(
        self, seeded, factory, expected
    ):
        with pytest.raises(ls.InvalidLayerRequest):
            seeded.stage(A, [entry("node", "a")], expected=expected)
        assert factory().load_layer(A) == []

    def test_a_version_token_tells_apart_entries_sharing_a_session_seq(
        self, seeded, factory
    ):
        """Instances with their own session documents may stamp the same
        session_seq; the token the staging op stamped is what names one
        version."""
        seeded.stage(A, [entry("node", "a", session_seq=3, stage_token="t1")])
        seeded.stage(A, [entry("node", "a", session_seq=3, stage_token="t2")])
        with pytest.raises(ls.LayerRaced):
            seeded.stage(A, [entry("node", "a")], expected={("node", "a"): "t1"})
        with pytest.raises(ls.LayerRaced):
            seeded.discard(A, [("node", "a")], expected={("node", "a"): "t1"})
        assert seeded.discard(A, [("node", "a")], expected={("node", "a"): "t2"}) == [
            ("node", "a")
        ]

    def test_an_op_that_read_the_whole_layer_is_refused_when_it_grew(
        self, seeded, factory
    ):
        seeded.stage(A, [entry("node", "a", session_seq=1)])
        seeded.stage(A, [entry("node", "b", session_seq=2)])
        with pytest.raises(ls.LayerRaced) as raised:
            seeded.stage(
                A, [entry("node", "c")], expected={("node", "a"): 1}, exact=True
            )
        assert raised.value.key == ("node", "b")
        seeded.stage(
            A,
            [entry("node", "c")],
            expected={("node", "a"): 1, ("node", "b"): 2},
            exact=True,
        )
        assert len(factory().load_layer(A)) == 3

    def test_a_discard_of_an_entry_changed_since_removes_nothing(self, seeded, factory):
        seeded.stage(A, [entry("node", "a", session_seq=2), entry("node", "b")])
        with pytest.raises(ls.LayerRaced):
            seeded.discard(
                A, [("node", "a"), ("node", "b")], expected={("node", "a"): 1}
            )
        assert len(factory().load_layer(A)) == 2
        assert seeded.discard(A, [("node", "a")], expected={("node", "a"): 2}) == [
            ("node", "a")
        ]

    def test_removing_an_entry_that_is_not_there_is_not_an_error(self, seeded):
        seeded.stage(A, [], [("node", "zz")])
        assert seeded.load_layer(A) == []

    @pytest.mark.parametrize(
        "entries,removals",
        [
            ([entry("node", "a"), entry("node", "a")], []),
            ([entry("node", "a")], [("node", "a")]),
            ([], [("node", "a"), ("node", "a")]),
            ([entry("graph", "a")], []),
            ([entry("node", "")], []),
            ([entry("node", "a\x00b")], []),
            ([entry("node", "\ud800")], []),
            ([entry("node", "a", staged={"v": float("nan")})], []),
            ([entry("node", "a", staged={"v": {1, 2}})], []),
            ([entry("node", "a", staged={"v": "nul\x00inside"})], []),
            ([entry("node", "a", staged={"v": "\udc00"})], []),
            ([entry("node", "a", staged={"k\x00": 1})], []),
            ([entry("node", "x" * 513)], []),
            (["not an object"], []),
        ],
        ids=[
            "same-key-twice",
            "entry-and-removal",
            "removal-twice",
            "unknown-kind",
            "empty-id",
            "nul-in-id",
            "lone-surrogate-id",
            "nan",
            "set",
            "nul-in-value",
            "surrogate-in-value",
            "nul-in-key",
            "id-too-long",
            "not-a-mapping",
        ],
    )
    def test_a_malformed_staging_op_is_refused_whole(
        self, seeded, factory, entries, removals
    ):
        seeded.stage(A, [entry("node", "b")])
        with pytest.raises(ls.InvalidLayerRequest):
            seeded.stage(A, entries, removals)
        assert self.keys(factory().load_layer(A)) == [("node", "b")]

    @pytest.mark.parametrize(
        "session_id",
        [
            "",
            "1111",
            "../1111-2222",
            "1111-2222-3333-4444\x00",
            "1111-2222'; --",
            7,
            None,
        ],
    )
    def test_only_a_session_id_shaped_value_reaches_the_store(self, seeded, session_id):
        for call in (
            lambda: seeded.record_staged(session_id),
            lambda: seeded.stage(session_id, [entry("node", "a")]),
            lambda: seeded.load_layer(session_id),
            lambda: seeded.staged_status(session_id),
            lambda: seeded.retire(session_id),
            lambda: seeded.discard(session_id),
            lambda: seeded.drop_layer(session_id),
            lambda: seeded.merge(session_id, "m", [], [], [], {}),
        ):
            with pytest.raises(ls.InvalidLayerRequest):
                call()

    def test_the_cap_refuses_a_staging_op_whole(self, seeded, factory):
        seeded.stage(A, [entry("node", "a"), entry("node", "b")], max_entries=2)
        with pytest.raises(ls.LayerFull) as raised:
            seeded.stage(A, [entry("node", "c"), entry("edge", "e1")], max_entries=3)
        assert (raised.value.limit, raised.value.would_hold) == (3, 4)
        assert self.keys(factory().load_layer(A)) == [("node", "a"), ("node", "b")]

    def test_the_cap_counts_the_layer_the_op_leaves(self, seeded, factory):
        seeded.stage(A, [entry("node", "a"), entry("node", "b")])
        # Replacing an entry adds nothing; a removal frees a place.
        seeded.stage(A, [entry("node", "a", session_seq=2)], max_entries=2)
        seeded.stage(A, [entry("node", "c")], [("node", "b")], max_entries=2)
        assert self.keys(factory().load_layer(A)) == [("node", "a"), ("node", "c")]

    @pytest.mark.parametrize("limit", [-1, 1.5, True, "3"])
    def test_a_cap_that_is_not_a_count_is_refused(self, seeded, limit):
        with pytest.raises(ls.InvalidLayerRequest):
            seeded.stage(A, [entry("node", "a")], max_entries=limit)

    # -- isolation (I1, I10) --------------------------------------------------------

    def test_a_layer_is_invisible_to_every_other_session(self, seeded, factory):
        seeded.record_staged(B)
        seeded.stage(A, [entry("node", "a")])
        seeded.stage(B, [entry("node", "b", session_id=B)])
        reopened = factory()
        assert self.keys(reopened.load_layer(A)) == [("node", "a")]
        assert self.keys(reopened.load_layer(B)) == [("node", "b")]
        reopened.discard(A)
        assert self.keys(factory().load_layer(B)) == [("node", "b")]

    def test_staging_and_discarding_never_touch_the_graph(self, seeded, factory):
        before = self.graph(seeded)
        seeded.stage(A, [entry("node", "a"), entry("node", "zz", action="create")])
        seeded.stage(A, [], [("node", "a")])
        seeded.discard(A)
        seeded.drop_layer(A)
        assert self.graph(factory()) == before

    def test_layers_in_use_names_only_non_empty_layers(self, seeded, factory):
        seeded.record_staged(B)
        seeded.stage(A, [entry("node", "a")])
        assert factory().layers_in_use() == [A]
        seeded.stage(B, [entry("node", "b", session_id=B)])
        assert factory().layers_in_use() == sorted([A, B])
        seeded.discard(A)
        assert factory().layers_in_use() == [B]

    # -- discard and drop -----------------------------------------------------

    def test_discard_removes_what_it_names_and_reports_it(self, seeded, factory):
        seeded.stage(A, [entry("node", "a"), entry("node", "b"), entry("edge", "e1")])
        removed = seeded.discard(A, [("node", "a"), ("node", "zz")])
        assert removed == [("node", "a")]
        assert self.keys(factory().load_layer(A)) == [("edge", "e1"), ("node", "b")]
        assert sorted(seeded.discard(A)) == [("edge", "e1"), ("node", "b")]
        assert factory().load_layer(A) == []
        assert factory().staged_status(A) == ls.STATUS_ACTIVE

    def test_discard_needs_an_active_record(self, seeded):
        with pytest.raises(ls.StagedSessionNotActive):
            seeded.discard(B)

    def test_drop_layer_empties_the_layer_and_keeps_the_record(self, seeded, factory):
        seeded.stage(A, [entry("node", "a")])
        seeded.drop_layer(A)
        assert factory().load_layer(A) == []
        assert factory().staged_status(A) == ls.STATUS_ACTIVE

    # -- merge ----------------------------------------------------------------

    def _update_a(self, backend, name="Merged"):
        current = self.graph(backend)["nodes"]["a"]
        return EntityOperation(
            "node",
            "upsert",
            "a",
            fresh({**current, "name": name}),
            expected_revision=current.get("revision", 0),
        )

    def test_a_merge_applies_removes_and_records_as_one(self, seeded, factory):
        seeded.stage(A, [entry("node", "a"), entry("node", "b")])
        op = self._update_a(seeded)
        create = EntityOperation(
            "node", "upsert", "n", fresh(node_payload("n")), expect_absent=True
        )
        result = seeded.merge(
            A,
            "merge-1",
            [op, create],
            [],
            [("node", "a")],
            {"actor": "tester", "entries": [{"entity_id": "a"}]},
        )
        assert not result.replayed
        reopened = factory()
        graph = self.graph(reopened)
        assert graph["nodes"]["a"]["name"] == "Merged"
        assert "n" in graph["nodes"]
        assert self.keys(reopened.load_layer(A)) == [("node", "b")]
        stored = reopened.load_merge("merge-1")
        assert stored == result.record
        assert stored["actor"] == "tester"
        assert (stored["merge_id"], stored["session_id"]) == ("merge-1", A)
        assert stored["status"] == ls.MERGE_APPLIED
        assert result.revisions == [
            graph["nodes"]["a"]["revision"],
            graph["nodes"]["n"]["revision"],
        ]
        assert [
            (i["kind"], i["entity_id"], i["action"]) for i in stored["applied"]
        ] == [
            ("node", "a", "upsert"),
            ("node", "n", "upsert"),
        ]

    def test_a_merge_with_only_removals_writes_no_graph_change(self, seeded, factory):
        # A `keep_main` resolution drops the entry and applies nothing.
        before = self.graph(seeded)
        seeded.stage(A, [entry("node", "a")])
        seeded.merge(A, "merge-keep", [], [], [("node", "a")], {})
        assert self.graph(factory()) == before
        assert factory().load_layer(A) == []
        assert factory().load_merge("merge-keep")["applied"] == []

    def test_a_repeated_merge_id_returns_the_first_result_and_writes_nothing(
        self, seeded, factory
    ):
        seeded.stage(A, [entry("node", "a"), entry("node", "b")])
        first = seeded.merge(A, "m", [self._update_a(seeded)], [], [("node", "a")], {})
        after_first = self.graph(factory())
        again = factory().merge(
            A,
            "m",
            [self._update_a(seeded, name="Second")],
            [],
            [("node", "b")],
            {"other": True},
        )
        assert again.replayed
        assert again.record == first.record
        assert self.graph(factory()) == after_first
        assert self.keys(factory().load_layer(A)) == [("node", "b")]

    def test_a_merge_id_of_another_session_is_refused(self, seeded, factory):
        seeded.record_staged(B)
        seeded.stage(A, [entry("node", "a")])
        seeded.stage(B, [entry("node", "b", session_id=B)])
        seeded.merge(A, "m", [], [], [("node", "a")], {"secret": "of A"})
        before = self.graph(seeded)
        with pytest.raises(ls.MergeIdTaken):
            factory().merge(B, "m", [], [], [("node", "b")], {})
        assert self.keys(factory().load_layer(B)) == [("node", "b")]
        assert self.graph(factory()) == before

    def test_a_merge_needs_an_active_record(self, seeded, factory):
        seeded.retire(B)
        with pytest.raises(ls.StagedSessionNotActive):
            seeded.merge(B, "m", [self._update_a(seeded)], [], [], {})
        assert factory().load_merge("m") is None
        assert self.graph(factory())["nodes"]["a"]["name"] == "A"

    def _assert_raced_and_untouched(self, seeded, factory, *merge_args):
        before = self.graph(seeded)
        layer_here = sorted(
            seeded.load_layer(A), key=lambda e: (e["kind"], e["entity_id"])
        )
        layer_before = sorted(
            factory().load_layer(A), key=lambda e: (e["kind"], e["entity_id"])
        )
        with pytest.raises(ls.MergeRaced):
            seeded.merge(A, "raced", *merge_args)
        # The instance that refused it holds nothing of it either.
        assert self.graph(seeded) == before
        assert (
            sorted(seeded.load_layer(A), key=lambda e: (e["kind"], e["entity_id"]))
            == layer_here
        )
        reopened = factory()
        assert self.graph(reopened) == before
        assert (
            sorted(reopened.load_layer(A), key=lambda e: (e["kind"], e["entity_id"]))
            == layer_before
        )
        assert reopened.load_merge("raced") is None

    def test_a_stale_expected_revision_fails_the_whole_merge(self, seeded, factory):
        seeded.stage(A, [entry("node", "a"), entry("node", "b")])
        good = EntityOperation(
            "node", "upsert", "n", fresh(node_payload("n")), expect_absent=True
        )
        stale = EntityOperation(
            "node",
            "upsert",
            "b",
            fresh(node_payload("b")),
            expected_revision=self.revision_of(seeded, "node", "b") + 999,
        )
        self._assert_raced_and_untouched(
            seeded, factory, [good, stale], [], [("node", "a"), ("node", "b")], {}
        )

    def test_a_create_whose_id_is_taken_fails_the_merge(self, seeded, factory):
        seeded.stage(A, [entry("node", "a", action="create")])
        taken = EntityOperation(
            "node", "upsert", "a", fresh(node_payload("a")), expect_absent=True
        )
        self._assert_raced_and_untouched(
            seeded, factory, [taken], [], [("node", "a")], {}
        )

    def test_a_stale_delete_fails_the_merge(self, seeded, factory):
        seeded.stage(A, [entry("edge", "e1", action="delete")])
        stale = EntityOperation(
            "edge",
            "delete",
            "e1",
            expected_revision=self.revision_of(seeded, "edge", "e1") + 1,
        )
        self._assert_raced_and_untouched(
            seeded, factory, [stale], [], [("edge", "e1")], {}
        )

    @pytest.mark.parametrize("expectation", [False, True])
    def test_a_delete_of_an_entity_already_gone_fails_the_merge(
        self, seeded, factory, expectation
    ):
        seeded.stage(
            A,
            [
                entry("node", "n", action="create"),
                entry("edge", "gone", action="delete"),
            ],
        )
        create = EntityOperation(
            "node", "upsert", "n", fresh(node_payload("n")), expect_absent=True
        )
        gone = EntityOperation("edge", "delete", "gone", expect_absent=expectation)
        self._assert_raced_and_untouched(
            seeded,
            factory,
            [create, gone],
            [],
            [("node", "n"), ("edge", "gone")],
            {},
        )

    def test_removing_an_entry_no_longer_staged_fails_the_merge(self, seeded, factory):
        seeded.stage(A, [entry("node", "b")])
        self._assert_raced_and_untouched(
            seeded, factory, [self._update_a(seeded)], [], [("node", "a")], {}
        )

    def test_a_layer_that_gained_an_entry_fails_a_merge_that_read_all_of_it(
        self, seeded, factory
    ):
        seeded.stage(A, [entry("node", "a"), entry("node", "b")])
        check = MergeCheck.layer_holds_only([("node", "a")])
        with pytest.raises(ls.MergeRaced):
            seeded.merge(A, "m-holds", [], [check], [("node", "a")], {})
        assert len(factory().load_layer(A)) == 2
        assert factory().load_merge("m-holds") is None
        allowed = MergeCheck.layer_holds_only([("node", "a"), ("node", "b")])
        seeded.merge(A, "m-holds", [], [allowed], [("node", "a")], {})
        assert [e["entity_id"] for e in factory().load_layer(A)] == ["b"]

    def test_an_entry_restaged_since_the_diff_fails_the_merge(self, seeded, factory):
        seeded.stage(A, [entry("node", "a", session_seq=1)])
        seeded.stage(A, [entry("node", "a", session_seq=2)])
        self._assert_raced_and_untouched(
            seeded,
            factory,
            [self._update_a(seeded)],
            [MergeCheck.entry_unchanged("node", "a", 1)],
            [("node", "a")],
            {},
        )

    def test_an_unchanged_entry_check_holds(self, seeded, factory):
        seeded.stage(A, [entry("node", "a", session_seq=2)])
        seeded.merge(
            A,
            "m",
            [self._update_a(seeded)],
            [MergeCheck.entry_unchanged("node", "a", 2)],
            [("node", "a")],
            {},
        )
        assert self.graph(factory())["nodes"]["a"]["name"] == "Merged"

    def _delete_a_with(self, backend, edges):
        return [
            EntityOperation(
                "edge",
                "delete",
                edge_id,
                expected_revision=self.revision_of(backend, "edge", edge_id),
            )
            for edge_id in edges
        ] + [
            EntityOperation(
                "node",
                "delete",
                "a",
                expected_revision=self.revision_of(backend, "node", "a"),
            )
        ]

    def test_a_node_delete_with_exactly_its_incident_edges_applies(
        self, seeded, factory
    ):
        seeded.stage(
            A,
            [entry("node", "a", action="delete"), entry("edge", "e1", action="delete")],
        )
        seeded.merge(
            A,
            "m",
            self._delete_a_with(seeded, ["e1"]),
            [MergeCheck.incident_edges("a", ["e1"])],
            [("node", "a"), ("edge", "e1")],
            {},
        )
        graph = self.graph(factory())
        assert "a" not in graph["nodes"] and "e1" not in graph["edges"]

    def test_an_incident_edge_added_since_the_diff_fails_a_node_delete(
        self, seeded, factory
    ):
        seeded.stage(
            A,
            [entry("node", "a", action="delete"), entry("edge", "e1", action="delete")],
        )
        ops = self._delete_a_with(seeded, ["e1"])
        # Another writer draws an edge to `a` after the diff was read.
        factory().apply_batch(
            [EntityOperation.upsert_edge(fresh(edge_payload("e2", "c", "a")))]
        )
        self._assert_raced_and_untouched(
            seeded,
            factory,
            ops,
            [MergeCheck.incident_edges("a", ["e1"])],
            [("node", "a"), ("edge", "e1")],
            {},
        )

    def test_a_kept_incident_edge_changed_since_the_diff_fails_the_merge(
        self, seeded, factory
    ):
        seeded.stage(A, [entry("node", "a")])
        revision = self.revision_of(seeded, "edge", "e1")
        checks = [MergeCheck.incident_edges("a", ["e1"], {"e1": revision})]
        factory().apply_batch(
            [
                EntityOperation.upsert_edge(
                    fresh(edge_payload("e1", "a", "b", label="new"))
                )
            ]
        )
        self._assert_raced_and_untouched(
            seeded, factory, [self._update_a(seeded)], checks, [("node", "a")], {}
        )

    @pytest.mark.parametrize("revisions", [{"unlisted": 1}, {"e1": "4"}, {"e1": True}])
    def test_invalid_incident_edge_revisions_are_refused(self, seeded, revisions):
        with pytest.raises(ls.InvalidLayerRequest):
            seeded.merge(
                A,
                "bad",
                [],
                [MergeCheck.incident_edges("a", ["e1"], revisions)],
                [],
                {},
            )
        assert seeded.load_merge("bad") is None

    def test_an_endpoint_deleted_since_the_diff_fails_an_edge_create(
        self, seeded, factory
    ):
        seeded.stage(A, [entry("edge", "e9", action="create")])
        factory().apply_batch([EntityOperation.delete_node("c")])
        create = EntityOperation(
            "edge",
            "upsert",
            "e9",
            fresh(edge_payload("e9", "b", "c")),
            expect_absent=True,
        )
        self._assert_raced_and_untouched(
            seeded,
            factory,
            [create],
            [MergeCheck.node_exists("b"), MergeCheck.node_exists("c")],
            [("edge", "e9")],
            {},
        )

    def test_an_endpoint_the_merge_itself_deletes_fails_the_check(
        self, seeded, factory
    ):
        seeded.stage(A, [entry("edge", "e1"), entry("node", "b", action="delete")])
        edge = self.graph(seeded)["edges"]["e1"]
        keep = EntityOperation(
            "edge",
            "upsert",
            "e1",
            fresh({**edge, "label": "kept"}),
            expected_revision=edge.get("revision", 0),
        )
        delete_b = EntityOperation(
            "node",
            "delete",
            "b",
            expected_revision=self.revision_of(seeded, "node", "b"),
        )
        self._assert_raced_and_untouched(
            seeded,
            factory,
            [keep, delete_b],
            [MergeCheck.node_exists("a"), MergeCheck.node_exists("b")],
            [("edge", "e1"), ("node", "b")],
            {},
        )

    def test_an_endpoint_created_in_the_same_unit_satisfies_the_check(
        self, seeded, factory
    ):
        seeded.stage(
            A,
            [entry("node", "n", action="create"), entry("edge", "e9", action="create")],
        )
        seeded.merge(
            A,
            "m",
            [
                EntityOperation(
                    "node", "upsert", "n", fresh(node_payload("n")), expect_absent=True
                ),
                EntityOperation(
                    "edge",
                    "upsert",
                    "e9",
                    fresh(edge_payload("e9", "a", "n")),
                    expect_absent=True,
                ),
            ],
            [MergeCheck.node_exists("a"), MergeCheck.node_exists("n")],
            [("node", "n"), ("edge", "e9")],
            {},
        )
        graph = self.graph(factory())
        assert "n" in graph["nodes"] and graph["edges"]["e9"]["target"] == "n"
        assert factory().load_layer(A) == []

    def test_a_partial_merge_leaves_the_unselected_entries_untouched(
        self, seeded, factory
    ):
        kept = entry("node", "b", staged={"name": "kept as staged"})
        seeded.stage(A, [entry("node", "a"), kept])
        seeded.merge(A, "m", [self._update_a(seeded)], [], [("node", "a")], {})
        assert factory().load_layer(A) == [kept]

    @pytest.mark.parametrize(
        "operations,checks,removals,record",
        [
            ([EntityOperation("node", "upsert", "a", {"id": "other"})], [], [], {}),
            ([EntityOperation("node", "upsert", "a", None)], [], [], {}),
            ([EntityOperation("node", "patch", "a", None)], [], [], {}),
            (
                [
                    EntityOperation("node", "delete", "a"),
                    EntityOperation("node", "delete", "a"),
                ],
                [],
                [],
                {},
            ),
            (["not an op"], [], [], {}),
            ([], ["not a check"], [], {}),
            ([], [MergeCheck("bogus", "a")], [], {}),
            ([], [], [("node", "a"), ("node", "a")], {}),
            ([], [], [], "not a record"),
            ([], [], [], {"t": float("inf")}),
            ([], [], [], {"note": "\x00"}),
            ([], [MergeCheck.incident_edges("a", ["e\x00"])], [], {}),
        ],
        ids=[
            "id-mismatch",
            "upsert-without-payload",
            "unknown-action",
            "entity-twice",
            "not-an-op",
            "not-a-check",
            "unknown-check",
            "removal-twice",
            "record-not-a-mapping",
            "record-not-json",
            "nul-in-record",
            "nul-in-check-edge-id",
        ],
    )
    def test_a_malformed_merge_is_refused_before_anything_is_read(
        self, seeded, factory, operations, checks, removals, record
    ):
        before = self.graph(seeded)
        with pytest.raises(ls.InvalidLayerRequest):
            seeded.merge(A, "bad", operations, checks, removals, record)
        with pytest.raises(ls.InvalidLayerRequest):
            seeded.merge(A, "bad\x00id", [], [], [], {})
        assert self.graph(factory()) == before
        assert factory().load_merge("bad") is None

    def test_merge_records_outlive_the_session(self, seeded, factory):
        seeded.stage(A, [entry("node", "a")])
        result = seeded.merge(A, "m", [], [], [("node", "a")], {})
        seeded.retire(A)
        assert factory().load_merge("m") == result.record

    def test_a_merged_upsert_carries_the_revision_the_record_names(
        self, seeded, factory
    ):
        seeded.stage(A, [entry("node", "a")])
        result = seeded.merge(A, "m", [self._update_a(seeded)], [], [("node", "a")], {})
        revision = self.revision_of(factory(), "node", "a")
        assert result.record["applied"][0]["revision"] == revision
        assert revision > 3

    # -- merge-record retention (§11.5) ---------------------------------------

    RETENTION = timedelta(days=90)

    @staticmethod
    def _merged(backend, session_id: str, merge_id: str, entity: str = "a"):
        """A merge record for `session_id`: one entry staged and merged away."""
        backend.stage(session_id, [entry("node", entity, session_id=session_id)])
        return backend.merge(session_id, merge_id, [], [], [("node", entity)], {})

    def _retired_between(self, backend, session_id: str):
        """Retire `session_id`; the moments just before and just after, which
        bracket the retirement time the store recorded."""
        before = ls.utc_now() - timedelta(seconds=1)
        backend.retire(session_id)
        return before, ls.utc_now() + timedelta(seconds=1)

    def test_a_record_of_an_existing_session_is_never_purged(self, seeded, factory):
        self._merged(seeded, A, "m-live")
        far = ls.utc_now() + timedelta(days=3650)
        assert factory().purge_merge_records(self.RETENTION, limit=10, now=far) == 0
        assert factory().load_merge("m-live") is not None

    def test_a_record_inside_the_window_is_kept_and_one_past_it_purged(
        self, seeded, factory
    ):
        self._merged(seeded, A, "m")
        before, after = self._retired_between(seeded, A)
        inside = before + self.RETENTION
        assert factory().purge_merge_records(self.RETENTION, limit=10, now=inside) == 0
        assert factory().load_merge("m") is not None
        outside = after + self.RETENTION
        assert factory().purge_merge_records(self.RETENTION, limit=10, now=outside) == 1
        assert factory().load_merge("m") is None

    def test_the_purge_takes_only_records_of_sessions_past_the_window(
        self, seeded, factory
    ):
        seeded.record_staged(B)
        self._merged(seeded, A, "m-a")
        self._merged(seeded, B, "m-b")
        _, after = self._retired_between(seeded, A)
        now = after + self.RETENTION
        assert factory().purge_merge_records(self.RETENTION, limit=10, now=now) == 1
        assert factory().load_merge("m-a") is None
        assert factory().load_merge("m-b") is not None

    def test_a_purge_is_bounded_by_its_limit_and_idempotent(self, seeded, factory):
        for n in range(3):
            self._merged(seeded, A, f"m-{n}", entity="abc"[n])
        _, after = self._retired_between(seeded, A)
        now = after + self.RETENTION
        assert factory().purge_merge_records(self.RETENTION, limit=2, now=now) == 2
        assert [factory().load_merge(f"m-{n}") is None for n in range(3)].count(
            True
        ) == 2
        assert factory().purge_merge_records(self.RETENTION, limit=2, now=now) == 1
        assert factory().purge_merge_records(self.RETENTION, limit=2, now=now) == 0
        assert all(factory().load_merge(f"m-{n}") is None for n in range(3))

    def test_a_purged_merge_id_replayed_is_refused_and_the_id_stays_retired(
        self, seeded, factory
    ):
        self._merged(seeded, A, "m")
        _, after = self._retired_between(seeded, A)
        factory().purge_merge_records(
            self.RETENTION, limit=10, now=after + self.RETENTION
        )
        with pytest.raises(ls.StagedSessionNotActive) as raised:
            factory().merge(A, "m", [], [], [], {})
        assert raised.value.status == ls.STATUS_RETIRED
        assert factory().load_merge("m") is None
        assert factory().staged_status(A) == ls.STATUS_RETIRED
        with pytest.raises(ls.StagedSessionRetired):
            factory().record_staged(A)

    def test_retiring_again_keeps_the_first_retirement_time(self, seeded, factory):
        self._merged(seeded, A, "m")
        before, _ = self._retired_between(seeded, A)
        factory().retire(A)
        factory().retire(A, require_empty=True)
        inside = before + self.RETENTION
        assert factory().purge_merge_records(self.RETENTION, limit=10, now=inside) == 0

    @pytest.mark.parametrize(
        "retention, limit, now",
        [
            (timedelta(0), 1, None),
            (timedelta(days=-1), 1, None),
            (90, 1, None),
            (timedelta(days=90), 0, None),
            (timedelta(days=90), True, None),
            (timedelta(days=90), 1, datetime(2026, 1, 1)),
        ],
        ids=["zero", "negative", "not-a-timedelta", "no-limit", "bool", "naive"],
    )
    def test_a_malformed_purge_is_refused_before_anything_is_read(
        self, seeded, factory, retention, limit, now
    ):
        self._merged(seeded, A, "m")
        seeded.retire(A)
        with pytest.raises(ls.InvalidLayerRequest):
            seeded.purge_merge_records(retention, limit=limit, now=now)
        assert factory().load_merge("m") is not None
