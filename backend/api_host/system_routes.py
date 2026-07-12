"""System and operability endpoints for the api_host application.

Favicon/redirect helpers, liveness/readiness/startup diagnostics, the root
redirect, logout handling, and the API info endpoint.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse, HTMLResponse
from fastapi import Path as FastAPIPath

from backend.llm.llm_providers import get_llm_availability

from .config import AppConfig
from .diagnostics import PUBLIC_READINESS_PATH, PUBLIC_STARTUP_DIAGNOSTICS_PATH

logger = logging.getLogger(__name__)


# Standalone logged-out page HTML (shared between /logged-out and the
# 401 response in Basic Auth mode).
LOGGED_OUT_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Logged out</title>
  <link rel="icon" href="/favicon.svg" />
  <style>
    :root { color-scheme: dark; }
    html, body {
      margin: 0;
      padding: 0;
      height: 100%;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        Helvetica, Arial, sans-serif;
      background: #121212;
      color: #eaeaea;
    }
    body {
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .logged-out-card {
      background: rgba(26, 26, 26, 0.95);
      border: 1px solid #333;
      border-radius: 12px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
      padding: 32px 40px;
      max-width: 420px;
      text-align: center;
    }
    .logged-out-card h1 {
      margin: 0 0 12px 0;
      font-size: 1.4rem;
      font-weight: 600;
      color: #fff;
    }
    .logged-out-card p {
      margin: 0 0 20px 0;
      color: #bbb;
      font-size: 0.95rem;
      line-height: 1.5;
    }
    .logged-out-card a {
      display: inline-block;
      padding: 10px 18px;
      background: #646cff;
      color: #fff;
      text-decoration: none;
      border-radius: 8px;
      font-size: 0.9rem;
      font-weight: 500;
      transition: background 0.15s;
    }
    .logged-out-card a:hover {
      background: #535bf2;
    }
  </style>
</head>
<body>
  <div class="logged-out-card">
    <h1>You have been logged out</h1>
    <p>Your session has ended. You can return to the application using the link below.</p>
    <a href="/">Back to start</a>
  </div>
</body>
</html>
"""


