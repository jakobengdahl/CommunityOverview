# covers: backend/api_host/config.py, backend/api_host/server.py
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


def test_webhook_database_uses_private_state_and_rejects_import_collision(
    tmp_path, monkeypatch
):
    import pytest

    private = tmp_path / "private"
    monkeypatch.setenv("COMMUNITYOVERVIEW_PRIVATE_STATE_DIR", str(private))
    config = AppConfig(graph_file=str(tmp_path / "graph" / "graph.json"))
    assert config.get_webhook_tokens_db_path() == private / "webhook_tokens.db"
    config.import_jobs_db = str(private / "webhook_tokens.db")
    with pytest.raises(ValueError, match="must be separate"):
        config.get_webhook_tokens_db_path()

    tenant = tmp_path / "tenant"
    tenant.mkdir()
    alias = tmp_path / "tenant-alias"
    alias.symlink_to(tenant, target_is_directory=True)
    monkeypatch.setenv("COMMUNITYOVERVIEW_TENANT_CONFIG_DIR", str(tenant))
    for state in (tenant, tenant / "nested", alias / "nested"):
        monkeypatch.setenv("COMMUNITYOVERVIEW_PRIVATE_STATE_DIR", str(state))
        with pytest.raises(ValueError, match="outside the tenant config directory"):
            config.get_webhook_tokens_db_path()

    monkeypatch.delenv("COMMUNITYOVERVIEW_PRIVATE_STATE_DIR")
    monkeypatch.setenv("HOME", str(tenant))
    config.import_jobs_db = None
    first = config.get_webhook_tokens_db_path()
    assert not first.resolve().is_relative_to(tenant.resolve())
    assert first == config.get_webhook_tokens_db_path()
    other = AppConfig(graph_file=str(tmp_path / "other" / "graph.json"))
    assert first != other.get_webhook_tokens_db_path()
    pg = AppConfig(
        graph_backend="postgres",
        graph_postgres_dsn="secret-dsn",
        graph_postgres_schema="one",
        graph_postgres_scope=None,
    )
    pg_other = AppConfig(
        graph_backend="postgres",
        graph_postgres_dsn="secret-dsn",
        graph_postgres_schema="two",
        graph_postgres_scope=None,
    )
    assert pg.get_webhook_tokens_db_path() != pg_other.get_webhook_tokens_db_path()
    assert "secret-dsn" not in str(pg.get_webhook_tokens_db_path())


def test_webhook_database_default_outside_graph_when_home_is_inside(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("COMMUNITYOVERVIEW_PRIVATE_STATE_DIR", raising=False)
    graph_dir = tmp_path / "graph"
    monkeypatch.setenv("HOME", str(graph_dir / "home"))
    config = AppConfig(graph_file=str(graph_dir / "graph.json"))

    assert (
        not config.get_webhook_tokens_db_path()
        .resolve()
        .is_relative_to(graph_dir.resolve())
    )
