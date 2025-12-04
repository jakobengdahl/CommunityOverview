/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ChatPanel from './ChatPanel';
import useGraphStore from '../store/graphStore';

// Mock dependencies
vi.mock('../utils/mcpClient', () => ({
  mcpClient: {
    connect: vi.fn().mockResolvedValue(),
    callTool: vi.fn().mockResolvedValue("Mock tool result"),
    serverUrl: "http://mock-server"
  }
}));

vi.mock('../utils/claude', () => ({
  claudeService: {
    setApiKey: vi.fn(),
    sendMessage: vi.fn().mockResolvedValue({
      content: [{ type: 'text', text: 'Mock Claude response' }]
    })
  }
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
      highlightNodes: vi.fn()
    });
  });

  it('renders input field and send button', () => {
    render(<ChatPanel />);
    expect(screen.getByPlaceholderText(/Ask a question/i)).toBeDefined();
    expect(screen.getByRole('button', { name: /Send/i })).toBeDefined();
  });

  it('handles user input and submission', async () => {
    render(<ChatPanel />);

    const input = screen.getByPlaceholderText(/Ask a question/i);
    const sendButton = screen.getByRole('button', { name: /Send/i });

    fireEvent.change(input, { target: { value: 'Hello graph' } });
    fireEvent.click(sendButton);

    await waitFor(() => {
      // Check if user message was added
      expect(screen.getByText('Hello graph')).toBeDefined();
    });
  });
});
