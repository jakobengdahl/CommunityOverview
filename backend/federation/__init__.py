"""Federation package."""

from .config import (
    FederationFileConfig,
    FederationSettings,
    FederationGraphConfig,
    load_federation_config,
    resolve_federation_config_path,
    summarize_federation_config,
)

__all__ = [
    "FederationManager",
    "FederationFileConfig",
    "FederationSettings",
    "FederationGraphConfig",
    "load_federation_config",
    "resolve_federation_config_path",
    "summarize_federation_config",
]

from .manager import FederationManager
