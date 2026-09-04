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
from scripts.generate_embeddings import generate_embeddings  # noqa: E402
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

    def test_an_empty_env_var_means_derive_not_the_current_directory(self, monkeypatch):
        """EMBEDDINGS_FILE= (exported but empty) is common in compose files and
        shell wrappers. Treated as a path it becomes the graph's own directory
        and every write fails."""
        monkeypatch.setenv("EMBEDDINGS_FILE", "")

        assert resolve_sidecar_path("/data/graph.json") is None

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

    def test_a_larger_pickle_still_does_not_outvote_the_sidecar(
        self, workspace, monkeypatch
    ):
        """One-against-one is a tie, and a tie resolves to the sidecar whether
        the rule is 'the sidecar decides' or 'the majority decides'. Only a
        pickle that OUTNUMBERS the sidecar separates them — and getting it wrong
        evicts vectors whose only durable copy is the sidecar, immediately
        before the pickle is renamed away."""
        tmpdir, graph_path = workspace
        monkeypatch.delenv("EMBEDDINGS_FILE", raising=False)
        # A pickle vector only reaches the merge through a node that exists, so
        # the graph needs a third node for the pickle to outnumber the sidecar.
        graph_path.write_text(
            json.dumps(
                {
                    "nodes": [
                        {"id": "n1", "type": "Actor", "name": "One"},
                        {"id": "n2", "type": "Actor", "name": "Two"},
                        {"id": "n3", "type": "Actor", "name": "Three"},
                    ],
                    "edges": [],
                    "metadata": {"version": "1.0", "graph_name": "graph"},
                }
            ),
            encoding="utf-8",
        )
        sidecar = FileEmbeddingSidecar(tmpdir / "graph.embeddings.bin")
        sidecar.save({"n3": np.full(DIM, 9.0, dtype=np.float32)})
        _write_pickle(
            tmpdir,
            {
                "n1": np.zeros(DIM + 4, dtype=np.float32),
                "n2": np.ones(DIM + 4, dtype=np.float32),
            },
        )

        migrate_embeddings(str(graph_path))

        assert set(sidecar.load()) == {"n3"}, (
            "the more numerous pickle rows outvoted the sidecar and destroyed "
            "the vectors it was the only durable copy of"
        )

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
        """Calls the script itself. The embedding generation is stubbed — that
        is the only part needing the ML extras — so the path resolution, the
        save and the success reporting are all the script's own code."""
        tmpdir, graph_path = workspace
        configured = tmpdir / "configured.bin"
        monkeypatch.setenv("EMBEDDINGS_FILE", str(configured))

        from backend.core.vector_store import VectorStore

        def fake_update(self, nodes):
            for i, node in enumerate(nodes):
                self.embeddings[node.id] = np.full(DIM, float(i), dtype=np.float32)
            self._update_matrix()

        monkeypatch.setattr(VectorStore, "update_nodes_embeddings", fake_update)

        generate_embeddings(str(graph_path))

        assert configured.exists()
        assert not (tmpdir / "graph.embeddings.bin").exists()
        assert set(FileEmbeddingSidecar(configured).load()) == {"n1", "n2"}

    def test_an_explicit_argument_overrides_the_environment(
        self, workspace, monkeypatch
    ):
        tmpdir, graph_path = workspace
        monkeypatch.setenv("EMBEDDINGS_FILE", str(tmpdir / "from-env.bin"))
        explicit = tmpdir / "explicit.bin"

        from backend.core.vector_store import VectorStore

        def fake_update(self, nodes):
            for node in nodes:
                self.embeddings[node.id] = np.zeros(DIM, dtype=np.float32)
            self._update_matrix()

        monkeypatch.setattr(VectorStore, "update_nodes_embeddings", fake_update)

        generate_embeddings(str(graph_path), str(explicit))

        assert explicit.exists()
        assert not (tmpdir / "from-env.bin").exists()


