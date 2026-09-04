"""
Tests for the embedding maintenance scripts.

Both write the only copy of the vectors, and both grew defects that two review
rounds missed because nothing exercised them: the pickle migration crashed on
the very case it was rewritten for, and neither script honoured EMBEDDINGS_FILE,
so on a deployment that sets it they wrote to a sidecar the app never reads.
"""

import json
import os
import pickle
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# scripts/ is not a package on the default path; the repo root above makes
# "scripts.migrate_embeddings" importable the same way the CLI entry point is.

from backend.core.embedding_sidecar import (  # noqa: E402
    FileEmbeddingSidecar,
    resolve_sidecar_path,
)
from scripts.migrate_embeddings import migrate_embeddings  # noqa: E402

DIM = 8


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        graph = {
            "nodes": [
                {"id": "n1", "type": "Actor", "name": "One"},
                {"id": "n2", "type": "Actor", "name": "Two"},
            ],
            "edges": [],
            "metadata": {"version": "1.0", "graph_name": "graph"},
        }
        graph_path = Path(tmpdir) / "graph.json"
        graph_path.write_text(json.dumps(graph), encoding="utf-8")
        yield Path(tmpdir), graph_path


def _write_pickle(directory: Path, embeddings: dict) -> None:
    with open(directory / "embeddings.pkl", "wb") as f:
        pickle.dump({"embeddings": embeddings}, f)


class TestResolveSidecarPath:
    def test_unset_means_derive_from_the_graph(self, monkeypatch):
        monkeypatch.delenv("EMBEDDINGS_FILE", raising=False)

        assert resolve_sidecar_path("/data/graph.json") is None

    def test_env_var_is_honoured(self, monkeypatch):
        monkeypatch.setenv("EMBEDDINGS_FILE", "/vol/vectors.bin")

        assert resolve_sidecar_path("/data/graph.json") == Path("/vol/vectors.bin")

    def test_relative_env_var_resolves_against_the_graph_directory(self, monkeypatch):
        monkeypatch.setenv("EMBEDDINGS_FILE", "vectors.bin")

        assert resolve_sidecar_path("/data/graph.json") == Path("/data/vectors.bin")

    def test_an_explicit_argument_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv("EMBEDDINGS_FILE", "/vol/from-env.bin")

        assert resolve_sidecar_path("/data/graph.json", "/vol/explicit.bin") == Path(
            "/vol/explicit.bin"
        )


class TestMigrateEmbeddings:
    def test_migrates_pickle_vectors_into_the_sidecar(self, workspace, monkeypatch):
        tmpdir, graph_path = workspace
        monkeypatch.delenv("EMBEDDINGS_FILE", raising=False)
        _write_pickle(tmpdir, {"n1": np.arange(DIM, dtype=np.float32)})

        migrate_embeddings(str(graph_path))

        sidecar = FileEmbeddingSidecar(tmpdir / "graph.embeddings.bin")
        np.testing.assert_allclose(
            sidecar.load()["n1"], np.arange(DIM, dtype=np.float32)
        )
        assert (tmpdir / "embeddings.pkl.bak").exists()

    def test_keeps_vectors_the_sidecar_already_had(self, workspace, monkeypatch):
        """A rebuild rather than a merge would silently drop n2's vector."""
        tmpdir, graph_path = workspace
        monkeypatch.delenv("EMBEDDINGS_FILE", raising=False)
        sidecar = FileEmbeddingSidecar(tmpdir / "graph.embeddings.bin")
        sidecar.save({"n2": np.full(DIM, 9.0, dtype=np.float32)})
        _write_pickle(tmpdir, {"n1": np.arange(DIM, dtype=np.float32)})

        migrate_embeddings(str(graph_path))

        assert set(sidecar.load()) == {"n1", "n2"}
        np.testing.assert_allclose(
            sidecar.load()["n2"], np.full(DIM, 9.0, dtype=np.float32)
        )

    def test_a_pickle_of_another_dimension_does_not_crash_the_migration(
        self, workspace, monkeypatch
    ):
        """The sidecar's dimension wins; the mismatched pickle rows are skipped
        rather than raising a numpy traceback at the operator."""
        tmpdir, graph_path = workspace
        monkeypatch.delenv("EMBEDDINGS_FILE", raising=False)
        sidecar = FileEmbeddingSidecar(tmpdir / "graph.embeddings.bin")
        sidecar.save({"n2": np.full(DIM, 9.0, dtype=np.float32)})
        _write_pickle(tmpdir, {"n1": np.zeros(DIM + 4, dtype=np.float32)})

        migrate_embeddings(str(graph_path))

        assert set(sidecar.load()) == {"n2"}

    def test_writes_to_the_configured_sidecar_not_the_default_location(
        self, workspace, monkeypatch
    ):
        """Otherwise the whole migration is invisible to the running app."""
        tmpdir, graph_path = workspace
        configured = tmpdir / "configured.bin"
        monkeypatch.setenv("EMBEDDINGS_FILE", str(configured))
        _write_pickle(tmpdir, {"n1": np.arange(DIM, dtype=np.float32)})

        migrate_embeddings(str(graph_path))

        assert configured.exists()
        assert not (tmpdir / "graph.embeddings.bin").exists()
        assert set(FileEmbeddingSidecar(configured).load()) == {"n1"}

    def test_a_missing_pickle_is_a_no_op(self, workspace, monkeypatch):
        tmpdir, graph_path = workspace
        monkeypatch.delenv("EMBEDDINGS_FILE", raising=False)

        migrate_embeddings(str(graph_path))

        assert not (tmpdir / "graph.embeddings.bin").exists()


class TestGenerateEmbeddings:
    def test_writes_to_the_configured_sidecar(self, workspace, monkeypatch):
        """generate_embeddings needs the ML extras to embed, but the path it
        would write to is decided before any model is loaded."""
        tmpdir, graph_path = workspace
        configured = tmpdir / "configured.bin"
        monkeypatch.setenv("EMBEDDINGS_FILE", str(configured))

        from backend.core import GraphStorage

        sidecar_path = resolve_sidecar_path(str(graph_path), None)
        storage = GraphStorage(
            json_path=str(graph_path), embeddings_path=str(sidecar_path)
        )
        try:
            assert storage.embeddings_path == configured
        finally:
            storage.flush()


def test_scripts_and_app_agree_on_the_sidecar_location(workspace, monkeypatch):
    """The scripts must resolve EMBEDDINGS_FILE exactly as AppConfig does, or
    they act on a different file than the one the app reads."""
    tmpdir, graph_path = workspace
    monkeypatch.setenv("EMBEDDINGS_FILE", "vectors.bin")

    from backend.api_host.config import AppConfig

    config = AppConfig(graph_file=str(graph_path), embeddings_file="vectors.bin")

    assert resolve_sidecar_path(str(graph_path)) == config.get_embeddings_path()
    assert os.path.basename(str(config.get_embeddings_path())) == "vectors.bin"
