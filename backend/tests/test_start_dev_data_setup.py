"""
start-dev.sh decides when the embedding sidecar is thrown away.

Getting that wrong is silent: delete it too eagerly and every ordinary start
loses the vectors; delete it too late (or not at all) and the app serves the
previous dataset's vectors for every node id the two datasets share. Neither
shows up as an error, and the script had no coverage at all, so this extracts
the data-setup block and drives it directly.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
START_DEV = REPO_ROOT / "start-dev.sh"


def _extract(source: str, start_marker: str, end_marker: str) -> str:
    """str.index raises rather than matching nothing, so a rename or a
    restructure fails these tests loudly instead of voiding them silently."""
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def _extract_resolution_block() -> str:
    """The EMBEDDINGS_FILE -> ACTIVE_DATA_EMBEDDINGS resolution, as shipped."""
    source = START_DEV.read_text(encoding="utf-8")
    return _extract(
        source,
        'if [ -n "${EMBEDDINGS_FILE:-}" ]; then',
        "# =====",
    )


def _extract_data_setup_block() -> str:
    """Pull the sidecar-cleanup function and the data-setup branch out of the
    script, so the block under test is the shipped source rather than a copy."""
    source = START_DEV.read_text(encoding="utf-8")

    start = source.index("drop_stale_embeddings() {")
    end = source.index('echo -e "${GREEN}Using existing active graph data.${NC}"')
    end = source.index("fi", end) + 2
    return source[start:end]


@pytest.fixture(scope="module")
def data_setup_block() -> str:
    return _extract_data_setup_block()


@pytest.fixture(scope="module")
def resolution_block() -> str:
    return _extract_resolution_block()


def _run(
    block: str,
    workdir: Path,
    data_source: str = "",
    env=None,
    resolution: str = "",
    profile_graph: str = "",
) -> str:
    """Run the extracted block against a temp DATA_DIR.

    `resolution` is the script's own EMBEDDINGS_FILE block; passing it means the
    path the cleanup targets is derived exactly as the shipped script derives
    it, rather than by the harness deciding for it.
    """
    fallback = (
        "" if resolution else 'ACTIVE_DATA_EMBEDDINGS="$DEFAULT_ACTIVE_DATA_EMBEDDINGS"'
    )
    harness = f"""
set -e
RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''
SCRIPT_DIR="{workdir}"
DATA_DIR="{workdir}/data"
ACTIVE_DATA="$DATA_DIR/active/graph.json"
DEFAULT_ACTIVE_DATA_EMBEDDINGS="$DATA_DIR/active/graph.embeddings.bin"
DEFAULT_EXAMPLE="$DATA_DIR/examples/default.json"
PROFILE_NAME="default"
DATA_SOURCE="{data_source}"
resolve_config() {{ echo "{profile_graph}"; }}

{resolution}
{fallback}

