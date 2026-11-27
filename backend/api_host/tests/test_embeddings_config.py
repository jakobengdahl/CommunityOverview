# covers: backend/api_host/config.py, backend/api_host/server.py
"""
EMBEDDINGS_FILE selects where the embedding sidecar is written.

The path matters operationally: resolved wrongly, a deployment writes its
vectors somewhere other than the mounted data volume and orphans the ones it
already has, with nothing failing to say so.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.api_host.config import AppConfig
from backend.api_host.server import create_app


@pytest.mark.parametrize("store", ["import", "history"])
def test_schedule_ledger_avoids_execution_store_and_survives_restart(
    tmp_path, monkeypatch, store
):
    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setenv("COMMUNITYOVERVIEW_PRIVATE_STATE_DIR", str(private))
    monkeypatch.delenv("AGENTS_RUN_HISTORY_DB", raising=False)
    _configure_function(tmp_path, monkeypatch, "schedule")
    collision = private / "function-schedule-slots.db"
    config = AppConfig(graph_file=str(tmp_path / "graph" / "graph.json"))
    if store == "import":
        config.import_jobs_db = str(collision)
    else:
        monkeypatch.setenv("AGENTS_RUN_HISTORY_DB", str(collision))
    fire = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = create_app(config)
    job = first.state.function_scheduler.slots.enqueue("example", fire)
    second = create_app(config)
    assert second.state.function_scheduler.slots.enqueue("example", fire).id == job.id


def _configure_function(tmp_path, monkeypatch, trigger):
    root = tmp_path / "functions"
    directory = root / "example"
    directory.mkdir(parents=True)
    manifest = {
        "contract_version": "5",
        "id": "example",
        "version": "1.0.0",
        "name": "Example",
        "runtime": "python",
        "entry_point": "main.py",
        "triggers": (
            [{"kind": "schedule", "interval_seconds": 60}]
            if trigger == "schedule"
            else [{"kind": "manual"}]
        ),
        "grants": [],
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
    }
    (directory / "function.json").write_text(json.dumps(manifest))
    (directory / "main.py").write_text("pass")
    monkeypatch.setenv("COMMUNITYOVERVIEW_FUNCTIONS_ROOT", str(root))
    monkeypatch.setenv("COMMUNITYOVERVIEW_FUNCTIONS_ENABLED", '{"example": []}')
    monkeypatch.setenv("AGENTS_RUN_HISTORY_DB", str(tmp_path / "history.db"))


def test_invalid_schedule_ledger_is_not_modified(tmp_path, monkeypatch):
    import sqlite3

    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setenv("COMMUNITYOVERVIEW_PRIVATE_STATE_DIR", str(private))
    _configure_function(tmp_path, monkeypatch, "manual")
    ledger = private / "webhook_tokens.schedule_slots.db"
    ledger.write_bytes(b"not a sqlite database")

    with pytest.raises(sqlite3.DatabaseError):
        create_app(AppConfig(graph_file=str(tmp_path / "graph" / "graph.json")))
    assert ledger.read_bytes() == b"not a sqlite database"


@pytest.mark.parametrize("initial", ["manual", "invalid"])
def test_scheduler_discovers_manifest_fixed_after_startup(
    tmp_path, monkeypatch, initial
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from backend.agents.execution import ExecutionState

    _configure_function(tmp_path, monkeypatch, "manual")
    monkeypatch.setenv(
        "COMMUNITYOVERVIEW_FUNCTIONS_SCHEDULE_GRAPHS", '{"example": "graph"}'
    )
    manifest_path = tmp_path / "functions" / "example" / "function.json"
    manifest = json.loads(manifest_path.read_text())
    if initial == "invalid":
        manifest_path.write_text("invalid")
    app = create_app(AppConfig(graph_file=str(tmp_path / "graph" / "graph.json")))
    scheduler = app.state.function_scheduler
    scheduler.service.invoke = Mock()
    now = datetime.now(timezone.utc)
    scheduler.tick(now)
    scheduler.service.invoke.assert_not_called()
    manifest["triggers"] = [{"kind": "schedule", "interval_seconds": 60}]
    manifest_path.write_text(json.dumps(manifest))
    scheduler.service.invoke.return_value = SimpleNamespace(
        state=ExecutionState.SUCCEEDED
    )
    scheduler.tick(now)
    assert scheduler.service.invoke.call_count == 1
    scheduler.stop()


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

    config.import_jobs_db = None
    tenant = tmp_path / "tenant"
    tenant.mkdir()
    alias = tmp_path / "tenant-alias"
    alias.symlink_to(tenant, target_is_directory=True)
    monkeypatch.setenv("COMMUNITYOVERVIEW_TENANT_CONFIG_DIR", str(tenant))
    for state in (tenant, tenant / "nested", alias / "nested"):
        monkeypatch.setenv("COMMUNITYOVERVIEW_PRIVATE_STATE_DIR", str(state))
        with pytest.raises(ValueError, match="outside the tenant config directory"):
            create_app(config)

    monkeypatch.delenv("COMMUNITYOVERVIEW_PRIVATE_STATE_DIR")
    monkeypatch.setenv("HOME", str(tenant))
    config.import_jobs_db = None
    first = config.get_webhook_tokens_db_path(excluded_dirs=[tenant])
    assert not first.resolve().is_relative_to(tenant.resolve())
    assert first == config.get_webhook_tokens_db_path(excluded_dirs=[tenant])
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
        not config.get_webhook_tokens_db_path(excluded_dirs=[graph_dir])
        .resolve()
        .is_relative_to(graph_dir.resolve())
    )

    import pwd
    from types import SimpleNamespace

    import pytest

    monkeypatch.setattr(
        pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=str(graph_dir))
    )
    with pytest.raises(ValueError, match="No private state directory"):
        config.get_webhook_tokens_db_path(excluded_dirs=[graph_dir])


def test_webhook_database_rejects_actual_graph_and_config_directories(
    tmp_path, monkeypatch
):
    import pytest
    from backend.core import GraphStorage

    graph_dir = tmp_path / "actual-graph"
    graph_dir.mkdir()
    storage = GraphStorage(str(graph_dir / "graph.json"))
    config = AppConfig(graph_file=str(tmp_path / "other" / "graph.json"))
    for name, env in (
        ("graph", None),
        ("tenant config", "COMMUNITYOVERVIEW_TENANT_CONFIG_DIR"),
        ("schema config", "SCHEMA_FILE"),
        ("federation config", "FEDERATION_FILE"),
    ):
        directory = graph_dir if env is None else tmp_path / name.replace(" ", "-")
        directory.mkdir(exist_ok=True)
        alias = tmp_path / (name.replace(" ", "-") + "-alias")
        alias.symlink_to(directory, target_is_directory=True)
        if env:
            monkeypatch.setenv(
                env,
                str(
                    directory if name == "tenant config" else directory / "config.json"
                ),
            )
        monkeypatch.setenv("COMMUNITYOVERVIEW_PRIVATE_STATE_DIR", str(alias / "nested"))
        with pytest.raises(ValueError, match=f"outside the {name} directory"):
            create_app(config, graph_storage=storage)
        if env:
            monkeypatch.delenv(env)
    monkeypatch.delenv("COMMUNITYOVERVIEW_PRIVATE_STATE_DIR")
    (tmp_path / "other").mkdir(exist_ok=True)
    app = create_app(config, graph_storage=storage)
    actual_path = app.state.function_invocation_service.webhook_token_store.path
    other_storage = GraphStorage(str(tmp_path / "second-graph" / "graph.json"))
    other_app = create_app(config, graph_storage=other_storage)
    assert (
        actual_path
        != other_app.state.function_invocation_service.webhook_token_store.path
    )
    other_storage.shutdown_events()
    storage.shutdown_events()


@pytest.mark.parametrize("host", [None, "127.0.0.1"])
def test_server_binds_configured_host_and_default(monkeypatch, host):
    from unittest.mock import Mock

    import uvicorn

    from backend.api_host.server import main

    if host is None:
        monkeypatch.delenv("HOST", raising=False)
    else:
        monkeypatch.setenv("HOST", host)
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    main()
    assert run.call_args.kwargs["host"] == (host or "0.0.0.0")
