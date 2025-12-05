
import pytest
from playwright.sync_api import Page, expect

# Basic smoke test
def test_home_page_title(page: Page):
    # This test expects the frontend to be running.
    # We might need to handle this in CI or local dev by starting the server.
    # For now, we assume it's running on localhost:5173 (Vite default) or we can parametrize it.

    # Skip if server is not reachable?
    # Or we can just fail.

    try:
        page.goto("http://localhost:5173")
    except Exception:
        pytest.skip("Frontend server not running on http://localhost:5173")

    # Check title (Vite default is usually "Vite + React" or similar,
    # but let's check what's in index.html)
    expect(page).to_have_title("Community Knowledge Graph")

def test_chat_panel_exists(page: Page):
    try:
        page.goto("http://localhost:5173")
    except Exception:
        pytest.skip("Frontend server not running")

    # Check if chat panel exists
    expect(page.locator(".chat-panel")).to_be_visible()

    # Check input
    expect(page.locator("textarea.chat-input")).to_be_visible()

def test_graph_exists(page: Page):
    try:
        page.goto("http://localhost:5173")
    except Exception:
        pytest.skip("Frontend server not running")

    # Check if react flow is there
    expect(page.locator(".react-flow")).to_be_visible()
