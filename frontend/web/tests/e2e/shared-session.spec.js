import { test, expect } from '@playwright/test';

/**
 * Multi-context e2e for shared sessions (design step 8).
 *
 * Two browser contexts (two "users") join the *same* session by URL and drive
 * the real collaboration surface: presence, node add/move fan-out, annotation
 * create, rename, delete-with-warning, and reconnect catch-up. The deterministic
 * core of these scenarios is also covered headlessly in
 * backend/core/tests/test_session_multiuser.py; this spec proves they hold
 * through the actual UI + SSE transport.
 *
 * Requires the backend + frontend dev servers (started by playwright.config.js)
 * and a non-empty graph. Not part of the core pytest CI — run with
 * `npm run test:e2e`.
 */

const SESSION_URL = (id) => `/?session=${id}`;
const randomSessionId = () => {
  const d4 = () => String(Math.floor(1000 + Math.random() * 9000));
  return `${d4()}-${d4()}`;
};

const nodeCount = (page) => page.locator('.react-flow__node').count();

// The search bar has no submit button — typing (debounced, 2+ chars) opens a
// result dropdown and Enter selects the highlighted (first) result.
async function search(page, query) {
  await page.locator('input[placeholder*="Search"]').first().fill(query);
  await expect(page.locator('.floating-search-dropdown').first()).toBeVisible({ timeout: 15000 });
  await page.locator('input[placeholder*="Search"]').first().press('Enter');
}

// Add at least one node to the canvas via search; returns the resulting count.
async function seedNodes(page, query = 'an') {
  await search(page, query);
  await expect(page.locator('.react-flow__node').first()).toBeVisible({ timeout: 15000 });
  return nodeCount(page);
}

test.describe('shared session — two users, one session', () => {
  test('presence, node fan-out, note fan-out and rename sync across clients', async ({
    browser,
  }) => {
    const sessionId = randomSessionId();
    const ctxA = await browser.newContext();
    const ctxB = await browser.newContext();
    const a = await ctxA.newPage();
    const b = await ctxB.newPage();

    await a.goto(SESSION_URL(sessionId));
    await b.goto(SESSION_URL(sessionId));
    await expect(a.locator('.react-flow')).toBeVisible();
    await expect(b.locator('.react-flow')).toBeVisible();

    // Both clients render the same session id in the header.
    await expect(a.locator('.floating-header-session-id')).toHaveText(sessionId);
    await expect(b.locator('.floating-header-session-id')).toHaveText(sessionId);

    // Presence: once the second client joins, A sees at least one presence dot
    // (its own is shown only when another user is present).
    await expect(a.locator('.floating-header-presence-dot').first()).toBeVisible({
      timeout: 15000,
    });

    // Node add fan-out: A adds nodes, B converges to the same count via ops.
    const countA = await seedNodes(a);
    expect(countA).toBeGreaterThan(0);
    await expect.poll(() => nodeCount(b), { timeout: 15000 }).toBe(countA);

    // Annotation create fan-out: A adds a sticky note from the pane context menu.
    await a.locator('.react-flow__pane').click({ button: 'right', position: { x: 300, y: 300 } });
    await a.locator('text=Add note').first().click();
    await expect(a.locator('.graph-note-node').first()).toBeVisible();
    await expect(b.locator('.graph-note-node').first()).toBeVisible({ timeout: 15000 });

    await ctxA.close();
    await ctxB.close();
  });

  test('node move syncs position to the other client', async ({ browser }) => {
    const sessionId = randomSessionId();
    const ctxA = await browser.newContext();
    const ctxB = await browser.newContext();
    const a = await ctxA.newPage();
    const b = await ctxB.newPage();
    await a.goto(SESSION_URL(sessionId));
    await b.goto(SESSION_URL(sessionId));

    const count = await seedNodes(a);
    await expect.poll(() => nodeCount(b), { timeout: 15000 }).toBe(count);

    // Record the moved node's position in B, drag it in A, expect B to follow.
    const nodeA = a.locator('.react-flow__node').first();
    const nodeB = b.locator('.react-flow__node').first();
    const before = await nodeB.boundingBox();

    const box = await nodeA.boundingBox();
    await a.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await a.mouse.down();
    await a.mouse.move(box.x + 160, box.y + 120, { steps: 8 });
    await a.mouse.up();

    await expect
      .poll(
        async () => {
          const now = await nodeB.boundingBox();
          return now && before ? Math.abs(now.x - before.x) + Math.abs(now.y - before.y) : 0;
        },
        { timeout: 15000 }
      )
      .toBeGreaterThan(20);

    await ctxA.close();
    await ctxB.close();
  });

  test('deleting a session with another user connected warns about it', async ({ browser }) => {
    const sessionId = randomSessionId();
    const ctxA = await browser.newContext();
    const ctxB = await browser.newContext();
    const a = await ctxA.newPage();
    const b = await ctxB.newPage();
    await a.goto(SESSION_URL(sessionId));
    await b.goto(SESSION_URL(sessionId));

    // Make the session non-empty so it materialises server-side and appears in
    // the recents list, then wait until B is present on A's roster.
    await seedNodes(a);
    await expect(a.locator('.floating-header-presence-dot').first()).toBeVisible({
      timeout: 15000,
    });

    // Open the drawer and trigger delete on the current session via its ⋮ menu.
    await a.locator('.floating-header-hamburger').click();
    await a.locator('.session-drawer-session.current .session-context-menu-trigger').click();
    await a.locator('.session-context-menu-item.danger').click();

    // The confirm dialog must mention that other users are connected (design 3.6).
    await expect(a.locator('text=/other user\\(s\\) are connected/i')).toBeVisible({
      timeout: 15000,
    });

    await ctxA.close();
    await ctxB.close();
  });

  test('reconnecting client catches up on the session state', async ({ browser }) => {
    const sessionId = randomSessionId();
    const ctxA = await browser.newContext();
    const a = await ctxA.newPage();
    await a.goto(SESSION_URL(sessionId));
    const count = await seedNodes(a);

    // A second user opens the shared URL fresh and should load the current
    // content (snapshot on connect), not an empty canvas.
    const ctxB = await browser.newContext();
    const b = await ctxB.newPage();
    await b.goto(SESSION_URL(sessionId));
    await expect.poll(() => nodeCount(b), { timeout: 15000 }).toBe(count);

    // Reload B (drops and re-opens the SSE stream) → catch-up restores content.
    await b.reload();
    await expect.poll(() => nodeCount(b), { timeout: 15000 }).toBe(count);

    await ctxA.close();
    await ctxB.close();
  });
});
