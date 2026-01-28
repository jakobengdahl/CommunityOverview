import { test, expect } from '@playwright/test';

test.describe('Community Graph Web App', () => {
  test('loads the main page', async ({ page }) => {
    await page.goto('/');

    // Should display the app title
    await expect(page.locator('h1')).toContainText('Community Graph');
  });

  test('displays search panel', async ({ page }) => {
    await page.goto('/');

    // Search input should be visible
    const searchInput = page.locator('input[placeholder*="Search"]');
    await expect(searchInput).toBeVisible();

    // Search button should be visible
    const searchButton = page.locator('button:has-text("Search")');
    await expect(searchButton).toBeVisible();
  });

  test('displays stats panel', async ({ page }) => {
    await page.goto('/');

    // Stats panel should be visible with node/edge counts
    await expect(page.locator('text=Nodes')).toBeVisible();
    await expect(page.locator('text=Edges')).toBeVisible();
  });

  test('can perform a search', async ({ page }) => {
    await page.goto('/');

    // Type in search box
    const searchInput = page.locator('input[placeholder*="Search"]');
    await searchInput.fill('test');

    // Click search button
    const searchButton = page.locator('button:has-text("Search")');
    await searchButton.click();

    // Wait for network request or loading state to complete
    // Either we get results or "no results" message
    await page.waitForTimeout(1000);

    // Graph canvas should be present
    const canvas = page.locator('.react-flow');
    await expect(canvas).toBeVisible();
  });

  test('graph canvas renders', async ({ page }) => {
    await page.goto('/');

    // ReactFlow container should be rendered
    const reactFlow = page.locator('.react-flow');
    await expect(reactFlow).toBeVisible();
  });

  test('displays type filters', async ({ page }) => {
    await page.goto('/');

    // Type filter checkboxes should exist
    // Looking for common node types
    const typeFilters = page.locator('.type-filters');
    await expect(typeFilters).toBeVisible();
  });

  test('search input accepts keyboard input', async ({ page }) => {
    await page.goto('/');

    const searchInput = page.locator('input[placeholder*="Search"]');
    await searchInput.fill('community');

    await expect(searchInput).toHaveValue('community');
  });

  test('can press Enter to search', async ({ page }) => {
    await page.goto('/');

    const searchInput = page.locator('input[placeholder*="Search"]');
    await searchInput.fill('actor');
    await searchInput.press('Enter');

    // Should trigger search - verify no crash
    await page.waitForTimeout(500);
    await expect(page.locator('.react-flow')).toBeVisible();
  });
});
