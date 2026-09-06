"""scripts/strip_profile_runtime_nodes.py removes instance state from a seed.

Runtime node types were always stripped. Since the journal identity stamp,
graph.json also carries metadata.journal_id, which names the lineage the
journal beside the file belongs to; a seed that keeps it gives every instance
seeded from it the same lineage. These tests pin both, and that no committed
seed carries the key.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "strip_profile_runtime_nodes.py"

sys.path.insert(0, str(REPO_ROOT))
from scripts.strip_profile_runtime_nodes import strip  # noqa: E402


def _node(node_id, node_type):
    return {"id": node_id, "type": node_type, "name": node_id}


def _edge(edge_id, source, target):
    return {"id": edge_id, "source": source, "target": target, "type": "RELATES_TO"}


def _seed():
    return {
        "nodes": [
            _node("a", "Actor"),
            _node("b", "Actor"),
            _node("view", "SavedView"),
            _node("hook", "EventSubscription"),
        ],
        "edges": [
            _edge("ab", "a", "b"),
            _edge("av", "a", "view"),
            _edge("hb", "hook", "b"),
        ],
        "metadata": {
            "version": "1.0",
            "graph_name": "seed",
            "journal_id": "0123456789abcdef0123456789abcdef",
        },
    }


class TestStrip:
    def test_runtime_nodes_and_their_edges_go(self):
        graph = _seed()
        removed = strip(graph)
        assert {n["id"] for n in graph["nodes"]} == {"a", "b"}
        assert {e["id"] for e in graph["edges"]} == {"ab"}
        assert removed["SavedView"] == 1 and removed["EventSubscription"] == 1

    def test_the_journal_id_goes_and_the_rest_of_the_metadata_stays(self):
        graph = _seed()
        removed = strip(graph)
        assert "journal_id" not in graph["metadata"]
        assert graph["metadata"] == {"version": "1.0", "graph_name": "seed"}
        assert removed["metadata.journal_id"] == 1

    @pytest.mark.parametrize(
        "runtime_type",
        ["SavedView", "VisualizationView", "EventSubscription", "Agent", "Skill"],
    )
    def test_every_runtime_type_goes(self, runtime_type):
        graph = {
            "nodes": [_node("a", "Actor"), _node("r", runtime_type)],
            "edges": [_edge("ar", "a", "r")],
            "metadata": {},
        }
        removed = strip(graph)
        assert {n["id"] for n in graph["nodes"]} == {"a"}
        assert graph["edges"] == []
        assert removed[runtime_type] == 1

    @pytest.mark.parametrize("value", ["", None])
    def test_a_present_but_empty_journal_id_goes_too(self, value):
        graph = {"nodes": [], "edges": [], "metadata": {"journal_id": value}}
        removed = strip(graph)
        assert "journal_id" not in graph["metadata"]
        assert removed["metadata.journal_id"] == 1

    @pytest.mark.parametrize("metadata", ["not a dict", ["list"], 42])
    def test_a_non_dict_metadata_is_left_alone(self, metadata):
        graph = {"nodes": [_node("a", "Actor")], "edges": [], "metadata": metadata}
        assert strip(graph) == {}
        assert graph["metadata"] == metadata

    def test_a_seed_without_runtime_state_is_left_alone(self):
        graph = {
            "nodes": [_node("a", "Actor")],
            "edges": [],
            "metadata": {"version": "1.0"},
        }
        before = json.dumps(graph, sort_keys=True)
        assert strip(graph) == {}
        assert json.dumps(graph, sort_keys=True) == before

    def test_a_seed_without_metadata_is_left_alone(self):
        graph = {"nodes": [_node("a", "Actor")], "edges": []}
        assert strip(graph) == {}
        assert "metadata" not in graph

    @pytest.mark.parametrize(
        "edge", [_edge("ax", "a", "missing"), _edge("xa", "missing", "a")]
    )
    def test_a_dangling_edge_on_either_end_is_refused(self, edge):
        graph = {"nodes": [_node("a", "Actor")], "edges": [edge], "metadata": {}}
        with pytest.raises(ValueError, match="dangling"):
            strip(graph)


class TestCli:
    def test_a_dry_run_reports_and_does_not_write(self, tmp_path):
        seed = tmp_path / "graph.json"
        seed.write_text(json.dumps(_seed()), encoding="utf-8")
        before = seed.read_bytes()

        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(seed)], capture_output=True, text=True
        )

        assert result.returncode == 0, result.stderr
        assert "removed metadata.journal_id: 1" in result.stdout
        assert "removed SavedView: 1" in result.stdout
        assert "dangling edges: 0" in result.stdout
        assert "(dry run)" in result.stdout
        assert seed.read_bytes() == before

    def test_write_strips_the_file(self, tmp_path):
        seed = tmp_path / "graph.json"
        graph = _seed()
        graph["nodes"][0]["name"] = "Ångström"
        seed.write_text(json.dumps(graph), encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(seed), "--write"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert "written" in result.stdout
        raw = seed.read_bytes()
        written = json.loads(raw.decode("utf-8"))
        assert "journal_id" not in written["metadata"]
        assert {n["id"] for n in written["nodes"]} == {"a", "b"}
        # The committed seeds are diffed and read by people: two-space
        # indent, real UTF-8 rather than escapes or replacement characters,
        # and a trailing newline.
        assert raw.endswith(b"}\n")
        assert b'\n  "nodes"' in raw
        assert "Ångström".encode("utf-8") in raw

    def test_a_dangling_edge_aborts_without_writing(self, tmp_path):
        seed = tmp_path / "graph.json"
        graph = {
            "nodes": [_node("a", "Actor")],
            "edges": [_edge("ax", "a", "missing")],
            "metadata": {},
        }
        seed.write_text(json.dumps(graph), encoding="utf-8")
        before = seed.read_bytes()

        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(seed), "--write"],
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0
        assert "ABORT" in result.stderr
        assert seed.read_bytes() == before


COMMITTED_SEEDS = sorted(REPO_ROOT.glob("config/*/graph.json")) + sorted(
    REPO_ROOT.glob("data/examples/*.json")
)


@pytest.mark.parametrize(
    "seed", COMMITTED_SEEDS, ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_no_committed_seed_carries_a_journal_id(seed):
    """A seed with a journal_id hands one lineage to every instance seeded
    from it. Exports never carry the key; a hand-copied graph.json does."""
    graph = json.loads(seed.read_text(encoding="utf-8"))
    metadata = graph.get("metadata") or {}
    assert "journal_id" not in metadata, f"{seed}: run strip_profile_runtime_nodes.py"


def test_the_committed_seed_list_is_not_empty():
    assert COMMITTED_SEEDS, "the guard above would pass vacuously"
