"""
Tests for backend.service.graph_merge_plan — the read-only merge planner and
plan digest of docs/adr/0013-graph-import-merge-mode.md (slice 1).

The planner runs against a real ``GraphStorage`` through ``StorageGraphView``
unless a test needs to control the live graph's reads directly.
"""

import os

import pytest

from backend.core import Edge, GraphStorage, Node
from backend.service import graph_merge_plan
from backend.service.attachments import AttachmentSettings
from backend.service.graph_merge_plan import (
    MergeParameters,
    MergePlanUnstable,
    MergePolicy,
    MergeRequestError,
    StorageGraphView,
    compute_merge_plan,
    parse_merge_request,
)


@pytest.fixture
def storage(temp_dir) -> GraphStorage:
    store = GraphStorage(json_path=os.path.join(temp_dir, "graph.json"))
    store.add_nodes(
        [
            Node(id="a1", type="Actor", name="Alice", subtypes=["Agency"]),
            Node(id="i1", type="Initiative", name="Project X"),
            Node(id="g1", type="Goal", name="Goal", archived=True),
        ],
        [Edge(id="e1", source="a1", target="i1", type="BELONGS_TO")],
    )
    return store


def _plan(
    storage,
    nodes=(),
    edges=(),
    *,
    policy=None,
    include_system_types=False,
    attachments=None,
):
    params = MergeParameters(
        policy=policy or MergePolicy(), include_system_types=include_system_types
    )
    return compute_merge_plan(
        {"nodes": list(nodes), "edges": list(edges)},
        params,
        StorageGraphView(storage, attachments),
    )


def _skip_reasons(plan):
    return {(s["kind"], s["id"]): s["reason"] for s in plan.report["skips"]["items"]}


def _conflicts(plan):
    return {
        (c["kind"], c["id"]): c["reason"] for c in plan.report["conflicts"]["items"]
    }


class TestRequestParsing:
    def test_defaults_are_fail_and_system_types_excluded(self):
        document, params = parse_merge_request({"document": {"nodes": []}})
        assert document == {"nodes": []}
        assert params.policy.default == "fail"
        assert params.include_system_types is False
        assert params.stamp_provenance is False

    def test_stamp_provenance_true_is_refused(self):
        with pytest.raises(MergeRequestError) as exc:
            parse_merge_request({"document": {"nodes": []}, "stamp_provenance": True})
        assert exc.value.error_code == "provenance_not_supported"

    @pytest.mark.parametrize(
        "body",
        [
            {"document": {}, "polcy": {}},
            {"document": {}, "policy": {"default": "merge"}},
            {"document": {}, "policy": {"by_type": {"NoSuchType": "skip"}}},
            {"document": {}, "policy": {"by_type": {"Actor": "replace"}}},
            {"document": {}, "policy": {"default": "skip", "extra": 1}},
            {"document": {}, "include_system_types": "yes"},
            {"policy": {}},
            [],
        ],
    )
    def test_malformed_parameters_are_refused_not_defaulted(self, body):
        with pytest.raises(MergeRequestError) as exc:
            parse_merge_request(body)
        assert exc.value.error_code == "invalid_parameters"

    def test_by_type_accepts_node_and_relationship_types(self):
        _, params = parse_merge_request(
            {
                "document": {},
                "policy": {"by_type": {"Actor": "skip", "BELONGS_TO": "overwrite"}},
            }
        )
        assert params.policy.for_type("Actor") == "skip"
        assert params.policy.for_type("BELONGS_TO") == "overwrite"
        assert params.policy.for_type("Goal") == "fail"


