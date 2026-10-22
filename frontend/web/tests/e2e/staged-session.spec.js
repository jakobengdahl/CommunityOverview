import { test, expect } from '@playwright/test';
import { addNodeViaSearch, canvasNode, seedNode, uniqueToken } from './helpers';

/**
 * Staged sessions through the real UI (docs/SESSION_OVERLAY_CONTRACT.md §17):
 * create a staged session, edit a node in it, see the edit held out of the
 * graph, review it against a conflicting edit made directly in the graph,
 * resolve and merge it, then stage another edit and discard it.
 *
 * Needs the backend with GRAPH_STAGED_SESSIONS=true (playwright.config.js
 * starts it that way). Not part of any CI job — run with `npm run test:e2e`.
 */

async function readMainNode(request, id) {
  const response = await request.get(`/api/nodes/${encodeURIComponent(id)}`);
  expect(response.ok()).toBe(true);
  return (await response.json()).node;
}

async function editDescription(page, name, description) {
  const node = canvasNode(page, name);
  await node.hover();
  await node.locator('.graph-edit-button').click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByTestId('staged-edit-notice')).toBeVisible();
  await dialog.locator('textarea').first().fill(description);
  await dialog.locator('button[type="submit"]').click();
  await expect(dialog).toBeHidden();
}

test('staged session: edit, review a conflict, merge, discard', async ({ page, request }) => {
  const token = uniqueToken();
  const seeded = await seedNode(request, {
    name: `Staged ${token}`,
    description: 'as the graph had it',
  });

  await page.goto('/');
  await expect(page.locator('.react-flow')).toBeVisible();
  // Direct editing says so where staging exists.
  await expect(page.getByTestId('write-mode-direct')).toBeVisible();

  // Start a staged session.
  await page.locator('.floating-header-hamburger').click();
  await page.locator('.session-drawer-item', { hasText: 'Start new session' }).click();
  await page.getByTestId('new-session-staged').click();
  await expect(page.getByTestId('write-mode-staged')).toBeVisible();
  await expect(page.getByTestId('staged-pending-count')).toHaveText('0 pending change(s)');

  // Edit a node in it: the canvas marks it, the count goes up, the graph keeps
  // what it had.
  await addNodeViaSearch(page, token, seeded.name);
  await editDescription(page, seeded.name, 'as the session wants it');
  await expect(page.getByTestId('staged-pending-count')).toHaveText('1 pending change(s)');
  await expect(canvasNode(page, seeded.name).locator('.graph-custom-node')).toHaveClass(
    /staged-updated/
  );
  expect((await readMainNode(request, seeded.id)).description).toBe('as the graph had it');

  // Someone edits the same field directly in the graph meanwhile.
  const direct = await request.patch(`/api/nodes/${encodeURIComponent(seeded.id)}`, {
    data: { updates: { description: 'as someone else changed it' } },
  });
  expect(direct.ok()).toBe(true);

  // Review: the conflict shows all three values and needs a decision.
  await page.getByText('Review changes').click();
  const review = page.getByTestId('staged-review-dialog');
  await review.getByText('Refresh').click();
  const item = review.getByTestId(`staged-item-node:${seeded.id}`);
  await expect(item).toContainText('Conflict');
  await expect(item).toContainText('as the graph had it');
  await expect(item).toContainText('as the session wants it');
  await expect(item).toContainText('as someone else changed it');
  await item.locator('input[type="checkbox"]').check();
  await review.getByTestId('staged-merge').click();
  await expect(review).toContainText('Choose how to resolve 1 change(s) first');

  await review.getByTestId(`resolution-node:${seeded.id}`).selectOption('take_session');
  await review.getByTestId('staged-merge').click();
  await expect(review).toContainText('Merged 1 change(s) into the graph');
  await expect(review).toContainText('Nothing is pending');
  expect((await readMainNode(request, seeded.id)).description).toBe('as the session wants it');
  await review.getByText('Close').click();
  await expect(page.getByTestId('staged-pending-count')).toHaveText('0 pending change(s)');

  // Stage another edit, then discard it: the graph does not change.
  await editDescription(page, seeded.name, 'a thought better dropped');
  await expect(page.getByTestId('staged-pending-count')).toHaveText('1 pending change(s)');
  await page.getByText('Review changes').click();
  await review
    .getByTestId(`staged-item-node:${seeded.id}`)
    .locator('input[type="checkbox"]')
    .check();
  await review.getByText('Discard 1').click();
  await page.getByRole('button', { name: 'Discard', exact: true }).click();
  await expect(review).toContainText('Discarded 1 change(s)');
  await review.getByText('Close').click();
  await expect(page.getByTestId('staged-pending-count')).toHaveText('0 pending change(s)');
  expect((await readMainNode(request, seeded.id)).description).toBe('as the session wants it');
  await expect(canvasNode(page, seeded.name)).toContainText(seeded.name);
});

