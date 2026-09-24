import { expect } from '@playwright/test';

/**
 * Shared setup for the desktop specs.
 *
 * The backend Playwright starts writes to its own e2e graph file, which begins
 * empty, so a spec that needs something to find has to put it there itself.
 * Names carry a random token: the file persists between runs and specs run in
 * parallel, so a fixed name would match leftovers from another test.
 */

export function uniqueToken() {
  return `e2e${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
}

/** Creates a node through the REST API and returns it as the backend stored it. */
export async function seedNode(request, { name, type = 'Actor', description = 'e2e seed node' }) {
  const response = await request.post('/api/nodes', {
    data: { nodes: [{ name, type, description }], edges: [] },
  });
  expect(response.ok(), `seeding ${name} failed: ${response.status()}`).toBe(true);
  const body = await response.json();
  expect(body.added_node_ids).toHaveLength(1);
  return { id: body.added_node_ids[0], name, type };
}

/** Canvas nodes whose visible text contains `name`. */
export function canvasNode(page, name) {
  return page.locator('.react-flow__node', { hasText: name });
}

/**
 * Types into the floating search, waits for `name` to be offered and picks it
 * with Enter. The search bar has no submit button: typing (debounced, two or
 * more characters) opens the dropdown and Enter takes the highlighted result.
 */
export async function addNodeViaSearch(page, query, name) {
  const input = page.locator('.floating-search-input');
  await input.fill(query);
  // Enter takes the highlighted result, so the node has to be the one highlighted.
  const highlighted = page.locator('.floating-search-result.selected .floating-search-result-name');
  await expect(highlighted).toHaveText(name, { timeout: 15000 });
  await input.press('Enter');
  await expect(canvasNode(page, name)).toBeVisible({ timeout: 15000 });
}