{block}
"""
    result = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    return result.stdout + result.stderr


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "data" / "active").mkdir(parents=True)
        (root / "data" / "examples").mkdir(parents=True)
        graph = {"nodes": [], "edges": [], "metadata": {"version": "1.0"}}
        (root / "data" / "active" / "graph.json").write_text(json.dumps(graph))
        (root / "data" / "examples" / "default.json").write_text(json.dumps(graph))
        (root / "data" / "active" / "graph.embeddings.bin").write_bytes(b"vectors")
        (root / "replacement.json").write_text(json.dumps(graph))
        yield root


def _sidecar(root: Path) -> Path:
    return root / "data" / "active" / "graph.embeddings.bin"


def test_an_ordinary_start_keeps_the_sidecar(data_setup_block, workspace):
    """The single most damaging mistake would be deleting it on every run."""
    _run(data_setup_block, workspace)

    assert _sidecar(workspace).exists()
    assert _sidecar(workspace).read_bytes() == b"vectors"


def test_replacing_the_graph_drops_the_sidecar(data_setup_block, workspace):
    _run(data_setup_block, workspace, data_source=str(workspace / "replacement.json"))

    assert not _sidecar(workspace).exists()


def test_a_missing_data_source_leaves_the_sidecar_alone(data_setup_block, workspace):
    """The graph was not replaced, so its vectors are still the right ones.
    Deleting before validating the source would lose them for nothing."""
    output = _run(
        data_setup_block, workspace, data_source=str(workspace / "does-not-exist.json")
    )

    assert "Data file not found" in output
    assert _sidecar(workspace).exists()
    assert _sidecar(workspace).read_bytes() == b"vectors"


def test_seeding_a_missing_graph_from_the_example_drops_the_sidecar(
    data_setup_block, workspace
):
    """A different dataset lands beside the old sidecar on this path too."""
    (workspace / "data" / "active" / "graph.json").unlink()

    _run(data_setup_block, workspace)

    assert not _sidecar(workspace).exists()


def test_an_absolute_configured_sidecar_is_the_one_removed(
    data_setup_block, resolution_block, workspace
):
    """EMBEDDINGS_FILE moves the sidecar; the cleanup has to follow it, or it
    deletes nothing and the stale vectors survive the graph replacement."""
    configured = workspace / "elsewhere.bin"
    configured.write_bytes(b"vectors")

    _run(
        data_setup_block,
        workspace,
        data_source=str(workspace / "replacement.json"),
        env={"EMBEDDINGS_FILE": str(configured)},
        resolution=resolution_block,
    )

    assert not configured.exists()
    assert _sidecar(workspace).exists()  # the default one was not the target


def test_a_relative_configured_sidecar_resolves_beside_the_graph(
    data_setup_block, resolution_block, workspace
):
    """A relative EMBEDDINGS_FILE belongs next to the graph file, the same way
    AppConfig resolves it — not in whatever directory the script was run from."""
    configured = workspace / "data" / "active" / "custom.bin"
    configured.write_bytes(b"vectors")

    _run(
        data_setup_block,
        workspace,
        data_source=str(workspace / "replacement.json"),
        env={"EMBEDDINGS_FILE": "custom.bin"},
        resolution=resolution_block,
    )

    assert not configured.exists()


def test_an_unset_embeddings_file_falls_back_to_the_default(
    data_setup_block, resolution_block, workspace
):
    _run(
        data_setup_block,
        workspace,
        data_source=str(workspace / "replacement.json"),
        resolution=resolution_block,
    )

    assert not _sidecar(workspace).exists()


def test_seeding_from_a_profile_graph_drops_the_sidecar(
    data_setup_block, resolution_block, workspace
):
    """The profile branch replaces the graph too, and had no coverage."""
    profile_graph = workspace / "profile-graph.json"
    profile_graph.write_text(json.dumps({"nodes": [], "edges": [], "metadata": {}}))
    (workspace / "data" / "active" / "graph.json").unlink()

    _run(
        data_setup_block,
        workspace,
        resolution=resolution_block,
        profile_graph=str(profile_graph),
    )

    assert not _sidecar(workspace).exists()


def test_the_script_exports_the_sidecar_path_it_cleans_up():
    """Otherwise the app reads one file and the cleanup targets another."""
    source = START_DEV.read_text(encoding="utf-8")

    assert 'export EMBEDDINGS_FILE="$ACTIVE_DATA_EMBEDDINGS"' in source
    assert re.search(r'ACTIVE_DATA_EMBEDDINGS="\$EMBEDDINGS_FILE"', source)


def test_the_extraction_still_matches_the_script(data_setup_block):
    """Guards the harness itself: if the block is renamed or restructured, these
    tests must fail loudly rather than silently testing nothing."""
    assert "drop_stale_embeddings()" in data_setup_block
    # One definition plus one call per replacement path: the --data copy, the
    # --data download, the profile graph, the example graph, the empty stub.
    # An exact count means removing any single call fails here.
    assert data_setup_block.count("drop_stale_embeddings") == 6


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_the_script_is_syntactically_valid():
    result = subprocess.run(
        ["bash", "-n", str(START_DEV)], capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr


def test_the_sidecar_is_dropped_only_after_a_graph_is_in_place(data_setup_block):
    """Source-order rule, and the one the code comment states. A copy that fails
    after a successful validation is awkward to provoke as root, so pin the
    placement directly: within its own branch, the cleanup call sits after the
    command that puts the replacement graph there, never before it."""
    lines = [line.strip() for line in data_setup_block.splitlines()]
    replacements = [
        'cp "$DATA_SOURCE" "$ACTIVE_DATA"',
        'cp "$PROFILE_GRAPH" "$ACTIVE_DATA"',
        'cp "$DEFAULT_EXAMPLE" "$ACTIVE_DATA"',
    ]

    for anchor in replacements:
        assert anchor in lines, f"{anchor} no longer in the data-setup block"
        index = lines.index(anchor)

        assert "drop_stale_embeddings" in lines[index + 1 : index + 3], (
            f"no sidecar cleanup follows {anchor}"
        )

        # Walk back only to the start of this branch, so a sibling branch's
        # (correct) call is not mistaken for a misplaced one.
        branch_start = 0
        for i in range(index - 1, -1, -1):
            if lines[i].endswith("then") or lines[i] == "else":
                branch_start = i + 1
                break
        assert "drop_stale_embeddings" not in lines[branch_start:index], (
            f"sidecar cleanup runs BEFORE {anchor} in its own branch; a failed "
            f"copy would then destroy the vectors of the graph still in place"
        )
