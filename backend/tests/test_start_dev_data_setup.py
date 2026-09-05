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
# What start-dev.sh checks for before deleting anything at the sidecar path.
SIDECAR_BYTES = b"CKGEMB\x01" + b"a stand-in for a real matrix"
JOURNAL_LINE = '{"ops": [{"kind": "node", "action": "delete", "entity_id": "x", "payload": null}]}\n'

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
        # Must carry the format's magic: the cleanup refuses to delete a file
        # that is not a sidecar, so a stand-in without it tests the wrong path.
        (root / "data" / "active" / "graph.embeddings.bin").write_bytes(SIDECAR_BYTES)
        (root / "data" / "active" / "graph.journal.ndjson").write_text(JOURNAL_LINE)
        (root / "replacement.json").write_text(json.dumps(graph))
        yield root


def _sidecar(root: Path) -> Path:
    return root / "data" / "active" / "graph.embeddings.bin"


def _journal(root: Path) -> Path:
    return root / "data" / "active" / "graph.journal.ndjson"


def test_an_ordinary_start_keeps_the_sidecar(data_setup_block, workspace):
    """The single most damaging mistake would be deleting it on every run."""
    _run(data_setup_block, workspace)

    assert _sidecar(workspace).exists()
    assert _sidecar(workspace).read_bytes() == SIDECAR_BYTES


def test_an_ordinary_start_keeps_the_journal(data_setup_block, workspace):
    """The journal holds mutations graph.json does not have yet. Deleting it
    on an ordinary start would lose every edit since the last checkpoint."""
    _run(data_setup_block, workspace)

    assert _journal(workspace).read_text() == JOURNAL_LINE


@pytest.mark.parametrize(
    "replace",
    [
        lambda ws: dict(data_source=str(ws / "replacement.json")),
        lambda ws: dict(remove_active=True),
    ],
    ids=["--data", "seeded-from-example"],
)
def test_replacing_the_graph_drops_the_journal(data_setup_block, workspace, replace):
    """Replayed onto a different graph, the journal would resurrect nodes of
    the old dataset and overwrite same-id nodes of the new one."""
    kwargs = replace(workspace)
    if kwargs.pop("remove_active", False):
        (workspace / "data" / "active" / "graph.json").unlink()

    _run(data_setup_block, workspace, **kwargs)

    assert not _journal(workspace).exists()


def test_a_missing_data_source_leaves_the_journal_alone(data_setup_block, workspace):
    _run(data_setup_block, workspace, data_source=str(workspace / "nope.json"))

    assert _journal(workspace).read_text() == JOURNAL_LINE


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
    assert _sidecar(workspace).read_bytes() == SIDECAR_BYTES


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
    configured.write_bytes(SIDECAR_BYTES)

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
    configured.write_bytes(SIDECAR_BYTES)

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
    assert "drop_stale_journal()" in data_setup_block
    # One definition plus one call per replacement path: the --data copy, the
    # --data download, the profile graph, the example graph, the empty stub.
    # An exact count means removing any single call fails here.
    assert data_setup_block.count("drop_stale_embeddings") == 6
    assert data_setup_block.count("drop_stale_journal") == 6


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
        # The download branch replaces the graph too, and a download is far
        # likelier to fail than a local copy — an unreachable host must not
        # cost the vectors of the graph still in place.
        'echo -e "${GREEN}Graph data downloaded to $ACTIVE_DATA${NC}"',
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


def test_a_failed_download_leaves_the_sidecar_alone(data_setup_block, workspace):
    """The graph was not replaced, so its vectors are still the right ones.
    Dropping them before the transfer succeeds loses them for nothing — and a
    download fails far more often than a local copy does."""
    output = _run(
        data_setup_block,
        workspace,
        data_source="http://127.0.0.1:1/does-not-exist.json",
    )

    assert _sidecar(workspace).exists(), output
    assert _sidecar(workspace).read_bytes() == SIDECAR_BYTES


@pytest.fixture
def path_without(tmp_path):
    """A PATH holding every executable the real one does, minus the named tools.

    `command -v curl` has to FAIL for the script to take its wget branch, and
    every CI runner and dev box here has curl - which is exactly why that
    branch had no coverage. Symlinking the rest of PATH keeps bash, wget, mv
    and cmp reachable while making curl genuinely absent rather than shadowed.
    """

    def _build(*hidden: str) -> str:
        shim = tmp_path / ("bin-without-" + "-".join(hidden))
        shim.mkdir()
        seen = set(hidden)
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            directory = Path(entry)
            if not directory.is_dir():
                continue
            for tool in directory.iterdir():
                if tool.name in seen or not os.access(tool, os.X_OK):
                    continue
                seen.add(tool.name)
                (shim / tool.name).symlink_to(tool)
        return str(shim)

    return _build


def _downloader_env(path_without, downloader: str) -> dict:
    """Force the script onto one download branch, skipping if it cannot."""
    if shutil.which(downloader) is None:
        pytest.skip(f"{downloader} required")
    hidden = {"curl": ("curl",), "wget": ("wget",)}
    other = "wget" if downloader == "curl" else "curl"
    return {"PATH": path_without(*hidden[other])}


