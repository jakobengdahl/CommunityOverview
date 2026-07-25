"""
Pytest configuration for event system tests.
"""


def pytest_configure(config):
    """Configure custom markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (network/timing dependent)"
    )
