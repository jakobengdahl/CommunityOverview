"""
Visual regression tests using Playwright screenshots
Tests visualization rendering and node editing UI
"""

import pytest
from playwright.sync_api import Page, expect

BASE_URL = "http://localhost:5173"

@pytest.fixture(scope="session")
def check_server_running(page: Page):
    """Check if frontend server is running"""
    try:
        page.goto(BASE_URL, timeout=2000)
        return True
    except Exception:
        pytest.skip(f"Frontend server not running on {BASE_URL}")
        return False

def test_empty_graph_screenshot(page: Page, check_server_running):
    """Screenshot test for empty graph state"""
    page.goto(BASE_URL)

    # Wait for page to load
    page.wait_for_selector(".visualization-panel", timeout=5000)

    # Should show empty state message
    expect(page.locator(".empty-graph-message")).to_be_visible()

    # Take screenshot
    page.screenshot(path="verification/screenshots/empty_graph.png")

def test_community_selection_ui(page: Page, check_server_running):
    """Screenshot test for community selection dropdown"""
    page.goto(BASE_URL)

    # Click community dropdown
    page.click(".community-dropdown-toggle")

    # Wait for dropdown to open
    page.wait_for_selector(".community-dropdown-menu", state="visible")

    # Take screenshot of dropdown
    page.screenshot(path="verification/screenshots/community_dropdown.png")

    # Verify communities are listed
    expect(page.locator(".community-option")).to_have_count(3)  # eSam, Myndigheter, Officiell Statistik

def test_settings_dialog_screenshot(page: Page, check_server_running):
    """Screenshot test for API key settings dialog"""
    page.goto(BASE_URL)

    # Click settings button
    page.click(".settings-button")

    # Wait for settings dialog
    page.wait_for_selector(".settings-dialog", state="visible")

    # Take screenshot
    page.screenshot(path="verification/screenshots/settings_dialog.png")

    # Verify API key input is visible
    expect(page.locator("#api-key-input")).to_be_visible()

def test_node_editing_dialog_visual(page: Page, check_server_running):
    """
    Screenshot test for node editing dialog
    Note: This test requires a graph with nodes to be loaded first
    """
    page.goto(BASE_URL)

    # Select a community first
    page.click(".community-dropdown-toggle")
    page.wait_for_selector(".community-dropdown-menu", state="visible")
    page.click("text=eSam")

    # Wait a bit for any initial load
    page.wait_for_timeout(1000)

    # Note: Since we need actual nodes for this test, we would need to:
    # 1. Either mock the data
    # 2. Or have a test setup that loads sample data
    # For now, we'll check if the dialog component exists in the DOM
    # and can be triggered programmatically in a future enhancement

    # Check that CustomNode component is registered
    # (This would be a more complete test with actual data)
    print("Node editing dialog test requires sample data - skipping visual test")

def test_stats_panel_visual(page: Page, check_server_running):
    """Screenshot test for statistics panel"""
    page.goto(BASE_URL)

    # Select community
    page.click(".community-dropdown-toggle")
    page.wait_for_selector(".community-dropdown-menu", state="visible")
    page.click("text=eSam")

    page.wait_for_timeout(500)

    # Take screenshot of full page
    page.screenshot(path="verification/screenshots/with_community_selected.png", full_page=True)

def test_text_extraction_panel_visual(page: Page, check_server_running):
    """Screenshot test for text extraction panel"""
    page.goto(BASE_URL)

    # Click "Extract from Text" button
    page.click("text=Extract from Text")

    # Wait for panel to open
    page.wait_for_selector(".text-extract-panel", state="visible")

    # Take screenshot
    page.screenshot(path="verification/screenshots/text_extraction_panel.png")

    # Verify panel elements
    expect(page.locator(".extract-textarea")).to_be_visible()
    expect(page.locator("text=Extract Nodes")).to_be_visible()

def test_responsive_layout(page: Page, check_server_running):
    """Test responsive behavior at different viewport sizes"""
    viewports = [
        {"width": 1920, "height": 1080, "name": "desktop"},
        {"width": 1024, "height": 768, "name": "tablet"},
        {"width": 375, "height": 667, "name": "mobile"}
    ]

    for viewport in viewports:
        page.set_viewport_size({"width": viewport["width"], "height": viewport["height"]})
        page.goto(BASE_URL)

        # Wait for page load
        page.wait_for_selector(".app", timeout=5000)

        # Take screenshot
        page.screenshot(path=f"verification/screenshots/layout_{viewport['name']}.png")

def test_graph_visualization_with_nodes_visual(page: Page, check_server_running):
    """
    Test visual appearance of graph with nodes loaded
    This would be a full integration test requiring actual graph data
    """
    # TODO: This test requires either:
    # 1. A way to load test data into the graph
    # 2. Mock API responses
    # 3. Pre-seeded database/backend

    # For now, we document the expected behavior:
    # - Nodes should be color-coded by type
    # - Layout should use dagre hierarchical algorithm
    # - Edges should connect related nodes
    # - Hovering shows node details tooltip
    # - Edit icon appears on hover

    print("Graph visualization visual test requires test data - documented for future implementation")

def test_lazy_loading_indicator_visual(page: Page, check_server_running):
    """
    Test lazy loading UI when many nodes are present
    Requires loading a large dataset (500+ nodes)
    """
    # This would test the lazy loading UI that shows
    # "Showing X of Y nodes" with "Load More" button
    print("Lazy loading visual test requires large dataset - documented for future implementation")