@pytest.fixture
def http_404_url():
    """A URL that answers 404 with a body — the shape a mistyped URL takes."""
    import http.server
    import socketserver
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"<html><body>404 Not Found</body></html>")

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/missing.json"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("downloader", ["curl", "wget"])
def test_an_http_error_response_leaves_the_graph_and_sidecar_alone(
    data_setup_block, workspace, http_404_url, path_without, downloader
):
    """The likeliest way to reach this branch is a mistyped URL. Each
    downloader fails it differently, and both ways lost data: curl without -f
    exits 0 on a 404 and writes the error page over the graph, so the cleanup
    then deletes the vectors of the graph that was there; wget -O opens its
    target before the request, so a 404 empties the graph to zero bytes and
    THEN fails. A connection refusal is not the same test - that one fails the
    transport, so set -e stops the script before either can do harm."""
    _run(
        data_setup_block,
        workspace,
        data_source=http_404_url,
        env=_downloader_env(path_without, downloader),
    )

    assert _sidecar(workspace).exists(), (
        "an HTTP error was treated as a successful download and the sidecar was dropped"
    )
    assert _sidecar(workspace).read_bytes() == SIDECAR_BYTES
    assert _journal(workspace).read_text() == JOURNAL_LINE
    body = (workspace / "data" / "active" / "graph.json").read_bytes()
    assert body, f"{downloader} emptied the graph before its request failed"
    graph = json.loads(body)
    assert graph["nodes"] == [], "the 404 body was written over the graph"
    assert not list((workspace / "data" / "active").glob("*.download")), (
        "a failed transfer left its partial file behind"
    )


@pytest.mark.parametrize(
    "foreign",
    [
        pytest.param(b"\x80\x04\x95 a pickle", id="pickle"),
        pytest.param(b"CSV,header\nrow", id="shares-the-first-byte"),
        pytest.param(b"CKGEMB\x02rest", id="shares-six-of-seven"),
        pytest.param(b"lock", id="shorter-than-the-magic"),
    ],
)
def test_the_cleanup_discriminates_on_the_whole_magic(
    data_setup_block, resolution_block, workspace, foreign
):
    """One sample proves only that the guard rejects that sample. Comparing a
    single byte still rejects a pickle while deleting anything beginning with
    C, and a size threshold deletes short files."""
    configured = workspace / "elsewhere.bin"
    configured.write_bytes(foreign)

    _run(
        data_setup_block,
        workspace,
        data_source=str(workspace / "replacement.json"),
        env={"EMBEDDINGS_FILE": str(configured)},
        resolution=resolution_block,
    )

    assert configured.exists()
    assert configured.read_bytes() == foreign


def test_the_cleanup_refuses_to_delete_a_file_that_is_not_a_sidecar(
    data_setup_block, resolution_block, workspace
):
    """EMBEDDINGS_FILE may point at a legacy embeddings.pkl — the value the
    previous .env.example suggested. That file is not derived data and cannot
    be regenerated, so the cleanup must leave it where it is rather than
    treating the path as its own."""
    foreign = workspace / "embeddings.pkl"
    original = b"\x80\x04\x95 a pickle, not a sidecar"
    foreign.write_bytes(original)

    output = _run(
        data_setup_block,
        workspace,
        data_source=str(workspace / "replacement.json"),
        env={"EMBEDDINGS_FILE": str(foreign)},
        resolution=resolution_block,
    )

    assert foreign.exists(), output
    assert foreign.read_bytes() == original
    assert "not an embedding sidecar" in output


@pytest.fixture
def http_graph_url(workspace):
    """A URL that serves a real graph — the success path both other download
    tests skip, since one refuses the connection and one answers 404."""
    import http.server
    import socketserver
    import threading

    body = json.dumps(
        {
            "nodes": [{"id": "downloaded", "type": "Actor", "name": "From the URL"}],
            "edges": [],
            "metadata": {"version": "1.0"},
        }
    ).encode("utf-8")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/graph.json"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("downloader", ["curl", "wget"])
def test_a_successful_download_replaces_the_graph_and_drops_the_sidecar(
    data_setup_block, workspace, http_graph_url, path_without, downloader
):
    """Both other download tests are failure paths, so nothing checked that a
    download which reports success actually put the graph where the cleanup
    then assumes it is. A download landing somewhere else would still drop the
    sidecar, leaving the old graph beside no vectors."""
    _run(
        data_setup_block,
        workspace,
        data_source=http_graph_url,
        env=_downloader_env(path_without, downloader),
    )

    graph = json.loads((workspace / "data" / "active" / "graph.json").read_text())
    assert [n["id"] for n in graph["nodes"]] == ["downloaded"], (
        "the download reported success but the graph was not replaced"
    )
    assert not _sidecar(workspace).exists()
    assert not list((workspace / "data" / "active").glob("*.download")), (
        "the transfer was copied into place rather than moved"
    )
