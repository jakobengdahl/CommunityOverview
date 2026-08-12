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
    const link = screen.getByRole('link', { name: 'link' });
    expect(link).toHaveAttribute('href', 'https://example.com');
  });

  it('does not render raw HTML in assistant messages (inherits the safe-by-default path)', async () => {
    await startKioskWithAssistantMessage('Before <img src=x onerror="alert(1)"> after');

    // The literal HTML must be escaped and shown as text, never injected as a node.
    await screen.findByText(/onerror/);
    expect(document.querySelector('img')).toBeNull();
  });
});
