import { test, expect } from '@playwright/test';
import { canvasNode, uniqueToken } from './helpers';

/**
 * Desktop chat panel ("Graph assistant").
 *
 * No request reaches an LLM provider: every test answers POST /ui/chat itself
 * through page.route, so the replies are fixed and the tests check what the
 * panel does with them — what it sends, what it renders, and what a reply's
 * tool result does to the canvas. The panel is mounted only because the
 * backend reports an LLM as available (see the placeholder key in
 * playwright.config.js).
 */

const CHAT_URL = '**/ui/chat';

/**
 * Answers every chat request with `reply(body, index)` and records the parsed
 * request bodies, so a test can assert on exactly what the panel sent.
 */
async function mockChat(page, reply = () => ({ content: 'Mocked reply' })) {
  const requests = [];
  await page.route(CHAT_URL, async (route) => {
    const body = route.request().postDataJSON();
    requests.push(body);
    const answer = await reply(body, requests.length - 1);
    await route.fulfill({
      status: answer.status ?? 200,
      contentType: 'application/json',
      body: JSON.stringify(
        answer.status ? answer.body : { toolUsed: null, toolResult: null, ...answer }
      ),
    });
  });
  return requests;
}

async function openApp(page) {
  await page.goto('/');
  await expect(page.locator('.chat-panel-floating')).toBeVisible();
}

const panel = (page) => page.locator('.chat-panel-floating');
const input = (page) => panel(page).locator('.chat-input');
const sendButton = (page) => panel(page).locator('.chat-send-button');
const userMessages = (page) => panel(page).locator('.chat-message.user');
const assistantMessages = (page) => panel(page).locator('.chat-message.assistant');

test.describe('chat panel', () => {
  test('starts expanded and minimizes to a bar and back', async ({ page }) => {
    await openApp(page);
    await expect(panel(page).locator('.chat-header h3')).toHaveText('Graph assistant');

    await panel(page).locator('.chat-collapse-button').click();
    await expect(panel(page)).toHaveCount(0);
    const minimized = page.locator('.chat-panel-minimized');
    await expect(minimized).toBeVisible();

    await minimized.click();
    await expect(panel(page)).toBeVisible();
    await expect(minimized).toHaveCount(0);
  });

  test('greets with the welcome message and a disabled Send', async ({ page }) => {
    await openApp(page);

    await expect(assistantMessages(page)).toHaveCount(1);
    await expect(assistantMessages(page).first()).toContainText(
      'Welcome to Community Knowledge Graph'
    );
    await expect(userMessages(page)).toHaveCount(0);
    await expect(sendButton(page)).toBeDisabled();
  });

  test('Send posts the message, renders the reply and clears the composer', async ({ page }) => {
    const requests = await mockChat(page, () => ({ content: 'There are **three** nodes.' }));
    await openApp(page);

    await input(page).fill('What nodes are in the graph?');
    await sendButton(page).click();

    await expect(userMessages(page)).toHaveCount(1);
    await expect(userMessages(page).first()).toContainText('What nodes are in the graph?');
    await expect(assistantMessages(page).last()).toContainText('There are three nodes.');
    // The reply is rendered as Markdown, not shown as raw asterisks.
    await expect(assistantMessages(page).last().locator('strong')).toHaveText('three');
    await expect(input(page)).toHaveValue('');

    expect(requests).toHaveLength(1);
    // The welcome message is UI-only and never part of the conversation sent.
    expect(requests[0].messages).toEqual([
      { role: 'user', content: 'What nodes are in the graph?' },
    ]);
  });

  test('shows a processing state until the reply arrives', async ({ page }) => {
    let release;
    const held = new Promise((resolve) => {
      release = resolve;
    });
    await mockChat(page, async () => {
      await held;
      return { content: 'Done processing' };
    });
    await openApp(page);

    await input(page).fill('Search for AI');
    await sendButton(page).click();

    await expect(panel(page).locator('.loading-text')).toHaveText('Processing...');
    await expect(sendButton(page)).toBeDisabled();
    await expect(sendButton(page)).toHaveText('Processing...');
    await expect(input(page)).toBeDisabled();
    await expect(assistantMessages(page)).toHaveCount(1);

    release();

    await expect(assistantMessages(page).last()).toContainText('Done processing');
    await expect(panel(page).locator('.loading-text')).toHaveCount(0);
    await expect(input(page)).toBeEnabled();
  });

  test('Enter sends; Shift+Enter adds a line instead', async ({ page }) => {
    const requests = await mockChat(page);
    await openApp(page);

    await input(page).fill('Line 1');
    await input(page).press('Shift+Enter');
    await input(page).pressSequentially('Line 2');
    await expect(input(page)).toHaveValue('Line 1\nLine 2');
    await expect(userMessages(page)).toHaveCount(0);

    await input(page).press('Enter');
    await expect(userMessages(page)).toHaveCount(1);
    await expect(assistantMessages(page).last()).toContainText('Mocked reply');
    expect(requests).toHaveLength(1);
    expect(requests[0].messages.at(-1)).toEqual({ role: 'user', content: 'Line 1\nLine 2' });
  });

  test('a follow-up carries the earlier turns', async ({ page }) => {
    const requests = await mockChat(page, (_body, index) => ({ content: `Reply ${index + 1}` }));
    await openApp(page);

    await input(page).fill('Hello');
    await sendButton(page).click();
    await expect(assistantMessages(page).last()).toContainText('Reply 1');

    await input(page).fill('What did I just say?');
    await sendButton(page).click();
    await expect(assistantMessages(page).last()).toContainText('Reply 2');

    await expect(userMessages(page)).toHaveCount(2);
    expect(requests).toHaveLength(2);
    expect(requests[1].messages).toEqual([
      { role: 'user', content: 'Hello' },
      { role: 'assistant', content: 'Reply 1' },
      { role: 'user', content: 'What did I just say?' },
    ]);
  });

  test('a reply that returns nodes adds them to the canvas', async ({ page }) => {
    const name = `Chat added ${uniqueToken()}`;
    await mockChat(page, () => ({
      content: 'Added one node.',
      toolUsed: 'search_graph',
      toolResult: {
        action: 'add_to_visualization',
        nodes: [{ id: `node-${uniqueToken()}`, name, type: 'Initiative', description: 'e2e' }],
        edges: [],
      },
    }));
    await openApp(page);
    await expect(page.locator('.react-flow__node')).toHaveCount(0);

    await input(page).fill('Show me the new initiative');
    await sendButton(page).click();

    await expect(assistantMessages(page).last()).toContainText('Added one node.');
    await expect(canvasNode(page, name)).toBeVisible();
    await expect(page.locator('.react-flow__node')).toHaveCount(1);
  });

  test('a failed request is reported in the thread and the banner', async ({ page }) => {
    await mockChat(page, () => ({ status: 500, body: { error: 'Internal server error' } }));
    await openApp(page);

    await input(page).fill('This should fail');
    await sendButton(page).click();

    await expect(assistantMessages(page).last()).toHaveText(/Error: Internal server error/);
    await expect(panel(page).locator('.chat-error')).toHaveText('Internal server error');
    // The composer recovers, so the user can try again.
    await expect(input(page)).toBeEnabled();
  });
});

