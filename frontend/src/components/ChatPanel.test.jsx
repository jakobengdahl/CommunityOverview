/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import ChatPanel from './ChatPanel';
import useGraphStore from '../store/graphStore';

// Mock dependencies
// Note: The component now uses sendMessageToBackend from ../services/api
// instead of mcpClient/claude directly.
vi.mock('../services/api', () => ({
  sendMessageToBackend: vi.fn().mockResolvedValue({
    content: "Mock backend response",
    toolUsed: null,
    toolResult: null
  }),
  uploadFileToBackend: vi.fn().mockResolvedValue({
    success: true,
    text: "Extracted text"
  })
}));

describe('ChatPanel', () => {
  beforeEach(() => {
    // Mock scrollIntoView
    window.HTMLElement.prototype.scrollIntoView = vi.fn();

    useGraphStore.setState({
      chatMessages: [],
      addChatMessage: (msg) => useGraphStore.setState(state => ({ chatMessages: [...state.chatMessages, msg] })),
      selectedCommunities: ['test-community'],
      updateVisualization: vi.fn(),
      highlightNodes: vi.fn(),
      setLoading: vi.fn(),
      setError: vi.fn(),
      clearError: vi.fn()
    });
  });

  afterEach(() => {
    cleanup();
  });

  it('renders input field and send button', () => {
    render(<ChatPanel />);
    // cleanup should handle it, but using getAll just to be safe if environment is quirky
    const inputs = screen.getAllByPlaceholderText(/Ask a question/i);
    expect(inputs[0]).toBeDefined();
    expect(screen.getAllByRole('button', { name: /Send/i })[0]).toBeDefined();
  });

  it('handles user input and submission', async () => {
    render(<ChatPanel />);

    const inputs = screen.getAllByPlaceholderText(/Ask a question/i);
    const input = inputs[0];
    const sendButtons = screen.getAllByRole('button', { name: /Send/i });
    const sendButton = sendButtons[0];

    fireEvent.change(input, { target: { value: 'Hello graph' } });
    fireEvent.click(sendButton);

    await waitFor(() => {
      // Check if user message was added
      // Use getAllByText in case of duplicates
      const messages = screen.getAllByText('Hello graph');
      expect(messages.length).toBeGreaterThan(0);
    });
  });
});