def register_system_routes(
    app: FastAPI,
    config: AppConfig,
    graph_storage,
    chat_service,
    federation_summary: Dict[str, Any],
    federation_manager,
) -> None:
    """Register favicon/redirect, health/readiness, root, logout and info routes."""

    # Serve favicon/graph-icon from web static path to prevent 404 noise
    _favicon_path = Path(config.web_static_path) / "graph-icon.svg"

    @app.get("/graph-icon.svg")
    @app.get("/favicon.svg")
    @app.get("/favicon.ico")
    @app.get("/favicon.png")
    async def favicon():
        if _favicon_path.exists():
            return FileResponse(str(_favicon_path), media_type="image/svg+xml")
        return JSONResponse(status_code=204, content=None)

    @app.get("/collect/{short_name}")
    @app.get("/collect/{short_name}/")
    async def collect_redirect(
        short_name: str = FastAPIPath(
            ..., pattern=r"^[a-z0-9][a-z0-9-]{0,98}[a-z0-9]$|^[a-z0-9]$"
        ),
    ) -> RedirectResponse:
        """Redirect collect kiosk URL to the web app in collect mode."""
        from urllib.parse import quote

        return RedirectResponse(
            url=f"/web/?collect={quote(short_name, safe='')}", status_code=302
        )

    @app.get("/collect")
    async def collect_root_redirect() -> RedirectResponse:
        """Redirect bare collect URL to web app."""
        return RedirectResponse(url="/web/", status_code=302)

    @app.get("/health")
    async def health_check() -> Dict[str, Any]:
        """Liveness endpoint that confirms the host process is serving requests."""
        return {
            "status": "healthy",
            "kind": "liveness",
            "graph_nodes": len(graph_storage.nodes),
            "graph_edges": len(graph_storage.edges),
            "readiness_endpoint": PUBLIC_READINESS_PATH,
        }

    @app.get(PUBLIC_READINESS_PATH)
    async def readiness_check() -> Dict[str, Any]:
        """Readiness endpoint with safe startup and dependency diagnostics."""
        return {
            "status": app.state.startup_diagnostics["status"],
            "kind": "readiness",
            "checks": app.state.startup_diagnostics["checks"],
            "warnings": app.state.startup_diagnostics["warnings"],
            "startup_diagnostics_endpoint": PUBLIC_STARTUP_DIAGNOSTICS_PATH,
        }

    @app.get(PUBLIC_STARTUP_DIAGNOSTICS_PATH)
    async def startup_diagnostics() -> Dict[str, Any]:
        """Structured startup diagnostics safe for public operability introspection."""
        return app.state.startup_diagnostics

    @app.get("/")
    async def root() -> RedirectResponse:
        """Redirect root to web application."""
        return RedirectResponse(url="/web/", status_code=302)

    # Logout endpoint - cloud-agnostic.
    # If LOGOUT_REDIRECT_URL is set, redirect there (e.g. an IAP / OAuth
    # proxy sign-out URL). Otherwise, choose a sensible default:
    #   - mcp_basic_auth mode (IAP): clear IAP cookie via GCP endpoint
    #   - auth_enabled mode (Basic Auth): return 401 to clear browser cache
    #   - no auth: simple redirect to local logged-out page
    # This endpoint is exempt from auth middleware so it never loops.
    logout_redirect_url_env = os.environ.get("LOGOUT_REDIRECT_URL")

    @app.get("/auth/logout")
    async def logout():
        """Log the user out, clearing auth state appropriately."""
        if logout_redirect_url_env:
            return RedirectResponse(url=logout_redirect_url_env, status_code=302)

        if config.mcp_basic_auth and not config.auth_enabled:
            # Behind GCP IAP – the only way to clear the IAP session cookie
            # is via the GCP-provided endpoint.
            return RedirectResponse(url="/_gcp_iap/clear_login_cookie", status_code=302)

        if config.auth_enabled:
            # Basic Auth – the browser caches credentials and resends them
            # automatically. Returning 401 forces the browser to drop its
            # cached credentials. The response body is the logged-out page
            # so the user sees it after dismissing the browser auth dialog
            # (or immediately in programmatic clients).
            return HTMLResponse(
                content=LOGGED_OUT_HTML,
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Logged out"'},
            )

        return RedirectResponse(url="/logged-out", status_code=302)

    # Fallback logged-out page, used when no external auth layer is present.
    # Must not require auth — this page is where users land after logout.
    @app.get("/logged-out")
    async def logged_out():
        """Simple standalone page shown after logout."""
        return HTMLResponse(content=LOGGED_OUT_HTML)

    @app.get("/info")
    async def info() -> Dict[str, Any]:
        """API information endpoint."""
        llm = get_llm_availability()
        return {
            "name": "Community Knowledge Graph",
            "version": "1.0.0",
            "config_profile": config.config_profile,
            "endpoints": {
                "api": config.api_prefix,
                "ui": "/ui",
                "mcp": "/mcp",
                "web": "/web",
                "widget": "/widget",
                "health": "/health",
                "ready": PUBLIC_READINESS_PATH,
                "startup_diagnostics": PUBLIC_STARTUP_DIAGNOSTICS_PATH,
            },
            "graph_stats": {
                "nodes": len(graph_storage.nodes),
                "edges": len(graph_storage.edges),
            },
            "llm_provider": chat_service.provider_type,
            "llm_available": llm["available"],
            "operability": {
                "startup_status": app.state.startup_diagnostics["status"],
                "warnings": app.state.startup_diagnostics["warnings"],
                "capabilities": app.state.startup_diagnostics["capabilities"],
                "config_context": app.state.startup_diagnostics["config_context"],
            },
            "federation": {
                **federation_summary,
                "runtime": federation_manager.get_status(),
            },
        }
