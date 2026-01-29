"""
Tests for static file serving in app_host.

Tests that static files for web app and widget are properly served.
"""

import pytest
import tempfile
from pathlib import Path
from fastapi.testclient import TestClient

from backend.api_host import create_app, AppConfig


class TestStaticFileServing:
    """Tests for static file serving functionality."""

    def test_web_index_served(self, test_app: TestClient):
        """Web app index.html is served at /web/."""
        response = test_app.get("/web/")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")

    def test_web_index_html_served(self, test_app: TestClient):
        """Web app index.html is served at /web/index.html."""
        response = test_app.get("/web/index.html")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        assert "Web App" in response.text

    def test_widget_index_served(self, test_app: TestClient):
        """Widget index.html is served at /widget/."""
        response = test_app.get("/widget/")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")

    def test_widget_index_html_served(self, test_app: TestClient):
        """Widget index.html is served at /widget/index.html."""
        response = test_app.get("/widget/index.html")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        assert "Widget" in response.text

    def test_nonexistent_static_file_returns_404(self, test_app: TestClient):
        """Non-existent static file returns 404."""
        response = test_app.get("/web/nonexistent.js")
        assert response.status_code == 404

    def test_nonexistent_widget_file_returns_404(self, test_app: TestClient):
        """Non-existent widget file returns 404."""
        response = test_app.get("/widget/nonexistent.css")
        assert response.status_code == 404


class TestStaticFilesWithAdditionalContent:
    """Tests static file serving with additional content files."""

    @pytest.fixture
    def app_with_extra_files(self) -> TestClient:
        """Create app with additional static files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create web directory with multiple files
            web_dir = Path(temp_dir) / "web"
            web_dir.mkdir()
            (web_dir / "index.html").write_text(
                "<!DOCTYPE html><html><body>Main App</body></html>"
            )
            (web_dir / "app.js").write_text(
                "console.log('App loaded');"
            )
            (web_dir / "styles.css").write_text(
                "body { margin: 0; }"
            )

            # Create subdirectory
            assets_dir = web_dir / "assets"
            assets_dir.mkdir()
            (assets_dir / "logo.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"></svg>'
            )

            # Create widget directory
            widget_dir = Path(temp_dir) / "widget"
            widget_dir.mkdir()
            (widget_dir / "index.html").write_text(
                "<!DOCTYPE html><html><body>Widget</body></html>"
            )
            (widget_dir / "widget.js").write_text(
                "console.log('Widget loaded');"
            )

            # Create empty graph file
            graph_file = Path(temp_dir) / "graph.json"
            graph_file.write_text('{"nodes": [], "edges": []}')

            config = AppConfig(
                graph_file=str(graph_file),
                web_static_path=str(web_dir),
                widget_static_path=str(widget_dir),
            )

            app = create_app(config)
            yield TestClient(app)

    def test_serve_javascript_file(self, app_with_extra_files: TestClient):
        """JavaScript files are served with correct content type."""
        response = app_with_extra_files.get("/web/app.js")
        assert response.status_code == 200
        assert "javascript" in response.headers.get("content-type", "")
        assert "console.log" in response.text

    def test_serve_css_file(self, app_with_extra_files: TestClient):
        """CSS files are served with correct content type."""
        response = app_with_extra_files.get("/web/styles.css")
        assert response.status_code == 200
        assert "css" in response.headers.get("content-type", "")
        assert "margin" in response.text

    def test_serve_file_in_subdirectory(self, app_with_extra_files: TestClient):
        """Files in subdirectories are served correctly."""
        response = app_with_extra_files.get("/web/assets/logo.svg")
        assert response.status_code == 200
        assert "svg" in response.headers.get("content-type", "")

    def test_serve_widget_javascript(self, app_with_extra_files: TestClient):
        """Widget JavaScript files are served."""
        response = app_with_extra_files.get("/widget/widget.js")
        assert response.status_code == 200
        assert "Widget loaded" in response.text


class TestStaticFilesNotBuilt:
    """Tests behavior when static files are not built."""

    @pytest.fixture
    def app_without_static_dirs(self) -> TestClient:
        """Create app without static directories."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Only create graph file, no static directories
            graph_file = Path(temp_dir) / "graph.json"
            graph_file.write_text('{"nodes": [], "edges": []}')

            # Point to non-existent directories
            config = AppConfig(
                graph_file=str(graph_file),
                web_static_path=str(Path(temp_dir) / "nonexistent_web"),
                widget_static_path=str(Path(temp_dir) / "nonexistent_widget"),
            )

            app = create_app(config)
            yield TestClient(app)

    def test_web_returns_error_when_not_built(
        self, app_without_static_dirs: TestClient
    ):
        """Web requests return helpful error when not built."""
        response = app_without_static_dirs.get("/web/index.html")
        assert response.status_code == 404
        data = response.json()
        assert "error" in data
        assert "not built" in data["error"].lower()

    def test_widget_returns_error_when_not_built(
        self, app_without_static_dirs: TestClient
    ):
        """Widget requests return helpful error when not built."""
        response = app_without_static_dirs.get("/widget/index.html")
        assert response.status_code == 404
        data = response.json()
        assert "error" in data
        assert "not built" in data["error"].lower()


class TestStaticFilesWithRealPaths:
    """Tests using the actual static file paths in apps directory."""

    def test_real_web_index_exists(self):
        """Verify real web index.html exists for actual deployment."""
        web_index = Path(__file__).parent.parent.parent / "apps" / "web" / "dist" / "index.html"
        assert web_index.exists(), f"Web index not found at {web_index}"

    def test_real_widget_index_exists(self):
        """Verify real widget index.html exists for actual deployment."""
        widget_index = Path(__file__).parent.parent.parent / "apps" / "widget" / "dist" / "index.html"
        assert widget_index.exists(), f"Widget index not found at {widget_index}"

    def test_real_static_files_served(self):
        """Test that real static files are served correctly."""
        # Use actual paths
        base_dir = Path(__file__).parent.parent.parent

        # Create a temporary graph file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write('{"nodes": [], "edges": []}')
            graph_path = f.name

        try:
            config = AppConfig(
                graph_file=graph_path,
                web_static_path=str(base_dir / "apps" / "web" / "dist"),
                widget_static_path=str(base_dir / "apps" / "widget" / "dist"),
            )

            app = create_app(config)
            client = TestClient(app)

            # Test web app
            response = client.get("/web/index.html")
            assert response.status_code == 200
            assert "Community Knowledge Graph" in response.text

            # Test widget
            response = client.get("/widget/index.html")
            assert response.status_code == 200
            assert "Graph Widget" in response.text

        finally:
            import os
            os.unlink(graph_path)
