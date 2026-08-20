import { test, expect } from '@playwright/test';

/**
 * Mobile smoke tests — the guard rail for phone-sized viewports.
 *
 * These run only under the `mobile-iphone` / `mobile-pixel` projects (see
 * playwright.config.js), which emulate touch and a coarse pointer. The app's
 * own mobile behaviour keys off `(pointer: coarse)`, so nothing here reproduces
 * on the desktop project.
 */

// Matches LONG_PRESS_DELAY_MS in packages/ui-graph-canvas/src/utils/longPress.js.
// Held well past it so a slow CI runner cannot shave the press below the
// threshold; the detector fires on the timer, not on release.
const LONG_PRESS_HOLD_MS = 900;

// Surfaces that already hang off a viewport edge on a phone, excluded so this
// check reports *new* breaks rather than failing on ones that predate it.
// Delete an entry once its surface is fixed and the check covers it again.
//
// Kept per-edge rather than as one list: exempting a subtree from both edges
// costs far more coverage than the break being silenced. The chat panel is the
// largest subtree in the shell, and only its left edge is broken.
//
const KNOWN_RIGHT_OVERFLOW = [];
// .chat-panel-floating — a fixed 380px anchored 16px from the right edge, so at
//   390px its left edge sits at x~-6. Its right edge is inside on both devices.
const KNOWN_LEFT_OVERFLOW = ['.chat-panel-floating'];

/**
 * Finds a point inside the first canvas node that nothing else covers.
 *
 * The floating toolbar overlays a large part of a phone viewport, and a freshly
 * created node is centred underneath it, so the node's own centre often belongs
 * to the toolbar. Probing with elementFromPoint picks a spot the press will
 * actually reach.
 */
async function touchPointOnNode(page) {
  let point = null;
  let previousRect = null;
  // Polled for two reasons: a newly created node is animated to the centre of
  // the viewport, so an early probe measures a position it is only passing
  // through, and the point has to be one nothing else covers once it lands.
  await expect
    .poll(
      async () => {
        const sample = await page.evaluate(() => {
          const node = document.querySelector('.react-flow__node');
          if (!node) return null;
          const id = node.getAttribute('data-id');
          const rect = node.getBoundingClientRect();
          let free = null;
          for (let column = 1; column <= 9 && !free; column += 1) {
            for (let row = 1; row <= 9 && !free; row += 1) {
              const x = Math.round(rect.left + (rect.width * column) / 10);
              const y = Math.round(rect.top + (rect.height * row) / 10);
              const hit = document.elementFromPoint(x, y);
              if (hit?.closest('.react-flow__node')?.getAttribute('data-id') === id) {
                free = { x, y };
              }
            }
          }
          return { key: `${rect.left},${rect.top},${rect.width},${rect.height}`, free };
        });
        const settled = sample !== null && sample.key === previousRect;
        previousRect = sample?.key ?? null;
        point = sample?.free ?? null;
        return settled && point !== null;
      },
      { message: 'node never settled on an uncovered point to press' }
    )
    .toBe(true);
  return point;
}

/**
 * Presses and holds at `point` using real touch events, running `whileHeld`
 * before the finger lifts.
 *
 * Playwright's touchscreen API only taps, and the canvas ignores any pointer
 * whose `pointerType` is not `touch`, so the press is driven through CDP.
 * Asserting while the touch is still down is the point: the detector fires on
 * its timer and explicitly has no long-press-on-release, so an assertion made
 * after the lift would also pass if that contract were inverted.
 */
async function longPress(page, point, whileHeld, holdMs = LONG_PRESS_HOLD_MS) {
  const cdp = await page.context().newCDPSession(page);
  try {
    await cdp.send('Input.dispatchTouchEvent', {
      type: 'touchStart',
      touchPoints: [{ x: point.x, y: point.y, id: 1 }],
    });
    await page.waitForTimeout(holdMs);
    await whileHeld();
  } finally {
    // Swallowed: if whileHeld() failed, that error is the one worth reporting.
    await cdp
      .send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
      .catch(() => {});
    await cdp.detach().catch(() => {});
  }
}

/**
 * Waits for every finite CSS animation and transition to finish.
 *
 * Panels slide in with a transform, so geometry measured on the first frame
 * reports positions the user never sees.
 */
async function settleAnimations(page) {
  await page.evaluate(() =>
    Promise.all(
      document
        .getAnimations()
        .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
        .map((animation) => animation.finished.catch(() => {}))
    )
  );
}

/** The chat panel opens expanded and covers the toolbar on a phone. */
async function minimizeChat(page) {
  const collapse = page.locator('.chat-collapse-button');
  if (await collapse.isVisible()) {
    await collapse.tap();
  }
  await expect(page.locator('.chat-panel-minimized')).toBeVisible();
}

