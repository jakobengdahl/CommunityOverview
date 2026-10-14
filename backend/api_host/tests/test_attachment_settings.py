"""Boot-time attachment settings (ADR 0008 sections 1, 4, 5 and 11)."""

from types import SimpleNamespace

import pytest

from backend.api_host.attachments import (
    AttachmentConfigurationError,
    build_attachment_settings,
)
from backend.api_host.config import AppConfig
from backend.core.attachment_store import (
    DEFAULT_MAX_BYTES_PER_NODE,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_FILES_PER_NODE,
    FileAttachmentStore,
)
from backend.core.attachment_types import PDF, SUPPORTED_TYPES, TEXT


@pytest.fixture(autouse=True)
def _no_ambient_attachment_settings(monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("ATTACHMENT_") or name.startswith("GRAPH_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def storage(tmp_path):
    return SimpleNamespace(json_path=tmp_path / "data" / "mygraph.json")


def test_defaults_are_the_decided_limits_beside_the_graph_file(storage, tmp_path):
    settings = build_attachment_settings(AppConfig(), storage)

    assert settings.limits.max_file_bytes == DEFAULT_MAX_FILE_BYTES == 25 * 1024**2
    assert settings.limits.max_files_per_node == DEFAULT_MAX_FILES_PER_NODE == 10
    assert (
        settings.limits.max_bytes_per_node
        == DEFAULT_MAX_BYTES_PER_NODE
        == 100 * 1024**2
    )
    assert settings.allowed_types == SUPPORTED_TYPES
    assert settings.namespace == "mygraph"
    assert isinstance(settings.store, FileAttachmentStore)
    assert settings.store.root == tmp_path / "data" / "mygraph.attachments"


def test_operator_settings_are_honoured(storage, tmp_path):
    config = AppConfig(
        attachment_dir=str(tmp_path / "files"),
        attachment_namespace="ns-1",
        attachment_max_file_bytes="10",
        attachment_max_files_per_node="2",
        attachment_max_bytes_per_node="15",
        attachment_allowed_types="application/pdf,text/plain",
    )
    settings = build_attachment_settings(config, storage)

    assert settings.store.root == tmp_path / "files"
    assert settings.namespace == "ns-1"
    assert settings.limits.to_dict() == {
        "max_file_bytes": 10,
        "max_files_per_node": 2,
        "max_bytes_per_node": 15,
    }
    assert settings.allowed_types == {PDF, TEXT}


@pytest.mark.parametrize(
    "field",
    [
        "attachment_max_file_bytes",
        "attachment_max_files_per_node",
        "attachment_max_bytes_per_node",
    ],
)
@pytest.mark.parametrize("value", ["0", "-5", "lots", "1.5"])
def test_unusable_limit_is_refused_at_boot(storage, field, value):
    variable = field.upper()
    with pytest.raises(AttachmentConfigurationError, match=variable):
        build_attachment_settings(AppConfig(**{field: value}), storage)


def test_unknown_backend_is_refused_at_boot(storage):
    with pytest.raises(AttachmentConfigurationError, match="ATTACHMENT_BACKEND"):
        build_attachment_settings(AppConfig(attachment_backend="s3"), storage)


def test_allowlist_entry_outside_the_table_is_refused_at_boot(storage):
    with pytest.raises(AttachmentConfigurationError, match="image/svg"):
        build_attachment_settings(
            AppConfig(attachment_allowed_types="image/svg+xml"), storage
        )


def test_postgres_without_attachment_dir_leaves_attachments_unavailable(storage):
    config = AppConfig(graph_backend="postgres")
    assert build_attachment_settings(config, storage) is None


def test_postgres_namespace_defaults_to_the_schema(storage, tmp_path):
    config = AppConfig(
        graph_backend="postgres",
        graph_postgres_schema="tenant_a",
        attachment_dir=str(tmp_path / "shared"),
    )
    settings = build_attachment_settings(config, storage)
    assert settings.namespace == "pg:tenant_a"
    assert settings.store.root == tmp_path / "shared"


def test_boot_sweeps_stale_staged_uploads(storage, tmp_path):
    import os
    import time

    tmp = tmp_path / "data" / "mygraph.attachments" / ".tmp"
    tmp.mkdir(parents=True)
    stale = tmp / "upload_stale"
    stale.write_bytes(b"x")
    old = time.time() - 7200
    os.utime(stale, (old, old))

    build_attachment_settings(AppConfig(), storage)
    assert not stale.exists()
