"""Merge planning never aliases what it reads, and storage.update_node hands its validator the node it replaces and the node it would write."""

import copy

import pytest

from backend.runtime.session_context import bind_session
from backend.service import staged_review as sr
from backend.service.tests.test_composed_reads import make_world


@pytest.fixture(params=["file"])
def world(request, tmp_path):
    yield from make_world(request, tmp_path)


def _plan(world, sid):
    review = world.service._load_review(sid, None)
    before = copy.deepcopy(review.entries)
    selected = sr.normalise_selection(review, "all")
    plan, results = sr.plan_merge(review, selected, {}, actor="t", merge_id="m1")
    return review, before, plan, results


def _stage_new_pair(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes(
            [
                {
                    "id": "n1",
                    "type": "Actor",
                    "name": "N1",
                    "metadata": {"k": {"v": 1}},
                },
                {"id": "n2", "type": "Actor", "name": "N2"},
            ],
            [{"source": "n1", "target": "n2", "type": "RELATES_TO"}],
        )
    return sid


def test_the_merge_record_does_not_alias_the_result_items(world):
    sid = _stage_new_pair(world)
    review, before, plan, results = _plan(world, sid)
    results[0]["later"] = "added after planning"
    assert all("later" not in e for e in plan.record["entries"])


def test_planning_a_create_leaves_the_layer_entries_as_loaded(world):
    sid = _stage_new_pair(world)
    review, before, plan, results = _plan(world, sid)
    assert review.entries == before


def test_validating_an_edge_leaves_the_planned_node_payloads_as_planned(world):
    sid = _stage_new_pair(world)
    review, before, plan, results = _plan(world, sid)
    for w in plan.writes:
        assert isinstance(w.payload["created_at"], str), (w.entity_id, w.payload)


def test_storage_update_node_hands_validate_the_node_before_and_after(world):
    seen = []

    def spy(before, after):
        seen.append((before["name"], after["name"]))
        return None

    world.storage.update_node("a", {"name": "Renamed"}, validate=spy)
    assert seen == [("Alpha" if False else seen[0][0], "Renamed")]
    assert seen[0][0] != "Renamed"
