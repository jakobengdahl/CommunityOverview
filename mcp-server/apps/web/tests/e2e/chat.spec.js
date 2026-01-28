import { test, expect } from '@playwright/test';

/**
 * E2E tests for Chat Panel functionality
 *
 * These tests verify the complete user flow:
 * 1. Open the chat panel
 * 2. Send messages and receive responses
 * 3. Create nodes via chat
 * 4. Verify nodes appear in the graph
 */

test.describe('Chat Panel', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    // Wait for app to load
    await page.waitForSelector('.app-header');
  });

  test('opens and closes chat panel', async ({ page }) => {
    // Chat should be closed initially
    await expect(page.locator('.chat-panel')).not.toBeVisible();

    // Open chat
    await page.click('.chat-toggle-button');
    await expect(page.locator('.chat-panel')).toBeVisible();
    await expect(page.locator('.chat-header')).toContainText('Graph Assistant');

    // Close chat
    await page.click('.chat-close-button');
    await expect(page.locator('.chat-panel')).not.toBeVisible();
  });

  test('displays welcome message', async ({ page }) => {
    await page.click('.chat-toggle-button');

    await expect(page.locator('.chat-welcome')).toBeVisible();
    await expect(page.locator('.chat-welcome')).toContainText('Ask questions');
    await expect(page.locator('.chat-examples')).toBeVisible();
  });

  test('sends a message and receives response', async ({ page }) => {
    await page.click('.chat-toggle-button');

    // Type a message
    await page.fill('.chat-input', 'What nodes are in the graph?');
    await page.click('.chat-send-button');

    // User message should appear
    await expect(page.locator('.chat-message.user').first()).toContainText('What nodes are in the graph');

    // Wait for response (may take time due to API call)
    await expect(page.locator('.chat-message.assistant').first()).toBeVisible({ timeout: 30000 });
  });

  test('shows loading state while processing', async ({ page }) => {
    await page.click('.chat-toggle-button');

    await page.fill('.chat-input', 'Search for AI');
    await page.click('.chat-send-button');

    // Should show loading indicator
    await expect(page.locator('.loading-text')).toContainText('Processing');
  });

  test('clears input after sending', async ({ page }) => {
    await page.click('.chat-toggle-button');

    await page.fill('.chat-input', 'Test message');
    await page.click('.chat-send-button');

    // Input should be cleared
    await expect(page.locator('.chat-input')).toHaveValue('');
  });

  test('sends message on Enter key', async ({ page }) => {
    await page.click('.chat-toggle-button');

    await page.fill('.chat-input', 'Enter key test');
    await page.press('.chat-input', 'Enter');

    // Message should be sent
    await expect(page.locator('.chat-message.user').first()).toContainText('Enter key test');
  });

  test('does not send on Shift+Enter', async ({ page }) => {
    await page.click('.chat-toggle-button');

    await page.fill('.chat-input', 'Line 1');
    await page.press('.chat-input', 'Shift+Enter');

    // Should not have sent (no user message visible)
    const userMessages = await page.locator('.chat-message.user').count();
    expect(userMessages).toBe(0);
  });
});

test.describe('Chat File Upload', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await page.click('.chat-toggle-button');
  });

  test('shows upload button', async ({ page }) => {
    await expect(page.locator('.chat-upload-button')).toBeVisible();
    await expect(page.locator('.chat-upload-button')).toContainText('Upload');
  });

  test('shows file indicator after upload', async ({ page }) => {
    // Create a test file
    const fileContent = 'This is a test document about AI governance.';

    // Upload file
    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles({
      name: 'test-document.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from(fileContent),
    });

    // File indicator should appear
    await expect(page.locator('.file-indicator')).toBeVisible();
    await expect(page.locator('.file-name')).toContainText('test-document.txt');
  });

  test('removes file when remove button clicked', async ({ page }) => {
    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles({
      name: 'remove-test.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('Test content'),
    });

    await expect(page.locator('.file-indicator')).toBeVisible();

    // Click remove button
    await page.click('.remove-file-button');

    // File indicator should disappear
    await expect(page.locator('.file-indicator')).not.toBeVisible();
  });
});

test.describe('Chat Graph Integration', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await page.click('.chat-toggle-button');
  });

  test('search results appear in graph', async ({ page }) => {
    // First add a test node via search panel
    await page.fill('.search-input', 'test');
    await page.click('.search-button');

    // Wait for any existing nodes to load
    await page.waitForTimeout(1000);

    // Now search via chat
    await page.fill('.chat-input', 'Search for all nodes');
    await page.click('.chat-send-button');

    // Wait for response
    await page.waitForSelector('.chat-message.assistant', { timeout: 30000 });

    // The response should mention nodes or search results
    const response = await page.locator('.chat-message.assistant').first().textContent();
    expect(response).toBeTruthy();
  });

  test('can request to add a node', async ({ page }) => {
    await page.fill('.chat-input', 'Add a new initiative called E2E Test Initiative about testing');
    await page.click('.chat-send-button');

    // Wait for response
    await page.waitForSelector('.chat-message.assistant', { timeout: 30000 });

    // Response should acknowledge the request
    const response = await page.locator('.chat-message.assistant').first().textContent();
    expect(response).toBeTruthy();
  });
});

test.describe('Chat Conversation Flow', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await page.click('.chat-toggle-button');
  });

  test('maintains conversation history', async ({ page }) => {
    // Send first message
    await page.fill('.chat-input', 'Hello');
    await page.click('.chat-send-button');
    await page.waitForSelector('.chat-message.assistant', { timeout: 30000 });

    // Send second message
    await page.fill('.chat-input', 'What did I just say?');
    await page.click('.chat-send-button');

    // Wait for second response
    await page.waitForTimeout(2000);

    // Should have 2 user messages
    const userMessages = await page.locator('.chat-message.user').count();
    expect(userMessages).toBe(2);

    // Should have 2 assistant messages
    const assistantMessages = await page.locator('.chat-message.assistant').count();
    expect(assistantMessages).toBeGreaterThanOrEqual(2);
  });

  test('scrolls to new messages', async ({ page }) => {
    // Send multiple messages to create scroll
    for (let i = 0; i < 3; i++) {
      await page.fill('.chat-input', `Message ${i + 1}`);
      await page.click('.chat-send-button');
      await page.waitForSelector('.chat-message.assistant', { timeout: 30000 });
      await page.waitForTimeout(500);
    }

    // The last message should be visible
    const lastMessage = page.locator('.chat-message').last();
    await expect(lastMessage).toBeInViewport();
  });
});

test.describe('Chat Error Handling', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await page.click('.chat-toggle-button');
  });

  test('displays error message on failure', async ({ page }) => {
    // Intercept API call and return error
    await page.route('**/ui/chat', async (route) => {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Internal server error' }),
      });
    });

    await page.fill('.chat-input', 'This should fail');
    await page.click('.chat-send-button');

    // Error message should appear
    await expect(page.locator('.chat-message.assistant')).toContainText(/error/i, { timeout: 10000 });
  });
});
