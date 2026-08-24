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
const KNOWN_RIGHT_OVERFLOW = [];
const KNOWN_LEFT_OVERFLOW = [];

/**
 * Finds a point inside a canvas node that nothing else covers.
 *
 * The floating toolbar overlays a large part of a phone viewport, and a freshly
 * created node is centred underneath it, so the node's own centre often belongs
 * to the toolbar. Probing with elementFromPoint picks a spot the press will
 * actually reach.
 *
 * `index` selects among the canvas nodes in DOM order; with several on screen
 * the earlier ones can end up underneath the later ones, which is not a state
 * this probe can resolve.
 */
async function touchPointOnNode(page, index = 0) {
  let point = null;
  let previousRect = null;
  // Polled for two reasons: a newly created node is animated to the centre of
  // the viewport, so an early probe measures a position it is only passing
  // through, and the point has to be one nothing else covers once it lands.
  await expect
    .poll(
      async () => {
        const sample = await page.evaluate((nodeIndex) => {
          const node = document.querySelectorAll('.react-flow__node')[nodeIndex];
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
        }, index);
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

async function expectMobileChatSheet(page) {
  await expect(page.locator('.bottom-sheet:has(.chat-panel-sheet)')).toBeVisible();
}

async function closeChatSheet(page) {
  const close = page.locator('.bottom-sheet:has(.chat-panel-sheet) .bottom-sheet-close');
  if (await close.isVisible()) {
    await close.tap();
  }
  await expect(page.locator('.bottom-sheet:has(.chat-panel-sheet)')).toHaveCount(0);
}

async function openChatSheet(page) {
  const chatNav = page.locator('.mobile-shell-bottomnav-item[aria-label="Chat"]');
  await expect(chatNav).toBeVisible();
  if ((await page.locator('.bottom-sheet:has(.chat-panel-sheet)').count()) === 0) {
    await chatNav.tap();
  }
  await expectMobileChatSheet(page);
}

async function openApp(page) {
  await page.goto('/');
  await expect(page.locator('.react-flow')).toBeVisible();
  // On mobile the chat surface is a bottom sheet, mounted only after
  // /ui/capabilities has reported an LLM. Waiting for the sheet means every
  // assertion below measures a fully mounted shell rather than racing startup.
  await expectMobileChatSheet(page);
  await settleAnimations(page);
  // The coarse-pointer branch is what every assertion below depends on; if the
  // project stopped emulating touch these tests would silently pass as desktop.
  await expect(page.locator('.app.is-touch')).toBeVisible();
  await expect(page.locator('.mobile-shell-bottomnav')).toBeVisible();
}

/**
 * Opens the Create bottom sheet via the nav (this also minimizes the chat
 * panel if it was expanded — MobileShell keeps at most one surface open).
 */
async function openCreatePanel(page) {
  await page.locator('.mobile-shell-bottomnav-item[aria-label="Create"]').tap();
  await expect(page.locator('.bottom-sheet')).toBeVisible();
}

async function createNodeFromToolbox(page, nodeType, name) {
  await openCreatePanel(page);
  await page.locator(`.floating-toolbar-item[aria-label="${nodeType}"]`).tap();

  const dialog = page.locator('.create-node-dialog');
  await expect(dialog).toBeVisible();
  await dialog.locator('#create-name').fill(name);
  await dialog.locator('button[type="submit"]').tap();
  await expect(dialog).toBeHidden();
  // Picking a type closes the sheet — it does not linger over the canvas.
  await expect(page.locator('.bottom-sheet')).toHaveCount(0);
}

test.describe('mobile shell', () => {
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

  test('the Menu nav item opens the session drawer', async ({ page }) => {
    await openApp(page);

    const drawer = page.locator('.session-drawer');
    await expect(drawer).not.toHaveClass(/\bopen\b/);

    await page.locator('.mobile-shell-bottomnav-item[aria-label="Menu"]').tap();

    await expect(drawer).toHaveClass(/\bopen\b/);
    await expect(drawer.locator('.session-drawer-item').first()).toBeVisible();
  });

  test('at most one surface covers the canvas at a time', async ({ page }) => {
    await openApp(page);
    // Chat starts expanded by default as the mobile Assistant bottom sheet.
    await expectMobileChatSheet(page);

    await page.locator('.mobile-shell-bottomnav-item[aria-label="Create"]').tap();
    await expect(page.locator('.bottom-sheet:has(.floating-toolbar)')).toBeVisible();
    // Opening the create sheet must close chat first.
    await expect(page.locator('.bottom-sheet:has(.chat-panel-sheet)')).toHaveCount(0);

    await page.locator('.mobile-shell-bottomnav-item[aria-label="Chat"]').tap();
    // Opening chat must close the create sheet.
    await expect(page.locator('.bottom-sheet:has(.floating-toolbar)')).toHaveCount(0);
    await expectMobileChatSheet(page);

    await page.locator('.mobile-shell-bottomnav-item[aria-label="Menu"]').tap();
    // Opening the menu drawer must close chat too.
    await expect(page.locator('.session-drawer')).toHaveClass(/\bopen\b/);
    await expect(page.locator('.bottom-sheet:has(.chat-panel-sheet)')).toHaveCount(0);
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

  test('canvas controls claim non-overlapping space', async ({ page }) => {
    await openApp(page);

    // The desktop control cluster is replaced, not merely restyled: its 26px
    // buttons are below any usable touch target.
    await expect(page.locator('.react-flow__controls')).toHaveCount(0);
    await expect(page.locator('.graph-compact-controls')).toBeVisible();

    // Every interactive surface the canvas positions for itself. A selector
    // that is absent in this profile (the minimap is off by default, the depth
    // control needs a federated deployment) contributes nothing rather than
    // failing — the check is about the ones that are on screen together.
    const CANVAS_CONTROLS = [
      '.graph-compact-controls',
      '.federation-depth-control',
      '.react-flow__controls',
      '.react-flow__minimap',
      '.graph-canvas-controls',
      '.graph-notification',
    ];

    const overlaps = await page.evaluate((selectors) => {
      const boxes = [];
      for (const selector of selectors) {
        for (const el of document.querySelectorAll(selector)) {
          const style = getComputedStyle(el);
          if (style.display === 'none' || style.visibility === 'hidden') continue;
          const rect = el.getBoundingClientRect();
          if (rect.width === 0 || rect.height === 0) continue;
          boxes.push({ selector, rect });
        }
      }
      const found = [];
      for (let i = 0; i < boxes.length; i += 1) {
        for (let j = i + 1; j < boxes.length; j += 1) {
          const a = boxes[i].rect;
          const b = boxes[j].rect;
          const intersects =
            a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
          if (intersects) found.push(`${boxes[i].selector} overlaps ${boxes[j].selector}`);
        }
      }
      return { measured: boxes.map((b) => b.selector), found };
    }, CANVAS_CONTROLS);

    expect(overlaps.measured).toContain('.graph-compact-controls');
    expect(overlaps.found, 'canvas controls overlap each other').toEqual([]);
  });

  test('the compact controls are touch-sized', async ({ page }) => {
    await openApp(page);

    const undersized = await page.evaluate(() =>
      Array.from(document.querySelectorAll('.graph-compact-controls button'))
        .map((el) => ({ label: el.getAttribute('aria-label'), rect: el.getBoundingClientRect() }))
        .filter(({ rect }) => rect.width < 44 || rect.height < 44)
        .map(({ label, rect }) => `${label} ${Math.round(rect.width)}x${Math.round(rect.height)}`)
    );

    expect(undersized).toEqual([]);
  });

  test('a node can be focused and unfocused without a context menu', async ({ page }, testInfo) => {
    await openApp(page);
    // Two unrelated nodes, so entering focus visibly narrows the canvas to one.
    await createNodeFromToolbox(page, 'Actor', `Focus actor ${testInfo.project.name}`);
    await createNodeFromToolbox(page, 'Actor', `Other actor ${testInfo.project.name}`);
    await expect(page.locator('.react-flow__node')).toHaveCount(2);

    const focus = page.locator('.graph-compact-control-focus');
    await expect(focus).toBeDisabled();

    // Selecting by tapping the node is the whole entry path — no long press,
    // no context menu.
    const point = await touchPointOnNode(page, 1);
    await page.touchscreen.tap(point.x, point.y);
    await expect(focus).toBeEnabled();

    await focus.tap();
    await expect(focus).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('.react-flow__node')).toHaveCount(1);

    // ...and the same control takes the whole canvas back.
    await focus.tap();
    await expect(focus).toHaveAttribute('aria-pressed', 'false');
    await expect(page.locator('.react-flow__node')).toHaveCount(2);
  });

  test('the chat panel reopens with a reachable composer', async ({ page }) => {
    await openApp(page);
    await closeChatSheet(page);

    await openChatSheet(page);

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
