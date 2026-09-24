import { test, expect } from '@playwright/test';
import { addNodeViaSearch, canvasNode, seedNode, uniqueToken } from './helpers';

/**
 * Desktop smoke tests: the floating-chrome shell on a mouse pointer.
 *
 * Runs under the `chromium` project only. The phone layout has its own spec
 * (mobile-smoke.spec.js); the first test here pins that this one really is the
 * desktop branch, so a project that started emulating touch would fail loudly
 * rather than quietly test the mobile shell twice.
 */

async function openApp(page) {
  await page.goto('/');
  await expect(page.locator('.react-flow')).toBeVisible();
}

test.describe('desktop shell', () => {
  test('renders the canvas under the floating header with a session id', async ({ page }) => {
    await openApp(page);

    await expect(page.locator('.floating-header-title')).toHaveText('Community Graph View');
    await expect(page.locator('.floating-header-session-id')).toHaveText(
      /^\d{4}-\d{4}(-\d{4}-\d{4})?$/
    );
    await expect(page.locator('.floating-search-input')).toBeVisible();
    await expect(page.locator('.app.is-touch')).toHaveCount(0);
    await expect(page.locator('.mobile-shell-bottomnav')).toHaveCount(0);
  });

  test('search offers a matching node and Enter adds it to the canvas', async ({
    page,
    request,
  }) => {
    const token = uniqueToken();
    const node = await seedNode(request, { name: `Smoke search ${token}` });
    await openApp(page);
    await expect(page.locator('.react-flow__node')).toHaveCount(0);

    await addNodeViaSearch(page, token, node.name);

    await expect(page.locator('.react-flow__node')).toHaveCount(1);
    await expect(canvasNode(page, node.name)).toHaveAttribute('data-id', node.id);
    // Picking a result resets the search so the next query starts clean.
    await expect(page.locator('.floating-search-input')).toHaveValue('');
    await expect(page.locator('.floating-search-dropdown')).toHaveCount(0);
  });

  test('a query with no match offers nothing and Enter adds nothing', async ({ page }) => {
    await openApp(page);
    const input = page.locator('.floating-search-input');

    const query = `nomatch${uniqueToken()}`;
    const searched = page.waitForResponse(
      (response) =>
        response.url().endsWith('/api/search') &&
        response.ok() &&
        response.request().postDataJSON()?.query === query
    );
    await input.fill(query);
    const body = await (await searched).json();
    expect(body.nodes).toEqual([]);

    // Set together with the results, so the absence checks below run against
    // the rendered outcome for this query, not before it.
    await expect(page.locator('.floating-search')).toHaveAttribute('data-results-query', query);
    await expect(input).toHaveValue(query);
    await expect(page.locator('.floating-search-dropdown')).toHaveCount(0);
    await input.press('Enter');
    await expect(page.locator('.react-flow__node')).toHaveCount(0);
  });

  test('a toolbar type button creates and stores a node of that type', async ({
    page,
    request,
  }) => {
    const name = `Toolbar actor ${uniqueToken()}`;
    await openApp(page);

    await page.locator('.floating-toolbar-item[aria-label="Actor"]').click();
    const dialog = page.locator('.create-node-dialog');
    await expect(dialog).toBeVisible();
    await dialog.locator('#create-name').fill(name);
    await dialog.locator('button[type="submit"]').click();
    await expect(dialog).toBeHidden();

    await expect(canvasNode(page, name)).toBeVisible();
    const found = await request.post('/api/search', { data: { query: name, limit: 5 } });
    const stored = (await found.json()).nodes.filter((n) => n.name === name);
    expect(stored).toHaveLength(1);
    expect(stored[0].type).toBe('Actor');
  });

  test('the Settings dialog reports the totals the backend returned', async ({ page, request }) => {
    // At least one node, so a dialog stuck at its `|| 0` fallback cannot pass.
    await seedNode(request, { name: `Stats seed ${uniqueToken()}` });

    // Other tests seed nodes in parallel, so compare against the latest stats
    // response this page received rather than a separate read that could race.
    let latestStats = null;
    page.on('response', async (response) => {
      if (response.url().endsWith('/api/stats') && response.ok()) {
        latestStats = await response.json().catch(() => latestStats);
      }
    });
    await openApp(page);

    await page.locator('.floating-header-hamburger').click();
    await page.locator('.session-drawer-item', { hasText: 'Settings' }).click();
    const dialog = page.locator('.settings-dialog');
    await expect(dialog).toBeVisible();

    const values = dialog.locator('.settings-dialog-stat-value');
    await expect(values).toHaveCount(2);
    await expect(dialog.locator('.settings-dialog-stat-label')).toHaveText(['Nodes', 'Edges']);
    const reported = () =>
      latestStats ? [String(latestStats.total_nodes), String(latestStats.total_edges)] : null;
    await expect
      .poll(async () => {
        const expected = reported();
        return expected !== null && (await values.allTextContents()).join() === expected.join();
      })
      .toBe(true);
    expect(Number(await values.first().textContent())).toBeGreaterThan(0);
  });
});