def test_scripts_and_app_agree_on_the_sidecar_location(workspace, monkeypatch):
    """The scripts must resolve EMBEDDINGS_FILE exactly as AppConfig does, or
    they act on a different file than the one the app reads."""
    tmpdir, graph_path = workspace
    monkeypatch.setenv("EMBEDDINGS_FILE", "vectors.bin")

    from backend.api_host.config import AppConfig

    config = AppConfig(graph_file=str(graph_path), embeddings_file="vectors.bin")

    assert resolve_sidecar_path(str(graph_path)) == config.get_embeddings_path()
    assert os.path.basename(str(config.get_embeddings_path())) == "vectors.bin"

    # ... and they must still agree when the variable is exported but empty.
    monkeypatch.setenv("EMBEDDINGS_FILE", "")
    empty_config = AppConfig(graph_file=str(graph_path), embeddings_file="")
    assert resolve_sidecar_path(str(graph_path)) is None
    assert empty_config.get_embeddings_path() is None


class TestFailedSidecarWrites:
    """A sidecar write failure does not fail the graph save, by design. Both
    scripts report to an operator, and one of them renames its own source on the
    strength of that report, so neither may infer success from save() returning."""

    @staticmethod
    def _break_sidecar(monkeypatch):
        from backend.core.embedding_sidecar import FileEmbeddingSidecar

        def failing_save(self, vectors):
            raise OSError("no space left on device")

        monkeypatch.setattr(FileEmbeddingSidecar, "save", failing_save)

    @staticmethod
    def _stub_encoder(monkeypatch):
        from backend.core.vector_store import VectorStore

        def fake_update(self, nodes):
            self._absorb({node.id: np.zeros(DIM, dtype=np.float32) for node in nodes})

        monkeypatch.setattr(VectorStore, "update_nodes_embeddings", fake_update)

    def test_generate_embeddings_reports_failure(self, workspace, monkeypatch):
        tmpdir, graph_path = workspace
        monkeypatch.delenv("EMBEDDINGS_FILE", raising=False)
        self._stub_encoder(monkeypatch)
        self._break_sidecar(monkeypatch)

        with pytest.raises(SystemExit) as exit_info:
            generate_embeddings(str(graph_path))

        assert exit_info.value.code == 1
        assert not (tmpdir / "graph.embeddings.bin").exists()

    def test_migrate_embeddings_keeps_the_pickle_when_the_write_fails(
        self, workspace, monkeypatch
    ):
        """Renaming it away would destroy the last copy of the vectors, and a
        re-run would then report there is nothing to migrate."""
        tmpdir, graph_path = workspace
        monkeypatch.delenv("EMBEDDINGS_FILE", raising=False)
        _write_pickle(tmpdir, {"n1": np.arange(DIM, dtype=np.float32)})
        self._break_sidecar(monkeypatch)

        with pytest.raises(SystemExit) as exit_info:
            migrate_embeddings(str(graph_path))

        assert exit_info.value.code == 1
        assert (tmpdir / "embeddings.pkl").exists()
        assert not (tmpdir / "embeddings.pkl.bak").exists()


def test_migration_refuses_to_write_the_sidecar_over_its_own_source(
    workspace, monkeypatch
):
    """EMBEDDINGS_FILE resolving onto the legacy pickle would have the script
    write the sidecar there and then rename it away as the 'old pickle'."""
    tmpdir, graph_path = workspace
    monkeypatch.setenv("EMBEDDINGS_FILE", str(tmpdir / "embeddings.pkl"))
    _write_pickle(tmpdir, {"n1": np.arange(DIM, dtype=np.float32)})

    migrate_embeddings(str(graph_path))

    assert (tmpdir / "embeddings.pkl").exists()
    assert not (tmpdir / "embeddings.pkl.bak").exists()
    with open(tmpdir / "embeddings.pkl", "rb") as f:
        assert f.read(2) != b"CK"
