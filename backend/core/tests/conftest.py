"""
Pytest configuration for graph_core tests
"""

import pytest


def pytest_configure(config):
    """Configure custom markers"""
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow (require model loading, deselect with '-m \"not slow\"')"
    )


def pytest_collection_modifyitems(config, items):
    """Skip slow tests by default unless explicitly requested"""
    if config.getoption("-m"):
        # If markers are explicitly specified, don't modify
        return

    skip_slow = pytest.mark.skip(reason="slow test - use '-m slow' to run")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)
