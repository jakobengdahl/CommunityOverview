"""Real health probes backing hc-02, hc-03, hc-07, and hc-13.

Each function returns a small JSON-serializable result dict with a "status"
key ("ok", "degraded", or "skipped") and never raises — failures are caught
and reported as a degraded result so the calling route can turn them into an
HTTP status code without a try/except of its own.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def check_deep(**startup_diagnostics_kwargs: Any) -> Dict[str, Any]:
    """hc-03: live end-to-end smoke check (config, graph storage, LLM key).

    Re-runs the same checks build_startup_diagnostics performs at startup,
    but live per-request rather than relying on the value cached in
    app.state.startup_diagnostics at process start.
    """
    from backend.api_host.diagnostics import build_startup_diagnostics

    try:
        diagnostics = build_startup_diagnostics(**startup_diagnostics_kwargs)
    except Exception as exc:  # pragma: no cover - defensive, probes must not raise
        logger.exception("hc-03 deep probe raised")
        return {"status": "degraded", "detail": str(exc)}

    if diagnostics["status"] != "ready":
        return {"status": "degraded", "detail": diagnostics["checks"]}
    return {"status": "ok", "detail": diagnostics["checks"]}


def check_storage(graph_storage: Any) -> Dict[str, Any]:
    """hc-07: real write+read+delete round-trip against the persistence backend."""
    backend = getattr(graph_storage, "_persistence_backend", None)
    json_path = getattr(backend, "json_path", None)
    if backend is None or json_path is None:
        return {
            "status": "degraded",
            "detail": "no file-backed persistence backend configured",
        }

    probe_path = Path(json_path).parent / f".health-probe-{uuid.uuid4().hex}"
    probe_value = uuid.uuid4().hex

    try:
        probe_path.parent.mkdir(parents=True, exist_ok=True)
        probe_path.write_text(probe_value, encoding="utf-8")
        read_back = probe_path.read_text(encoding="utf-8")
        if read_back != probe_value:
            return {
                "status": "degraded",
                "detail": "storage round-trip content mismatch",
            }
        return {"status": "ok", "detail": None}
    except Exception as exc:
        logger.exception("hc-07 storage probe failed")
        return {"status": "degraded", "detail": str(exc)}
    finally:
        try:
            probe_path.unlink(missing_ok=True)
        except OSError:
            pass


def check_secret_store(secret_id: Optional[str]) -> Dict[str, Any]:
    """hc-13: confirms Secret Manager is reachable for one known secret.

    Uses GetSecret (metadata only), not AccessSecretVersion — one of the two
    baseline secrets (prod's cw-saas-db-password) intentionally has zero
    versions today, so an access-version call would report unhealthy even
    when Secret Manager itself is fine.
    """
    if not secret_id:
        return {
            "status": "skipped",
            "detail": "SECRET_STORE_HEALTH_CHECK_SECRET_ID not configured",
        }

    try:
        import google.auth
        from google.cloud import secretmanager

        _, project_id = google.auth.default()
        client = secretmanager.SecretManagerServiceClient()
        client.get_secret(name=f"projects/{project_id}/secrets/{secret_id}")
        return {"status": "ok", "detail": None}
    except Exception as exc:
        logger.exception("hc-13 secret store probe failed")
        return {"status": "degraded", "detail": str(exc)}
