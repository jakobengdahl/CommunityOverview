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
