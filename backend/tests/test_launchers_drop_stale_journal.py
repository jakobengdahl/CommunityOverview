"""The other two launchers seed a missing graph.json the same way start-dev.sh
does, and inherit the same hazard: a journal left behind by the previous
graph would be replayed onto the seed at startup. Each must drop it exactly
when it seeds - and never on an ordinary start."""

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LAUNCHERS = ["scripts/start-sprint.sh", "scripts/start-sspcloud-metadata.sh"]
JOURNAL_LINE = '{"ops": []}\n'


def _graph_data_block(script: Path) -> str:
    source = script.read_text(encoding="utf-8")
    match = re.search(
        r"^# Graph data\n# =+\n(.*?)^export GRAPH_FILE=", source, re.S | re.M
    )
    assert match, f"{script.name}: seeding block not found"
    return match.group(1)


def _run(block: str, root: Path) -> None:
    harness = f"""
set -e
DATA_DIR="{root}/data"
ACTIVE_DATA="$DATA_DIR/active/graph.json"
SPRINT_CONFIG_DIR="{root}/nowhere"
PROFILE_CONFIG_DIR="{root}/nowhere"
{block}
"""
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def root():
    with tempfile.TemporaryDirectory() as d:
        r = Path(d)
        (r / "data" / "active").mkdir(parents=True)
        (r / "data" / "active" / "graph.journal.ndjson").write_text(JOURNAL_LINE)
        yield r


@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_seeding_a_missing_graph_drops_the_journal(launcher, root):
    _run(_graph_data_block(REPO / launcher), root)

    assert (root / "data" / "active" / "graph.json").exists()
    assert not (root / "data" / "active" / "graph.journal.ndjson").exists()


@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_an_ordinary_start_keeps_the_journal(launcher, root):
    graph = {"nodes": [], "edges": [], "metadata": {"version": "1.0"}}
    (root / "data" / "active" / "graph.json").write_text(json.dumps(graph))

    _run(_graph_data_block(REPO / launcher), root)

    assert (
        root / "data" / "active" / "graph.journal.ndjson"
    ).read_text() == JOURNAL_LINE
