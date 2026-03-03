"""
Security tests for CORS configuration.
"""

import pytest
from fastapi.testclient import TestClient
from backend.api_host import create_app, AppConfig

def test_cors_wildcard_no_credentials(temp_graph_file, temp_static_dirs):
    """Test that wildcard origins do not allow credentials."""
    web_path, widget_path = temp_static_dirs
    config = AppConfig(
        graph_file=temp_graph_file,
        web_static_path=web_path,
        widget_static_path=widget_path,
        cors_allowed_origins=["*"]
    )
    # Ensure no auth required for health to simplify test
    config.auth_enabled = False

    app = create_app(config)
    client = TestClient(app)

    # Preflight request
    headers = {
        "Origin": "https://example.com",
        "Access-Control-Request-Method": "GET",
    }
    response = client.options("/health", headers=headers)

    # Starlette's CORSMiddleware responds to OPTIONS
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "*"
    # When allow_origins is ["*"], allow_credentials must be False
    assert response.headers.get("access-control-allow-credentials") is None

def test_cors_specific_origin_allows_credentials(temp_graph_file, temp_static_dirs):
    """Test that specific origins allow credentials."""
    web_path, widget_path = temp_static_dirs
    config = AppConfig(
        graph_file=temp_graph_file,
        web_static_path=web_path,
        widget_static_path=widget_path,
        cors_allowed_origins=["https://example.com"]
    )
    config.auth_enabled = False

    app = create_app(config)
    client = TestClient(app)

    # Preflight request
    headers = {
        "Origin": "https://example.com",
        "Access-Control-Request-Method": "GET",
    }
    response = client.options("/health", headers=headers)

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://example.com"
    assert response.headers.get("access-control-allow-credentials") == "true"

def test_cors_unauthorized_origin_rejected(temp_graph_file, temp_static_dirs):
    """Test that unauthorized origins are rejected in CORS."""
    web_path, widget_path = temp_static_dirs
    config = AppConfig(
        graph_file=temp_graph_file,
        web_static_path=web_path,
        widget_static_path=widget_path,
        cors_allowed_origins=["https://trusted.com"]
    )
    config.auth_enabled = False

    app = create_app(config)
    client = TestClient(app)

    # Preflight request from untrusted origin
    headers = {
        "Origin": "https://malicious.com",
        "Access-Control-Request-Method": "GET",
    }
    response = client.options("/health", headers=headers)

    # If origin doesn't match, Starlette CORSMiddleware doesn't add CORS headers
    assert "access-control-allow-origin" not in response.headers