class TestDocumentShape:
    def test_a_non_object_document_cannot_be_planned(self, storage):
        with pytest.raises(MergeRequestError) as exc:
            compute_merge_plan([], MergeParameters(), StorageGraphView(storage))
        assert exc.value.error_code == "validation_failed"

    def test_document_caps_are_the_v1_caps(self, storage, monkeypatch):
        from backend.service import graph_import

        monkeypatch.setattr(graph_import, "MAX_IMPORT_NODES", 1)
        with pytest.raises(MergeRequestError) as exc:
            _plan(
                storage,
                [
                    {"id": "x", "type": "Actor", "name": "X"},
                    {"id": "y", "type": "Actor", "name": "Y"},
                ],
            )
        assert exc.value.error_code == "validation_failed"


class TestNodeCollisions:
    def test_new_ids_are_adds(self, storage):
        plan = _plan(storage, [{"id": "new", "type": "Actor", "name": "N"}])
        assert plan.report["adds"]["nodes"] == {"count": 1, "ids": ["new"]}
        assert plan.committable

    def test_default_policy_fail_makes_a_collision_a_conflict(self, storage):
        plan = _plan(storage, [{"id": "a1", "type": "Actor", "name": "Other"}])
        assert _conflicts(plan) == {("node", "a1"): "id_collision"}
        assert not plan.committable

    def test_per_type_override_beats_the_default(self, storage):
        plan = _plan(
            storage,
            [
                {"id": "a1", "type": "Actor", "name": "Other"},
                {"id": "i1", "type": "Initiative", "name": "Other"},
            ],
            policy=MergePolicy(default="skip", by_type=(("Actor", "fail"),)),
        )
        assert _conflicts(plan) == {("node", "a1"): "id_collision"}
        assert _skip_reasons(plan)[("node", "i1")] == "collision_kept_live"

    def test_overwrite_lists_changed_caller_writable_fields_only(self, storage):
        live = storage.get_node("a1")
        plan = _plan(
            storage,
            [
                {
                    "id": "a1",
                    "type": "Actor",
                    "name": "Alice B",
                    "subtypes": ["Agency"],
                    "revision": 999,
                    "archived": True,
                    "created_at": "2001-01-01T00:00:00Z",
                }
            ],
            policy=MergePolicy(default="overwrite"),
        )
        items = plan.report["overwrites"]["nodes"]["items"]
        assert items == [
            {
                "id": "a1",
                "changes": {"name": {"main": live.name, "incoming": "Alice B"}},
            }
        ]

    def test_overwrite_with_no_change_is_a_skip_not_a_write(self, storage):
        plan = _plan(
            storage,
            [{"id": "i1", "type": "Initiative", "name": "Project X"}],
            policy=MergePolicy(default="overwrite"),
        )
        assert plan.report["overwrites"]["nodes"]["count"] == 0
        assert _skip_reasons(plan)[("node", "i1")] == "unchanged"
        assert plan.report["events"]["count"] == 0

    def test_rename_rewrites_document_edges_and_mints_no_id_in_the_plan(self, storage):
        plan = _plan(
            storage,
            [{"id": "a1", "type": "Actor", "name": "Twin"}],
            [{"id": "e9", "source": "a1", "target": "i1", "type": "RELATES_TO"}],
            policy=MergePolicy(default="rename"),
        )
        assert plan.report["renames"]["nodes"]["items"] == [
            {"id": "a1", "edges_rewritten": ["e9"]}
        ]
        # The rewritten edge is created against the renamed node, so the
        # live a1 -> i1 edges are not duplicates of it.
        assert plan.report["adds"]["edges"]["ids"] == ["e9"]
        assert "new_id" not in str(plan.report)

    @pytest.mark.parametrize("policy", ["fail", "skip", "overwrite"])
    def test_type_mismatch_is_a_conflict_under_every_policy_but_rename(
        self, storage, policy
    ):
        plan = _plan(
            storage,
            [{"id": "a1", "type": "Goal", "name": "Alice"}],
            policy=MergePolicy(default=policy),
        )
        assert _conflicts(plan) == {("node", "a1"): "type_mismatch"}

    def test_type_mismatch_under_rename_is_a_rename(self, storage):
        plan = _plan(
            storage,
            [{"id": "a1", "type": "Goal", "name": "Alice"}],
            policy=MergePolicy(default="rename"),
        )
        assert plan.report["renames"]["nodes"]["count"] == 1
        assert plan.committable


