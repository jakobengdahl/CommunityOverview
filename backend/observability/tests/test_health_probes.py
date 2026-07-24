"""Unit tests for backend.observability.health_probes."""

from unittest.mock import MagicMock, patch

from backend.observability.health_probes import (
    check_deep,
    check_secret_store,
    check_storage,
)


class TestCheckDeep:
    def test_ok_when_diagnostics_ready(self):
        with patch(
            "backend.api_host.diagnostics.build_startup_diagnostics",
            return_value={"status": "ready", "checks": {"config": {"status": "ok"}}},
        ):
            result = check_deep()
        assert result["status"] == "ok"

    def test_degraded_when_diagnostics_not_ready(self):
        with patch(
            "backend.api_host.diagnostics.build_startup_diagnostics",
            return_value={
                "status": "not_ready",
                "checks": {"graph_storage": {"status": "degraded"}},
            },
        ):
            result = check_deep()
        assert result["status"] == "degraded"

    def test_degraded_on_exception(self):
        with patch(
            "backend.api_host.diagnostics.build_startup_diagnostics",
            side_effect=RuntimeError("boom"),
        ):
            result = check_deep()
        assert result["status"] == "degraded"
        assert "boom" not in result["detail"]


class TestCheckStorage:
    def test_ok_round_trip(self, tmp_path):
        backend = MagicMock()
        backend.json_path = tmp_path / "graph.json"
        graph_storage = MagicMock()
        graph_storage._persistence_backend = backend

        result = check_storage(graph_storage)

        assert result["status"] == "ok"
        assert not list(tmp_path.glob(".health-probe-*"))

    def test_degraded_when_no_backend(self):
        graph_storage = MagicMock()
        graph_storage._persistence_backend = None

        result = check_storage(graph_storage)

        assert result["status"] == "degraded"

    def test_degraded_on_write_failure(self, tmp_path):
        backend = MagicMock()
        # Non-existent parent directory that cannot be created (file, not dir)
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        backend.json_path = blocker / "graph.json"
        graph_storage = MagicMock()
        graph_storage._persistence_backend = backend

        result = check_storage(graph_storage)

        assert result["status"] == "degraded"


class TestCheckSecretStore:
    def test_skipped_when_no_secret_id(self):
        result = check_secret_store(None)
        assert result["status"] == "skipped"

    def test_ok_when_secret_reachable(self):
        with patch("google.auth.default", return_value=(MagicMock(), "test-project")):
            with patch(
                "google.cloud.secretmanager.SecretManagerServiceClient"
            ) as client_cls:
                client_cls.return_value.get_secret.return_value = MagicMock()
                result = check_secret_store("cw-saas-session-signing-key")

        assert result["status"] == "ok"

    def test_degraded_when_unreachable(self):
        with patch("google.auth.default", return_value=(MagicMock(), "test-project")):
            with patch(
                "google.cloud.secretmanager.SecretManagerServiceClient"
            ) as client_cls:
                client_cls.return_value.get_secret.side_effect = Exception(
                    "permission denied: projects/test-project/secrets/cw-saas-db-password"
                )
                result = check_secret_store("cw-saas-session-signing-key")

        assert result["status"] == "degraded"
        assert "permission denied" not in result["detail"]
        assert "test-project" not in result["detail"]

    def test_never_calls_access_secret_version(self):
        """hc-13 must only call GetSecret — AccessSecretVersion would fail against
        prod's cw-saas-db-password, which intentionally has zero versions."""
        with patch("google.auth.default", return_value=(MagicMock(), "test-project")):
            with patch(
                "google.cloud.secretmanager.SecretManagerServiceClient"
            ) as client_cls:
                client_cls.return_value.get_secret.return_value = MagicMock()
                check_secret_store("cw-saas-db-password")

                client = client_cls.return_value
                client.get_secret.assert_called_once_with(
                    name="projects/test-project/secrets/cw-saas-db-password"
                )
                client.access_secret_version.assert_not_called()
