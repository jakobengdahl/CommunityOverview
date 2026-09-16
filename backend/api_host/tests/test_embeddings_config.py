"""
EMBEDDINGS_FILE selects where the embedding sidecar is written.

The path matters operationally: resolved wrongly, a deployment writes its
vectors somewhere other than the mounted data volume and orphans the ones it
already has, with nothing failing to say so.
"""

import os
import tempfile
from pathlib import Path

from backend.api_host.config import AppConfig
from backend.api_host.server import create_app


def test_unset_means_derive_the_path():
    config = AppConfig(graph_file="/data/graph.json", embeddings_file=None)

    assert config.get_embeddings_path() is None


def test_absolute_path_is_used_as_given():
    config = AppConfig(
        graph_file="/data/graph.json", embeddings_file="/elsewhere/vectors.bin"
    )

    assert config.get_embeddings_path() == Path("/elsewhere/vectors.bin")


def test_relative_path_resolves_against_the_graph_directory():
    """So the graph and its vectors stay together on a mounted data volume,
    rather than the sidecar landing in the process working directory."""
    config = AppConfig(graph_file="/data/graph.json", embeddings_file="vectors.bin")

    assert config.get_embeddings_path() == Path("/data/vectors.bin")


def test_configured_path_reaches_the_graph_storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        graph_file = os.path.join(tmpdir, "graph.json")
        sidecar = os.path.join(tmpdir, "custom-vectors.bin")
        config = AppConfig(graph_file=graph_file, embeddings_file=sidecar)

        app = create_app(config)
        storage = app.state.graph_storage

        assert storage.embeddings_path == Path(sidecar)
        storage.flush()


def test_the_environment_variable_is_read_when_nothing_is_passed(monkeypatch):
    """Every other test hands embeddings_file in explicitly, so the field's
    default - reading EMBEDDINGS_FILE - was exercised nowhere. The app could
    have silently ignored the variable while both maintenance scripts and
    start-dev.sh honoured it, and the pair would drift apart on the volume."""
    monkeypatch.setenv("GRAPH_FILE", "/data/graph.json")
    monkeypatch.setenv("EMBEDDINGS_FILE", "vectors.bin")

    config = AppConfig.from_env()

    assert config.embeddings_file == "vectors.bin"
    assert config.get_embeddings_path() == Path("/data/vectors.bin")


def test_an_unset_environment_variable_means_derive(monkeypatch):
    monkeypatch.setenv("GRAPH_FILE", "/data/graph.json")
    monkeypatch.delenv("EMBEDDINGS_FILE", raising=False)

    config = AppConfig.from_env()

    assert config.embeddings_file is None
    assert config.get_embeddings_path() is None