class TestEdges:
    def test_an_edge_may_attach_to_a_live_node(self, storage):
        plan = _plan(
            storage,
            [{"id": "new", "type": "Initiative", "name": "N"}],
            [{"id": "e9", "source": "a1", "target": "new", "type": "BELONGS_TO"}],
        )
        assert plan.report["adds"]["edges"]["ids"] == ["e9"]
        assert plan.committable

    def test_a_dangling_endpoint_is_rejected_never_a_placeholder(self, storage):
        plan = _plan(
            storage,
            [],
            [{"id": "e9", "source": "a1", "target": "Project X", "type": "RELATES_TO"}],
        )
        assert plan.report["rejected"]["count"] == 1
        assert "dangling" in plan.report["rejected"]["items"][0]["message"]
        assert plan.report["adds"]["edges"]["count"] == 0
        assert not plan.committable

    def test_an_edge_of_a_skipped_node_attaches_to_the_live_node(self, storage):
        plan = _plan(
            storage,
            [{"id": "i1", "type": "Initiative", "name": "Different"}],
            [{"id": "e9", "source": "i1", "target": "a1", "type": "RELATES_TO"}],
            policy=MergePolicy(default="skip"),
        )
        assert plan.report["adds"]["edges"]["ids"] == ["e9"]

    def test_attaching_to_a_live_node_is_judged_against_its_live_subtypes(
        self, storage, monkeypatch
    ):
        def _rule(rel, s_type, t_type, s_sub=None, t_sub=None):
            allowed = not (rel == "BELONGS_TO" and "Agency" in (t_sub or []))
            return {"allowed": allowed, "message": "" if allowed else "no agencies"}

        monkeypatch.setattr(
            graph_merge_plan.config_loader, "relationship_type_allows_node_types", _rule
        )
        plan = _plan(
            storage,
            [{"id": "new", "type": "Actor", "name": "N"}],
            [{"id": "e9", "source": "new", "target": "a1", "type": "BELONGS_TO"}],
        )
        assert _conflicts(plan) == {("edge", "e9"): "inapplicable"}

    def test_inapplicable_between_document_nodes_is_rejected(
        self, storage, monkeypatch
    ):
        monkeypatch.setattr(
            graph_merge_plan.config_loader,
            "relationship_type_allows_node_types",
            lambda *a, **k: {"allowed": False, "message": "never"},
        )
        plan = _plan(
            storage,
            [
                {"id": "x", "type": "Actor", "name": "X"},
                {"id": "y", "type": "Actor", "name": "Y"},
            ],
            [{"id": "e9", "source": "x", "target": "y", "type": "RELATES_TO"}],
        )
        assert plan.report["rejected"]["items"] == [
            {"location": "edges[0]", "message": "never"}
        ]

    def test_a_duplicate_live_triple_is_skipped(self, storage):
        plan = _plan(
            storage,
            [],
            [
                {
                    "id": "e-regenerated",
                    "source": "a1",
                    "target": "i1",
                    "type": "BELONGS_TO",
                }
            ],
        )
        assert plan.report["skips"]["items"] == [
            {
                "kind": "edge",
                "id": "e-regenerated",
                "reason": "duplicate_edge",
                "live_edge_id": "e1",
            }
        ]
        assert plan.report["adds"]["edges"]["count"] == 0

    def test_the_same_triple_with_another_type_is_not_a_duplicate(self, storage):
        plan = _plan(
            storage,
            [],
            [{"id": "e9", "source": "a1", "target": "i1", "type": "RELATES_TO"}],
        )
        assert plan.report["adds"]["edges"]["ids"] == ["e9"]

    def test_edge_endpoint_mismatch_is_a_conflict_unless_rename(self, storage):
        edges = [{"id": "e1", "source": "i1", "target": "a1", "type": "BELONGS_TO"}]
        plan = _plan(storage, [], edges, policy=MergePolicy(default="overwrite"))
        assert _conflicts(plan) == {("edge", "e1"): "endpoint_mismatch"}
        renamed = _plan(storage, [], edges, policy=MergePolicy(default="rename"))
        assert renamed.report["renames"]["edges"]["ids"] == ["e1"]

    def test_edge_overwrite_lists_changed_fields(self, storage):
        plan = _plan(
            storage,
            [],
            [
                {
                    "id": "e1",
                    "source": "a1",
                    "target": "i1",
                    "type": "BELONGS_TO",
                    "label": "member",
                }
            ],
            policy=MergePolicy(default="overwrite"),
        )
        assert plan.report["overwrites"]["edges"]["items"] == [
            {"id": "e1", "changes": {"label": {"main": "", "incoming": "member"}}}
        ]

    def test_an_edge_to_an_archived_live_node_is_allowed_and_reported(self, storage):
        plan = _plan(
            storage,
            [],
            [{"id": "e9", "source": "a1", "target": "g1", "type": "AIMS_FOR"}],
        )
        assert plan.report["adds"]["edges"]["ids"] == ["e9"]
        assert plan.report["edges_to_archived_nodes"]["ids"] == ["e9"]