async function openApp(page) {
  await page.goto('/');
  await expect(page.locator('.react-flow')).toBeVisible();
  // The chat panel mounts only once /ui/capabilities has reported an LLM.
  // Waiting for it means every assertion below measures a fully mounted shell,
  // rather than racing that fetch and missing the panel entirely.
  await expect(page.locator('.chat-panel-floating, .chat-panel-minimized')).toBeVisible();
  await settleAnimations(page);
  // The coarse-pointer branch is what every assertion below depends on; if the
  // project stopped emulating touch these tests would silently pass as desktop.
  await expect(page.locator('.app.is-touch')).toBeVisible();
}

async function createNodeFromToolbox(page, nodeType, name) {
  await minimizeChat(page);
  await page.locator(`.floating-toolbar-item[aria-label="${nodeType}"]`).tap();

  const dialog = page.locator('.create-node-dialog');
  await expect(dialog).toBeVisible();
  await dialog.locator('#create-name').fill(name);
  await dialog.locator('button[type="submit"]').tap();
  await expect(dialog).toBeHidden();
}

test.describe('mobile shell', () => {
  test('search stays below the header and uses the available phone width', async ({ page }) => {
    await openApp(page);

    const geometry = await page.evaluate(() => {
      const header = document.querySelector('.floating-header').getBoundingClientRect();
      const search = document.querySelector('.floating-search').getBoundingClientRect();
      return { headerBottom: header.bottom, searchTop: search.top, searchWidth: search.width };
    });

    expect(geometry.searchTop).toBeGreaterThanOrEqual(geometry.headerBottom + 7);
    expect(geometry.searchWidth).toBeGreaterThanOrEqual(page.viewportSize().width - 40);
  });

  test('lays out inside the viewport width', async ({ page }) => {
    await openApp(page);

    const overflowing = await page.evaluate(
      (allowed) => {
        const viewportWidth = window.innerWidth;
        const offenders = [];
        const exempt = (el, selectors) => selectors.some((selector) => el.closest(selector));
        for (const el of document.querySelectorAll('body *')) {
          const style = getComputedStyle(el);
          if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
            continue;
          }
          const rect = el.getBoundingClientRect();
          if (rect.width === 0 && rect.height === 0) continue;
          // Both edges: content pushed off the left is as unreachable as content
          // pushed off the right, and a fixed panel can do either.
          const overRight = rect.right > viewportWidth + 1 && !exempt(el, allowed.right);
          const overLeft = rect.left < -1 && !exempt(el, allowed.left);
          if (overRight || overLeft) {
            const className = typeof el.className === 'string' ? el.className : '';
            offenders.push(
              `${el.tagName.toLowerCase()}.${className} left=${Math.round(rect.left)} right=${Math.round(rect.right)}`
            );
          }
        }
        return { viewportWidth, offenders };
      },
      { right: KNOWN_RIGHT_OVERFLOW, left: KNOWN_LEFT_OVERFLOW }
    );

    expect(
      overflowing.offenders,
      `elements hang off the edge of the ${overflowing.viewportWidth}px viewport`
    ).toEqual([]);
  });

  test('hamburger opens the session drawer', async ({ page }) => {
    await openApp(page);

    const drawer = page.locator('.session-drawer');
    await expect(drawer).not.toHaveClass(/\bopen\b/);

    await page.locator('.floating-header-hamburger').tap();

    await expect(drawer).toHaveClass(/\bopen\b/);
    await expect(drawer.locator('.session-drawer-item').first()).toBeVisible();
  });

  test('a node can be created from the toolbox', async ({ page }, testInfo) => {
    await openApp(page);
    await createNodeFromToolbox(page, 'Actor', `Toolbox actor ${testInfo.project.name}`);

    await expect(page.locator('.react-flow__node')).toHaveCount(1);
  });

  test('long-press on a node opens its context menu', async ({ page }, testInfo) => {
    await openApp(page);
    await createNodeFromToolbox(page, 'Actor', `Long-press actor ${testInfo.project.name}`);

    await expect(page.locator('.react-flow__node')).toHaveCount(1);
    await expect(page.locator('.node-context-menu')).toHaveCount(0);

    await longPress(page, await touchPointOnNode(page), async () => {
      await expect(page.locator('.node-context-menu')).toBeVisible();
    });
  });

  test('the chat panel reopens with a reachable composer', async ({ page }) => {
    await openApp(page);
    await minimizeChat(page);

    await page.locator('.chat-panel-minimized').tap();

    const composer = page.locator('.chat-input');
    await expect(composer).toBeVisible();

    // Reachable means the composer's whole box sits inside the layout viewport.
    // A panel that grows past the bottom edge leaves it visible to the DOM but
    // off screen to the user, which is what this catches.
    const fits = await composer.evaluate((el) => {
      const rect = el.getBoundingClientRect();
      return {
        top: rect.top,
        bottom: rect.bottom,
        viewportHeight: window.innerHeight,
      };
    });
    expect(fits.top).toBeGreaterThanOrEqual(0);
    expect(fits.bottom).toBeLessThanOrEqual(fits.viewportHeight);
  });
});