test.describe('chat file upload', () => {
  const fileInput = (page) => panel(page).locator('input[type="file"]');

  test('the file chip shows the name the user uploaded', async ({ page }) => {
    await openApp(page);
    await fileInput(page).setInputFiles({
      name: 'exact-name.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('Test content'),
    });
    await expect(panel(page).locator('.file-name')).toHaveText('exact-name.txt');
  });

  test('an uploaded text file is attached and can be removed', async ({ page }) => {
    await openApp(page);
    await expect(panel(page).locator('.chat-upload-button')).toHaveText('Upload');
    await expect(panel(page).locator('.file-indicator')).toHaveCount(0);

    await fileInput(page).setInputFiles({
      name: 'remove-test.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('Test content'),
    });
    await expect(panel(page).locator('.file-name')).toHaveText('remove-test.txt');
    // A file alone is enough to send: it is analysed without a typed prompt.
    await expect(sendButton(page)).toBeEnabled();

    await panel(page).locator('.remove-file-button').click();
    await expect(panel(page).locator('.file-indicator')).toHaveCount(0);
    await expect(sendButton(page)).toBeDisabled();
  });

  test('the attached file text is sent with the message', async ({ page }) => {
    const requests = await mockChat(page, () => ({ content: 'Read it.' }));
    const marker = `governance-${uniqueToken()}`;
    await openApp(page);

    await fileInput(page).setInputFiles({
      name: 'test-document.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from(`This is a test document about AI ${marker}.`),
    });
    await expect(panel(page).locator('.file-name')).toHaveText('test-document.txt');

    await input(page).fill('Summarise this');
    await sendButton(page).click();
    await expect(assistantMessages(page).last()).toContainText('Read it.');
    // Sending consumes the attachment.
    await expect(panel(page).locator('.file-indicator')).toHaveCount(0);

    expect(requests).toHaveLength(1);
    const sent = requests[0].messages.at(-1);
    expect(sent.role).toBe('user');
    expect(sent.content).toMatch(/^Summarise this\n\n\[Uploaded file: test-document\.txt\]/);
    expect(sent.content).toContain(marker);
  });
});