class TestSubtypeChangeRejudgesLiveIncidentEdges:
    def test_an_overwrite_that_strands_a_live_edge_is_a_conflict(
        self, storage, monkeypatch
    ):
        def _rule(rel, s_type, t_type, s_sub=None, t_sub=None):
            allowed = not (rel == "BELONGS_TO" and "Person" in (s_sub or []))
            return {"allowed": allowed, "message": "" if allowed else "persons cannot"}

        monkeypatch.setattr(
            graph_merge_plan.config_loader, "relationship_type_allows_node_types", _rule
        )
        plan = _plan(
            storage,
            [{"id": "a1", "type": "Actor", "name": "Alice", "subtypes": ["Person"]}],
            policy=MergePolicy(default="overwrite"),
        )
        assert _conflicts(plan) == {("edge", "e1"): "incident_edge_inapplicable"}
        assert not plan.committable


class TestValidation:
    def test_an_unknown_node_type_blocks_the_whole_import(self, storage):
        plan = _plan(
            storage,
            [
                {"id": "ok", "type": "Actor", "name": "Fine"},
                {"id": "bad", "type": "NoSuchType", "name": "Bad"},
            ],
        )
        assert plan.report["rejected"]["items"][0]["location"] == "nodes[1]"
        assert not plan.committable

    def test_an_unknown_relationship_type_blocks_the_whole_import(self, storage):
        plan = _plan(
            storage,
            [],
            [{"id": "e9", "source": "a1", "target": "i1", "type": "NO_SUCH_REL"}],
        )
        assert plan.report["rejected"]["count"] == 1
        assert not plan.committable

    def test_duplicate_ids_within_the_document_stay_an_error(self, storage):
        plan = _plan(
            storage,
            [
                {"id": "x", "type": "Actor", "name": "X"},
                {"id": "x", "type": "Actor", "name": "X again"},
            ],
        )
        assert plan.report["rejected"]["items"][0]["location"] == "nodes[1]"


