import { test, expect } from '@playwright/test';
import { addNodeViaSearch, seedNode, uniqueToken } from './helpers';

/**
 * Multi-context e2e for shared sessions (design step 8).
 *
 * Two browser contexts (two "users") join the *same* session by URL and drive
 * the real collaboration surface: presence, node add/move fan-out, annotation
 * create, delete-with-warning, and reconnect catch-up. The deterministic
 * core of these scenarios is also covered headlessly in
 * backend/core/tests/test_session_multiuser.py; this spec proves they hold
 * through the actual UI + SSE transport.
 *
 * Requires the backend + frontend dev servers (started by playwright.config.js).
 * Each test seeds the node it puts on the canvas, so the e2e graph may start
 * empty. Not part of any CI job — run with `npm run test:e2e`.
 */

const SESSION_URL = (id) => `/?session=${id}`;
const randomSessionId = () => {
  const d4 = () => String(Math.floor(1000 + Math.random() * 9000));
  return `${d4()}-${d4()}`;
};

const nodeCount = (page) => page.locator('.react-flow__node').count();

// A canvas node's position in flow coordinates, read from the translate that
// React Flow applies to it.
async function flowPosition(node) {
  const transform = await node.evaluate((el) => el.style.transform);
  const match = /translate\((-?[\d.]+)px, (-?[\d.]+)px\)/.exec(transform);
  expect(match, `unexpected node transform: ${transform}`).not.toBeNull();
  return { x: Math.round(Number(match[1])), y: Math.round(Number(match[2])) };
}

// Seeds a uniquely named node, puts it on `page`'s canvas via search and
// returns the resulting canvas node count.
async function seedNodes(page, request) {
  const token = uniqueToken();
  const node = await seedNode(request, { name: `Shared session ${token}` });
  await addNodeViaSearch(page, token, node.name);
  return nodeCount(page);
}

test.describe('shared session — two users, one session', () => {
  test('presence, node fan-out and note fan-out across clients', async ({ browser, request }) => {
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
    const countA = await seedNodes(a, request);
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

  test('node move syncs position to the other client', async ({ browser, request }) => {
    const sessionId = randomSessionId();
    const ctxA = await browser.newContext();
    const ctxB = await browser.newContext();
    const a = await ctxA.newPage();
    const b = await ctxB.newPage();
    await a.goto(SESSION_URL(sessionId));
    await b.goto(SESSION_URL(sessionId));
    // Wait until B's stream is live (A sees it on the roster) before seeding, so
    // the add reaches B as an op rather than racing B's initial load.
    await expect(a.locator('.floating-header-presence-dot').first()).toBeVisible({
      timeout: 15000,
    });

    const count = await seedNodes(a, request);
    await expect.poll(() => nodeCount(b), { timeout: 15000 }).toBe(count);

    // Compare flow coordinates (the node's own translate), not screen boxes:
    // B's viewport can pan or fit on its own, which moves the box on screen
    // without the node having moved at all.
    const nodeA = a.locator('.react-flow__node').first();
    const nodeB = b.locator('.react-flow__node').first();
    const before = await flowPosition(nodeB);
    expect(await flowPosition(nodeA)).toEqual(before);

    const box = await nodeA.boundingBox();
    await a.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await a.mouse.down();
    await a.mouse.move(box.x + 160, box.y + 120, { steps: 8 });
    await a.mouse.up();

    const moved = await flowPosition(nodeA);
    expect(Math.abs(moved.x - before.x) + Math.abs(moved.y - before.y)).toBeGreaterThan(20);
    await expect.poll(() => flowPosition(nodeB), { timeout: 15000 }).toEqual(moved);

    await ctxA.close();
    await ctxB.close();
  });

  test('deleting a session with another user connected warns about it', async ({
    browser,
    request,
  }) => {
    const sessionId = randomSessionId();
    const ctxA = await browser.newContext();
    const ctxB = await browser.newContext();
    const a = await ctxA.newPage();
    const b = await ctxB.newPage();
    await a.goto(SESSION_URL(sessionId));
    await b.goto(SESSION_URL(sessionId));

    // Make the session non-empty so it materialises server-side and appears in
    // the recents list, then wait until B is present on A's roster.
    await seedNodes(a, request);
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

  test('a client whose stream subscribes after ops landed still shows them', async ({
    browser,
    request,
  }) => {
    const sessionId = randomSessionId();
    const ctxA = await browser.newContext();
    const a = await ctxA.newPage();
    await a.goto(SESSION_URL(sessionId));
    await seedNodes(a, request);

    // Hold B's first stream request so its initial session GET completes
    // before A's next op lands, and its stream subscribes only afterwards.
    const ctxB = await browser.newContext();
    const b = await ctxB.newPage();
    let releaseStream;
    const streamHeld = new Promise((resolve) => {
      releaseStream = resolve;
    });
    let held = false;
    await b.route('**/api/sessions/*/stream*', async (route) => {
      if (!held) {
        held = true;
        await streamHeld;
      }
      await route.continue();
    });
    const loaded = b.waitForResponse(
      (r) => r.url().includes(`/api/sessions/${sessionId}?`) && r.request().method() === 'GET'
    );
    await b.goto(SESSION_URL(sessionId));
    await loaded;
    await expect.poll(() => held, { timeout: 15000 }).toBe(true);

    const opLanded = a.waitForResponse(
      (r) => r.url().includes(`/api/sessions/${sessionId}/ops`) && r.request().method() === 'POST'
    );
    const count = await seedNodes(a, request);
    expect((await opLanded).ok()).toBe(true);

    releaseStream();
    await expect.poll(() => nodeCount(b), { timeout: 15000 }).toBe(count);

    await ctxA.close();
    await ctxB.close();
  });

  // Also covers the late-joiner race: A's ops can land between B's initial
  // session GET and its stream subscribe, which the first snapshot's seq
  // must then trigger a resync for.
  test('reconnecting client catches up on the session state', async ({ browser, request }) => {
    const sessionId = randomSessionId();
    const ctxA = await browser.newContext();
    const a = await ctxA.newPage();
    await a.goto(SESSION_URL(sessionId));
    const count = await seedNodes(a, request);

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
