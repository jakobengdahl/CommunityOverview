"""Process-local state isolation shared by all backend test directories."""

import pytest

from backend.config import config_loader


@pytest.fixture(autouse=True)
def preserve_config_loader():
    """A temporary cwd or SCHEMA_FILE must not seed the next test's schema.

    ConfigLoader has both a module cache and a class singleton. Restoring only
    the environment (or just one cache) leaves relationship applicability using
    the previous test's schema, depending on xdist's worker assignment.
    """
    loader = config_loader._loader
    instance = config_loader.ConfigLoader._instance
    try:
        yield
    finally:
        config_loader._loader = loader
        config_loader.ConfigLoader._instance = instance
