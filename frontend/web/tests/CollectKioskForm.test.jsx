// covers: frontend/web/src/components/CollectKioskView.jsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import CollectKioskView from '../src/components/CollectKioskView';
import * as api from '../src/services/api';

vi.mock('../src/services/api', () => ({
  getCollectConfig: vi.fn(),
  sendChatMessage: vi.fn(),
}));

describe('CollectKioskView form flow', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    if (!globalThis.crypto?.randomUUID) {
      globalThis.crypto = { randomUUID: () => Math.random().toString(36).slice(2) };
    }
  });

  it('renders a form from present_form and submits structured answers', async () => {
    api.getCollectConfig.mockResolvedValue({ name: 'Survey', introduction_text: 'Hi' });

    const formResponse = {
      content: 'Please answer:',
      toolUsed: 'present_form',
      toolResult: {
        action: 'present_form',
        form: {
          title: 'Q1',
          fields: [
            {
              id: 'role',
              label: 'Role',
              type: 'radio',
              options: ['Manager', 'Analyst'],
              required: true,
            },
          ],
        },
      },
    };
    // First call: kickstart returns the form. Second call: submission acknowledgement.
    api.sendChatMessage.mockResolvedValueOnce(formResponse).mockResolvedValueOnce({
      content: 'Saved, thank you.',
      toolUsed: 'save_collection_response',
      toolResult: null,
    });

    render(<CollectKioskView shortName="survey" />);

    // Dismiss the intro overlay to start the session.
    const startBtn = await screen.findByRole('button', { name: /Start/i });
    fireEvent.click(startBtn);

    // The AI-authored form should render.
    await waitFor(() => expect(screen.getByText('Q1')).toBeInTheDocument());
    expect(screen.getByText('Manager')).toBeInTheDocument();

    // Answer and submit.
    fireEvent.click(screen.getByLabelText('Manager'));
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }));

    await waitFor(() => expect(api.sendChatMessage).toHaveBeenCalledTimes(2));

    // The submission turn must carry the structured answers for save_collection_response.
    const submitCallHistory = api.sendChatMessage.mock.calls[1][0];
    const lastMsg = submitCallHistory[submitCallHistory.length - 1];
    expect(lastMsg.role).toBe('user');
    expect(lastMsg.content).toContain('save_collection_response');
    expect(lastMsg.content).toContain('"field_id":"role"');
    expect(lastMsg.content).toContain('Manager');
  });
});

describe('CollectKioskView rich-text rendering', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    if (!globalThis.crypto?.randomUUID) {
      globalThis.crypto = { randomUUID: () => Math.random().toString(36).slice(2) };
    }
  });

  async function startKioskWithAssistantMessage(content) {
    api.getCollectConfig.mockResolvedValue({ name: 'Survey', introduction_text: 'Hi' });
    api.sendChatMessage.mockResolvedValueOnce({ content, toolUsed: null, toolResult: null });

    render(<CollectKioskView shortName="survey" />);
    const startBtn = await screen.findByRole('button', { name: /Start/i });
    fireEvent.click(startBtn);
  }

  it('renders assistant markdown (bold, lists, links) the same way the main assistant does', async () => {
    await startKioskWithAssistantMessage(
      'Here is **bold** text, a list:\n\n- item one\n- item two\n\nand a [link](https://example.com).'
    );

    // Bold → <strong>, not literal "**bold**".
    const bold = await screen.findByText('bold');
    expect(bold.tagName).toBe('STRONG');
    expect(screen.queryByText(/\*\*bold\*\*/)).toBeNull();

    // List markers → real <li> inside a <ul>.
    const firstItem = screen.getByText('item one');
    expect(firstItem.closest('li')).not.toBeNull();
    expect(firstItem.closest('ul')).not.toBeNull();

    // Link syntax → real anchor with href.
    const link = screen.getByRole('link', { name: /link https:\/\/example\.com/ });
    expect(link).toHaveAttribute('href', 'https://example.com');
  });

  it('does not render raw HTML in assistant messages (inherits the safe-by-default path)', async () => {
    await startKioskWithAssistantMessage('Before <img src=x onerror="alert(1)"> after');

    // The literal HTML must be escaped and shown as text, never injected as a node.
    await screen.findByText(/onerror/);
    expect(document.querySelector('img')).toBeNull();
  });
});

describe('CollectKioskView conversation replay', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('replays only what was sent and received, under one key per opened kiosk', async () => {
    api.getCollectConfig.mockResolvedValue({ name: 'Survey' });
    const form = { title: 'Q1', fields: [{ id: 'q', label: 'Q', type: 'text' }] };
    api.sendChatMessage
      .mockResolvedValueOnce({
        content: 'Welcome',
        toolResult: { action: 'present_form', form },
        pending_actions: [{ token: 't', tool: 'add_nodes', arguments: {} }],
      })
      .mockRejectedValueOnce(new Error('down'))
      .mockResolvedValue({ content: 'Thanks' });
    const input = () => document.querySelector('.kiosk-input');

    const { unmount } = render(<CollectKioskView shortName="survey" />);
    fireEvent.click(await screen.findByRole('button', { name: /Start/i }));
    // The server's pending action is shown as a card.
    expect(await screen.findByText('add_nodes')).toBeInTheDocument();
    await waitFor(() => expect(input()).not.toBeDisabled());
    fireEvent.change(input(), { target: { value: 'first' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    await waitFor(() => expect(input()).not.toBeDisabled());
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }));
    await waitFor(() => expect(api.sendChatMessage).toHaveBeenCalledTimes(3));

    const [kickstart, message, submit] = api.sendChatMessage.mock.calls;
    const key = kickstart[2].conversationId;
    expect(key).toBeTruthy();
    expect([message[2].conversationId, submit[2].conversationId]).toEqual([key, key]);
    // The failed turn's error notice is display-only and stays out of the history.
    expect(submit[0].slice(0, 3)).toEqual([
      { role: 'user', content: '[COLLECTION_START]' },
      { role: 'assistant', content: 'Welcome' },
      { role: 'user', content: 'first' },
    ]);
    expect(submit[0]).toHaveLength(4);

    unmount();
    render(<CollectKioskView shortName="survey" />);
    fireEvent.click(await screen.findByRole('button', { name: /Start/i }));
    await waitFor(() => expect(api.sendChatMessage).toHaveBeenCalledTimes(4));
    expect(api.sendChatMessage.mock.calls[3][2].conversationId).not.toBe(key);
  });
});
