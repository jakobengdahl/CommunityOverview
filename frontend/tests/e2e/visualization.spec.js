/**
 * E2E tests for visualization updates and context menu
 * Run with: npm run test:e2e (if playwright is configured)
 */

// Note: This is a template. You may need to install Playwright:
// npm install -D @playwright/test
// npx playwright install

import { test, expect } from '@playwright/test';

test.describe('Visualization Updates', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate to the app
    await page.goto('http://localhost:3000');

    // Wait for app to load
    await page.waitForSelector('.chat-panel');
  });

  test('should update visualization when searching for NIS2 project', async ({ page }) => {
    // Enable console logging
    page.on('console', msg => console.log('BROWSER:', msg.text()));

    // Type in chat
    await page.fill('.chat-input', 'visa NIS2-projekt');

    // Click send
    await page.click('.chat-send-button');

    // Wait for response (adjust timeout as needed)
    await page.waitForTimeout(10000);

    // Check that visualization has nodes
    const nodes = await page.locator('.react-flow__node').count();
    expect(nodes).toBeGreaterThan(0);

    // Take screenshot
    await page.screenshot({
      path: 'test-results/visualization-with-nis2.png',
      fullPage: true
    });

    // Verify console logs show correct flow
    // Note: This requires more sophisticated console log capture
  });

  test('should show custom context menu on right-click', async ({ page }) => {
    // First, load some nodes
    await page.fill('.chat-input', 'visa NIS2-projekt');
    await page.click('.chat-send-button');
    await page.waitForTimeout(8000);

    // Find the ReactFlow pane
    const reactFlowPane = await page.locator('.react-flow__pane');

    // Get bounding box to click in empty area
    const box = await reactFlowPane.boundingBox();

    // Right-click in the middle of the pane
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2, {
      button: 'right'
    });

    // Wait a bit for menu to appear
    await page.waitForTimeout(500);

    // Check if custom context menu is visible
    const contextMenu = await page.locator('.context-menu');
    await expect(contextMenu).toBeVisible();

    // Take screenshot
    await page.screenshot({
      path: 'test-results/context-menu-showing.png',
      fullPage: true
    });

    // Verify menu has expected content
    await expect(page.locator('.context-menu-item')).toContainText('Rectangle');
  });

  test('should hide node when right-clicking on it', async ({ page }) => {
    // Load some nodes
    await page.fill('.chat-input', 'visa NIS2-projekt');
    await page.click('.chat-send-button');
    await page.waitForTimeout(8000);

    // Get initial node count
    const initialCount = await page.locator('.react-flow__node').count();

    // Right-click on first node
    const firstNode = page.locator('.react-flow__node').first();
    await firstNode.click({ button: 'right' });

    // Wait for node to be hidden
    await page.waitForTimeout(500);

    // Check that node count decreased
    const newCount = await page.locator('.react-flow__node').count();
    expect(newCount).toBeLessThan(initialCount);

    // Take screenshot
    await page.screenshot({
      path: 'test-results/node-hidden.png',
      fullPage: true
    });
  });
});

test.describe('Console Logging', () => {
  test('should log correct messages during visualization update', async ({ page }) => {
    const consoleLogs = [];

    page.on('console', msg => {
      consoleLogs.push(msg.text());
    });

    await page.goto('http://localhost:3000');
    await page.waitForSelector('.chat-panel');

    // Trigger visualization update
    await page.fill('.chat-input', 'visa NIS2-projekt');
    await page.click('.chat-send-button');
    await page.waitForTimeout(10000);

    // Check for expected log messages
    const hasBackendResponse = consoleLogs.some(log =>
      log.includes('[ChatPanel] ========== BACKEND RESPONSE ==========')
    );
    expect(hasBackendResponse).toBe(true);

    const hasUpdateVisualization = consoleLogs.some(log =>
      log.includes('[ChatPanel] Calling updateVisualization')
    );
    expect(hasUpdateVisualization).toBe(true);

    const hasStateUpdate = consoleLogs.some(log =>
      log.includes('[GraphStore] State updated successfully')
    );
    expect(hasStateUpdate).toBe(true);

    // Save logs to file for inspection
    const fs = require('fs');
    fs.writeFileSync(
      'test-results/console-logs.txt',
      consoleLogs.join('\n')
    );
  });
});
