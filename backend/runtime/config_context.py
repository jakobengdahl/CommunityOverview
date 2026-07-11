"""Shared helpers for resolving public configuration context."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable

TENANT_CONFIG_DIR_ENV = "COMMUNITYOVERVIEW_TENANT_CONFIG_DIR"

SCHEMA_CONFIG_FILENAME = "schema_config.json"
FEDERATION_CONFIG_FILENAME = "federation_config.json"

SCHEMA_EXPLICIT_ENV_VARS = ("SCHEMA_FILE", "GRAPH_SCHEMA_CONFIG")
FEDERATION_EXPLICIT_ENV_VARS = ("FEDERATION_FILE", "GRAPH_FEDERATION_CONFIG")


def get_project_root() -> Path:
    """Return the repository root for backend-relative config defaults."""
    return Path(__file__).resolve().parent.parent


def normalize_config_path(path: str) -> str:
    """Normalize a config path for public reporting and file access."""
    return str(Path(path).expanduser().resolve(strict=False))


def get_tenant_config_dir() -> str:
    """Return the normalized tenant config directory, if configured."""
    raw_value = (os.getenv(TENANT_CONFIG_DIR_ENV) or "").strip()
    if not raw_value:
        return ""
    return normalize_config_path(raw_value)


def _resolve_default_path(default_relative_path: str, *, allow_cwd_fallback: bool) -> str:
    default_path = get_project_root() / default_relative_path
    if default_path.exists() or not allow_cwd_fallback:
        return str(default_path)

    cwd_path = Path.cwd() / default_relative_path
    if cwd_path.exists():
        return str(cwd_path)

    return str(default_path)


def resolve_config_path(
    *,
    explicit_env_vars: Iterable[str],
    tenant_filename: str,
    default_relative_path: str,
    allow_cwd_fallback: bool = False,
) -> Dict[str, str]:
    """Resolve a config path with explicit-env and tenant-dir precedence."""
    tenant_config_dir = get_tenant_config_dir()

    for env_var in explicit_env_vars:
        env_value = (os.getenv(env_var) or "").strip()
        if env_value:
            return {
                "path": normalize_config_path(env_value),
                "source": "explicit_env",
                "tenant_config_dir": tenant_config_dir,
            }

    if tenant_config_dir:
        return {
            "path": normalize_config_path(str(Path(tenant_config_dir) / tenant_filename)),
            "source": "tenant_config_dir",
            "tenant_config_dir": tenant_config_dir,
        }

    return {
        "path": normalize_config_path(
            _resolve_default_path(
                default_relative_path,
                allow_cwd_fallback=allow_cwd_fallback,
            )
        ),
        "source": "default",
        "tenant_config_dir": "",
    }


def resolve_schema_config_path_info(default_relative_path: str) -> Dict[str, str]:
    """Resolve the schema config path and its public source metadata."""
    return resolve_config_path(
        explicit_env_vars=SCHEMA_EXPLICIT_ENV_VARS,
        tenant_filename=SCHEMA_CONFIG_FILENAME,
        default_relative_path=default_relative_path,
        allow_cwd_fallback=True,
    )


def resolve_federation_config_path_info(default_relative_path: str) -> Dict[str, str]:
    """Resolve the federation config path and its public source metadata."""
    return resolve_config_path(
        explicit_env_vars=FEDERATION_EXPLICIT_ENV_VARS,
        tenant_filename=FEDERATION_CONFIG_FILENAME,
        default_relative_path=default_relative_path,
        allow_cwd_fallback=False,
    )