async function startStagedSession(page) {
  await page.goto('/');
  await expect(page.locator('.react-flow')).toBeVisible();
  await page.locator('.floating-header-hamburger').click();
  await page.locator('.session-drawer-item', { hasText: 'Start new session' }).click();
  await page.getByTestId('new-session-staged').click();
  await expect(page.getByTestId('write-mode-staged')).toBeVisible();
  await expect.poll(() => new URL(page.url()).searchParams.get('session')).not.toBeNull();
  return new URL(page.url()).searchParams.get('session');
}

test('staged session: live for every participant, replaced staged when deleted', async ({
  browser,
  request,
}) => {
  const token = uniqueToken();
  const seeded = await seedNode(request, { name: `Live ${token}`, description: 'before' });
  const ctxA = await browser.newContext();
  const ctxB = await browser.newContext();
  const a = await ctxA.newPage();
  const b = await ctxB.newPage();

  const sessionId = await startStagedSession(a);
  await addNodeViaSearch(a, token, seeded.name);
  // B joins by link and learns the session is staged from the server.
  await b.goto(`/?session=${sessionId}`);
  await expect(b.getByTestId('write-mode-staged')).toBeVisible();
  await expect(canvasNode(b, seeded.name)).toBeVisible({ timeout: 15000 });

  // A's staged edit reaches B: the count, the marker and the composed content.
  await editDescription(a, seeded.name, 'staged by A');
  await expect(b.getByTestId('staged-pending-count')).toHaveText('1 pending change(s)', {
    timeout: 15000,
  });
  await expect(canvasNode(b, seeded.name).locator('.graph-custom-node')).toHaveClass(
    /staged-updated/
  );

  // B deletes the session: the dialog warns its staged change goes with it.
  await b.locator('.floating-header-hamburger').click();
  await b.locator('.session-drawer-session.current .session-context-menu-trigger').click();
  await b.locator('.session-context-menu-item.danger').click();
  await expect(b.getByText(/its 1 unmerged change\(s\) will be discarded/)).toBeVisible();
  await b.getByRole('button', { name: 'Delete' }).click();

  // Both land in new staged sessions, never in a direct one (§13).
  for (const page of [a, b]) {
    await expect
      .poll(() => new URL(page.url()).searchParams.get('session'), { timeout: 15000 })
      .not.toBe(sessionId);
    await expect(page.getByTestId('write-mode-staged')).toBeVisible();
    const replacement = new URL(page.url()).searchParams.get('session');
    const payload = await (await request.get(`/api/sessions/${replacement}`)).json();
    expect(payload.write_mode).toBe('staged');
  }
  expect((await readMainNode(request, seeded.id)).description).toBe('before');

  await ctxA.close();
  await ctxB.close();
});

test('switching a direct session to staged asks first, then stages', async ({ page, request }) => {
  const token = uniqueToken();
  const seeded = await seedNode(request, { name: `Switch ${token}`, description: 'before' });
  await page.goto('/');
  await expect(page.getByTestId('write-mode-direct')).toBeVisible();
  await addNodeViaSearch(page, token, seeded.name);

  await page.getByText('Switch to staged…').click();
  await expect(page.getByText(/This cannot be undone/)).toBeVisible();
  await page.getByRole('button', { name: 'Switch to staged', exact: true }).click();
  await expect(page.getByTestId('write-mode-staged')).toBeVisible();

  await editDescription(page, seeded.name, 'held back');
  await expect(page.getByTestId('staged-pending-count')).toHaveText('1 pending change(s)');
  expect((await readMainNode(request, seeded.id)).description).toBe('before');
});