class TestSystemTypes:
    DOCUMENT_NODES = [
        {"id": "agent-1", "type": "Agent", "name": "Agent"},
        {"id": "skill-1", "type": "Skill", "name": "Skill"},
    ]
    DOCUMENT_EDGES = [
        {"id": "es", "source": "agent-1", "target": "skill-1", "type": "USES_SKILL"}
    ]

    def test_excluded_by_default_with_their_edges_skipped_not_dangling(self, storage):
        plan = _plan(storage, self.DOCUMENT_NODES, self.DOCUMENT_EDGES)
        reasons = _skip_reasons(plan)
        assert reasons == {
            ("node", "agent-1"): "system_type_excluded",
            ("node", "skill-1"): "system_type_excluded",
            ("edge", "es"): "system_type_excluded",
        }
        assert plan.report["rejected"]["count"] == 0
        assert plan.committable

    def test_an_edge_to_a_live_system_node_is_excluded_too(self, storage):
        storage.add_nodes([Node(id="live-agent", type="Agent", name="A")], [])
        plan = _plan(
            storage,
            [],
            [
                {
                    "id": "e9",
                    "source": "a1",
                    "target": "live-agent",
                    "type": "RELATES_TO",
                }
            ],
        )
        assert _skip_reasons(plan) == {("edge", "e9"): "system_type_excluded"}

    def test_included_only_on_request(self, storage):
        plan = _plan(
            storage, self.DOCUMENT_NODES, self.DOCUMENT_EDGES, include_system_types=True
        )
        assert plan.report["adds"]["nodes"]["ids"] == ["agent-1", "skill-1"]
        assert plan.report["adds"]["edges"]["ids"] == ["es"]


class _FakeAttachmentStore:
    def __init__(self, with_files):
        self.with_files = set(with_files)

    def list(self, namespace, node_id):
        return ["record"] if node_id in self.with_files else []


class TestAttachmentFlags:
    def test_overwrites_and_creates_over_existing_files_are_flagged(self, storage):
        settings = AttachmentSettings(
            store=_FakeAttachmentStore({"a1", "orphan"}), namespace="ns"
        )
        plan = _plan(
            storage,
            [
                {"id": "a1", "type": "Actor", "name": "Changed"},
                {"id": "orphan", "type": "Actor", "name": "New"},
                {"id": "clean", "type": "Actor", "name": "New"},
            ],
            policy=MergePolicy(default="overwrite"),
            attachments=settings,
        )
        flags = plan.report["attachments"]
        assert flags["checked"] is True
        assert flags["overwritten_with_attachments"]["ids"] == ["a1"]
        assert flags["created_over_orphan_attachments"]["ids"] == ["orphan"]

    def test_without_an_attachment_store_the_check_is_reported_as_not_run(
        self, storage
    ):
        plan = _plan(storage, [{"id": "x", "type": "Actor", "name": "X"}])
        assert plan.report["attachments"]["checked"] is False


class TestEmbeddingsAndEvents:
    def test_counts_creates_renames_and_text_changing_overwrites(self, storage):
        plan = _plan(
            storage,
            [
                {"id": "new", "type": "Actor", "name": "N"},
                {"id": "a1", "type": "Actor", "name": "Alice", "metadata": {"k": 1}},
                {"id": "i1", "type": "Initiative", "name": "Renamed text"},
            ],
            [{"id": "e9", "source": "new", "target": "i1", "type": "RELATES_TO"}],
            policy=MergePolicy(default="overwrite"),
        )
        # new (create) + i1 (name changed); a1 changes metadata only.
        assert plan.report["embeddings"]["nodes_to_embed"] == 2
        # node create + 2 node overwrites + edge create
        assert plan.report["events"]["count"] == 4


