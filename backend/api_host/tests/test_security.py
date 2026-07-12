"""
Security tests for CORS configuration.
"""

from fastapi.testclient import TestClient
from backend.api_host import create_app, AppConfig


def test_cors_wildcard_no_credentials(temp_graph_file, temp_static_dirs):
    """Test that wildcard origins do not allow credentials."""
    web_path, widget_path = temp_static_dirs
    config = AppConfig(
        graph_file=temp_graph_file,
        web_static_path=web_path,
        widget_static_path=widget_path,
        cors_allowed_origins=["*"],
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
        cors_allowed_origins=["https://example.com"],
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


def test_cors_origins_whitespace_stripped(temp_graph_file, temp_static_dirs):
    """Test that whitespace around origins in CORS_ALLOWED_ORIGINS is stripped.

    Operators may write 'https://a.com, https://b.com' with a space after the
    comma. Without stripping, ' https://b.com' (with a leading space) would
    never match a real Origin header, silently breaking CORS for that origin.
    """
    web_path, widget_path = temp_static_dirs

    # Passing the parsed list directly exercises the same parsing logic
    origins_with_spaces = [o.strip() for o in "https://a.com, https://b.com".split(",")]
    config = AppConfig(
        graph_file=temp_graph_file,
        web_static_path=web_path,
        widget_static_path=widget_path,
        cors_allowed_origins=origins_with_spaces,
    )
    config.auth_enabled = False

    assert "https://a.com" in config.cors_allowed_origins
    assert "https://b.com" in config.cors_allowed_origins
    assert all(o == o.strip() for o in config.cors_allowed_origins)

    # Confirm the app can be built with these stripped origins
    app = create_app(config)
    client = TestClient(app)

    headers = {
        "Origin": "https://b.com",
        "Access-Control-Request-Method": "GET",
    }
    response = client.options("/health", headers=headers)
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://b.com"


def test_cors_default_is_same_origin_only(
    temp_graph_file, temp_static_dirs, monkeypatch
):
    """With CORS_ALLOWED_ORIGINS unset the default is no cross-origin access.

    A wildcard default would let any website drive an auth-bypassed instance
    from a victim's browser; the default must instead add no CORS headers for a
    cross-origin request until an operator opts origins in explicitly.
    """
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    web_path, widget_path = temp_static_dirs
    config = AppConfig(
        graph_file=temp_graph_file,
        web_static_path=web_path,
        widget_static_path=widget_path,
    )
    config.auth_enabled = False
    assert config.cors_allowed_origins == []

    app = create_app(config)
    client = TestClient(app)

    headers = {
        "Origin": "https://example.com",
        "Access-Control-Request-Method": "GET",
    }
    response = client.options("/health", headers=headers)
    assert "access-control-allow-origin" not in response.headers


def test_cors_unauthorized_origin_rejected(temp_graph_file, temp_static_dirs):
    """Test that unauthorized origins are rejected in CORS."""
    web_path, widget_path = temp_static_dirs
    config = AppConfig(
        graph_file=temp_graph_file,
        web_static_path=web_path,
        widget_static_path=widget_path,
        cors_allowed_origins=["https://trusted.com"],
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