class TestDigest:
    DOC_NODES = [
        {"id": "x", "type": "Actor", "name": "X"},
        {"id": "a1", "type": "Actor", "name": "Alice"},
    ]
    DOC_EDGES = [{"id": "e9", "source": "x", "target": "a1", "type": "RELATES_TO"}]

    def _digest(self, storage, nodes=None, edges=None, policy=None, **kw):
        return _plan(
            storage,
            self.DOC_NODES if nodes is None else nodes,
            self.DOC_EDGES if edges is None else edges,
            policy=policy or MergePolicy(default="skip"),
            **kw,
        ).digest

    def test_is_deterministic_and_independent_of_document_order(self, storage):
        first = self._digest(storage)
        again = self._digest(storage)
        reordered = self._digest(storage, nodes=list(reversed(self.DOC_NODES)))
        assert first == again == reordered
        assert first.startswith("sha256:")

    def test_changes_with_the_policy(self, storage):
        assert self._digest(storage) != self._digest(
            storage, policy=MergePolicy(default="overwrite")
        )

    def test_changes_with_include_system_types(self, storage):
        assert self._digest(storage) != self._digest(storage, include_system_types=True)

    def test_changes_with_the_content_of_a_created_entity(self, storage):
        edited = [dict(self.DOC_NODES[0], description="edited"), self.DOC_NODES[1]]
        assert self._digest(storage) != self._digest(storage, nodes=edited)

    def test_ignores_a_document_revision_field(self, storage):
        with_revision = [dict(self.DOC_NODES[0], revision=42), self.DOC_NODES[1]]
        assert self._digest(storage) == self._digest(storage, nodes=with_revision)

    def test_changes_when_a_skipped_live_entity_is_written(self, storage):
        before = self._digest(storage)
        storage.update_node("a1", {"description": "touched"})
        assert self._digest(storage) != before

    def test_changes_when_an_attach_target_is_written(self, storage):
        edges = [{"id": "e9", "source": "x", "target": "i1", "type": "RELATES_TO"}]
        before = self._digest(storage, edges=edges)
        storage.update_node("i1", {"metadata": {"k": "v"}})
        assert self._digest(storage, edges=edges) != before

    def test_changes_when_an_absent_id_appears_live(self, storage):
        before = self._digest(storage)
        storage.add_nodes([Node(id="x", type="Actor", name="X")], [])
        assert self._digest(storage) != before

    def test_changes_with_the_generation(self, storage):
        before = self._digest(storage)
        storage.replace_all_nodes_and_edges(
            storage.get_all_nodes(), storage.get_all_edges()
        )
        assert self._digest(storage) != before

    def test_ignores_the_embedding_backlog(self, storage):
        view = StorageGraphView(storage)
        params = MergeParameters(policy=MergePolicy(default="skip"))
        document = {"nodes": self.DOC_NODES, "edges": self.DOC_EDGES}
        first = compute_merge_plan(document, params, view)

        class _MoreBacklog(StorageGraphView):
            def nodes_without_vector(self):
                return 10_000

        second = compute_merge_plan(document, params, _MoreBacklog(storage))
        assert second.report["embeddings"] != first.report["embeddings"]
        assert second.digest == first.digest


class _ShiftingGenerationView(StorageGraphView):
    """Reports a new generation on every read: a replace landing mid-plan."""

    def __init__(self, storage, stable_after=None):
        super().__init__(storage)
        self.reads = 0
        self.stable_after = stable_after

    @property
    def generation(self):
        self.reads += 1
        if self.stable_after is not None and self.reads > self.stable_after:
            return 1_000
        return self.reads


class TestGenerationRecheck:
    def test_a_replace_mid_plan_restarts_the_plan(self, storage):
        view = _ShiftingGenerationView(storage, stable_after=2)
        plan = compute_merge_plan(
            {"nodes": [{"id": "x", "type": "Actor", "name": "X"}]},
            MergeParameters(),
            view,
        )
        assert plan.committable
        assert view.reads == 4

    def test_a_graph_that_never_settles_is_refused(self, storage):
        with pytest.raises(MergePlanUnstable):
            compute_merge_plan(
                {"nodes": []}, MergeParameters(), _ShiftingGenerationView(storage)
            )


class TestListCaps:
    def test_lists_are_capped_and_counts_stay_exact(self, storage, monkeypatch):
        monkeypatch.setattr(graph_merge_plan, "PLAN_LIST_CAP", 2)
        plan = _plan(
            storage,
            [{"id": f"n{i}", "type": "Actor", "name": "N"} for i in range(5)],
        )
        assert plan.report["adds"]["nodes"]["count"] == 5
        assert len(plan.report["adds"]["nodes"]["ids"]) == 2
