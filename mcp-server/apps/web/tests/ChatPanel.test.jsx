import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ChatPanel from '../src/components/ChatPanel';
import useGraphStore from '../src/store/graphStore';

// Mock the API module
vi.mock('../src/services/api', () => ({
  sendChatMessage: vi.fn(),
  uploadFile: vi.fn(),
  executeTool: vi.fn(),
}));

import * as api from '../src/services/api';

describe('ChatPanel', () => {
  const mockOnClose = vi.fn();

  beforeEach(() => {
    // Reset store state before each test
    useGraphStore.setState({
      chatMessages: [],
      nodes: [],
      edges: [],
      isChatOpen: true,
    });
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('Rendering', () => {
    it('renders the chat panel with header', () => {
      render(<ChatPanel onClose={mockOnClose} />);

      expect(screen.getByText('Graph Assistant')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /close/i })).toBeInTheDocument();
    });

    it('shows welcome message when no messages', () => {
      render(<ChatPanel onClose={mockOnClose} />);

      expect(screen.getByText(/ask questions about the graph/i)).toBeInTheDocument();
      expect(screen.getByText(/examples:/i)).toBeInTheDocument();
    });

    it('renders input field and buttons', () => {
      render(<ChatPanel onClose={mockOnClose} />);

      expect(screen.getByPlaceholderText(/ask a question/i)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /upload/i })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /send/i })).toBeInTheDocument();
    });

    it('calls onClose when close button clicked', () => {
      render(<ChatPanel onClose={mockOnClose} />);

      fireEvent.click(screen.getByTitle(/close chat/i));
      expect(mockOnClose).toHaveBeenCalled();
    });
  });

  describe('Message sending', () => {
    it('disables send button when input is empty', () => {
      render(<ChatPanel onClose={mockOnClose} />);

      const sendButton = screen.getByRole('button', { name: /send/i });
      expect(sendButton).toBeDisabled();
    });

    it('enables send button when input has text', async () => {
      render(<ChatPanel onClose={mockOnClose} />);
      const user = userEvent.setup();

      const input = screen.getByPlaceholderText(/ask a question/i);
      await user.type(input, 'Hello');

      const sendButton = screen.getByRole('button', { name: /send/i });
      expect(sendButton).not.toBeDisabled();
    });

    it('sends message and displays response', async () => {
      api.sendChatMessage.mockResolvedValueOnce({
        content: 'I found 3 nodes.',
        toolUsed: 'search_graph',
        toolResult: { nodes: [], edges: [], total: 3 },
      });

      render(<ChatPanel onClose={mockOnClose} />);
      const user = userEvent.setup();

      const input = screen.getByPlaceholderText(/ask a question/i);
      await user.type(input, 'Search for AI');
      await user.click(screen.getByRole('button', { name: /send/i }));

      // User message should appear
      await waitFor(() => {
        expect(screen.getByText('Search for AI')).toBeInTheDocument();
      });

      // Assistant response should appear
      await waitFor(() => {
        expect(screen.getByText('I found 3 nodes.')).toBeInTheDocument();
      });
    });

    it('shows loading state while processing', async () => {
      // Make API call hang
      api.sendChatMessage.mockImplementationOnce(() => new Promise(() => {}));

      render(<ChatPanel onClose={mockOnClose} />);
      const user = userEvent.setup();

      const input = screen.getByPlaceholderText(/ask a question/i);
      await user.type(input, 'Search');
      await user.click(screen.getByRole('button', { name: /send/i }));

      await waitFor(() => {
        expect(screen.getByText(/processing/i)).toBeInTheDocument();
      });
    });

    it('displays error message on API failure', async () => {
      api.sendChatMessage.mockRejectedValueOnce(new Error('Network error'));

      render(<ChatPanel onClose={mockOnClose} />);
      const user = userEvent.setup();

      const input = screen.getByPlaceholderText(/ask a question/i);
      await user.type(input, 'Search');
      await user.click(screen.getByRole('button', { name: /send/i }));

      await waitFor(() => {
        expect(screen.getByText(/error: network error/i)).toBeInTheDocument();
      });
    });

    it('clears input after sending', async () => {
      api.sendChatMessage.mockResolvedValueOnce({
        content: 'Response',
        toolUsed: null,
        toolResult: null,
      });

      render(<ChatPanel onClose={mockOnClose} />);
      const user = userEvent.setup();

      const input = screen.getByPlaceholderText(/ask a question/i);
      await user.type(input, 'Test message');
      await user.click(screen.getByRole('button', { name: /send/i }));

      await waitFor(() => {
        expect(input).toHaveValue('');
      });
    });
  });

  describe('Node proposals', () => {
    it('displays proposal card when LLM proposes a node', async () => {
      api.sendChatMessage.mockResolvedValueOnce({
        content: 'I suggest adding this node.',
        toolUsed: 'propose_new_node',
        toolResult: {
          proposed_node: {
            type: 'Initiative',
            name: 'AI Strategy Project',
            description: 'A new AI initiative',
          },
          similar_nodes: [],
        },
      });

      render(<ChatPanel onClose={mockOnClose} />);
      const user = userEvent.setup();

      await user.type(screen.getByPlaceholderText(/ask a question/i), 'Add a node');
      await user.click(screen.getByRole('button', { name: /send/i }));

      await waitFor(() => {
        expect(screen.getByText(/proposed addition/i)).toBeInTheDocument();
        expect(screen.getByText('AI Strategy Project')).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /approve/i })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /reject/i })).toBeInTheDocument();
      });
    });

    it('shows similar nodes warning when duplicates found', async () => {
      api.sendChatMessage.mockResolvedValueOnce({
        content: 'I suggest adding this node.',
        toolUsed: 'propose_new_node',
        toolResult: {
          proposed_node: {
            type: 'Initiative',
            name: 'AI Project',
            description: 'An AI initiative',
          },
          similar_nodes: [
            { name: 'AI Strategy', similarity_score: 0.85 },
            { name: 'AI Initiative', similarity_score: 0.75 },
          ],
        },
      });

      render(<ChatPanel onClose={mockOnClose} />);
      const user = userEvent.setup();

      await user.type(screen.getByPlaceholderText(/ask a question/i), 'Add AI node');
      await user.click(screen.getByRole('button', { name: /send/i }));

      await waitFor(() => {
        expect(screen.getByText(/similar nodes found/i)).toBeInTheDocument();
        expect(screen.getByText(/AI Strategy.*85%/)).toBeInTheDocument();
      });
    });

    it('sends approval message when approve clicked', async () => {
      api.sendChatMessage
        .mockResolvedValueOnce({
          content: 'I suggest adding this node.',
          toolUsed: 'propose_new_node',
          toolResult: {
            proposed_node: {
              type: 'Initiative',
              name: 'Test Node',
              description: 'Test',
            },
            similar_nodes: [],
          },
        })
        .mockResolvedValueOnce({
          content: 'Node added successfully.',
          toolUsed: 'add_nodes',
          toolResult: { nodes: [{ id: '123', name: 'Test Node' }] },
        });

      render(<ChatPanel onClose={mockOnClose} />);
      const user = userEvent.setup();

      await user.type(screen.getByPlaceholderText(/ask a question/i), 'Add node');
      await user.click(screen.getByRole('button', { name: /send/i }));

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /approve/i })).toBeInTheDocument();
      });

      await user.click(screen.getByRole('button', { name: /approve/i }));

      await waitFor(() => {
        expect(screen.getByText(/yes, add the node/i)).toBeInTheDocument();
      });
    });
  });

  describe('Delete confirmations', () => {
    it('displays delete confirmation card', async () => {
      api.sendChatMessage.mockResolvedValueOnce({
        content: 'Are you sure you want to delete?',
        toolUsed: 'delete_nodes',
        toolResult: {
          requires_confirmation: true,
          nodes_to_delete: [
            { id: '1', name: 'Node 1', type: 'Actor' },
            { id: '2', name: 'Node 2', type: 'Initiative' },
          ],
          affected_edges: [{ id: 'e1' }],
        },
      });

      render(<ChatPanel onClose={mockOnClose} />);
      const user = userEvent.setup();

      await user.type(screen.getByPlaceholderText(/ask a question/i), 'Delete nodes');
      await user.click(screen.getByRole('button', { name: /send/i }));

      await waitFor(() => {
        expect(screen.getByText(/confirm deletion/i)).toBeInTheDocument();
        expect(screen.getByText(/Node 1/)).toBeInTheDocument();
        expect(screen.getByText(/Node 2/)).toBeInTheDocument();
        expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument();
      });
    });
  });

  describe('File upload', () => {
    it('shows file indicator when file uploaded', async () => {
      api.uploadFile.mockResolvedValueOnce({
        success: true,
        filename: 'document.pdf',
        text: 'Sample text content from PDF',
      });

      render(<ChatPanel onClose={mockOnClose} />);

      // Find the hidden file input
      const fileInput = document.querySelector('input[type="file"]');
      const file = new File(['test content'], 'document.pdf', { type: 'application/pdf' });

      // Trigger file upload
      fireEvent.change(fileInput, { target: { files: [file] } });

      await waitFor(() => {
        expect(screen.getByText('document.pdf')).toBeInTheDocument();
      });
    });

    it('removes file when remove button clicked', async () => {
      api.uploadFile.mockResolvedValueOnce({
        success: true,
        filename: 'test.txt',
        text: 'Content',
      });

      render(<ChatPanel onClose={mockOnClose} />);

      const fileInput = document.querySelector('input[type="file"]');
      const file = new File(['test'], 'test.txt', { type: 'text/plain' });
      fireEvent.change(fileInput, { target: { files: [file] } });

      await waitFor(() => {
        expect(screen.getByText('test.txt')).toBeInTheDocument();
      });

      // Click remove button
      fireEvent.click(screen.getByTitle(/remove file/i));

      await waitFor(() => {
        expect(screen.queryByText('test.txt')).not.toBeInTheDocument();
      });
    });

    it('includes file content in message', async () => {
      api.uploadFile.mockResolvedValueOnce({
        success: true,
        filename: 'report.pdf',
        text: 'Report content here',
      });
      api.sendChatMessage.mockResolvedValueOnce({
        content: 'Analyzed the document.',
        toolUsed: null,
        toolResult: null,
      });

      render(<ChatPanel onClose={mockOnClose} />);
      const user = userEvent.setup();

      // Upload file
      const fileInput = document.querySelector('input[type="file"]');
      const file = new File(['test'], 'report.pdf', { type: 'application/pdf' });
      fireEvent.change(fileInput, { target: { files: [file] } });

      await waitFor(() => {
        expect(screen.getByText('report.pdf')).toBeInTheDocument();
      });

      // Type message and send
      await user.type(screen.getByPlaceholderText(/describe what to do/i), 'Analyze this');
      await user.click(screen.getByRole('button', { name: /send/i }));

      await waitFor(() => {
        // Message should include file reference
        const messages = api.sendChatMessage.mock.calls[0][0];
        const lastMessage = messages[messages.length - 1];
        expect(lastMessage.content).toContain('report.pdf');
        expect(lastMessage.content).toContain('Report content here');
      });
    });

    it('shows error when upload fails', async () => {
      api.uploadFile.mockRejectedValueOnce(new Error('Upload failed'));

      render(<ChatPanel onClose={mockOnClose} />);

      const fileInput = document.querySelector('input[type="file"]');
      const file = new File(['test'], 'bad.pdf', { type: 'application/pdf' });
      fireEvent.change(fileInput, { target: { files: [file] } });

      await waitFor(() => {
        expect(screen.getByText(/upload failed/i)).toBeInTheDocument();
      });
    });
  });

  describe('Keyboard interactions', () => {
    it('sends message on Enter key (without Shift)', async () => {
      api.sendChatMessage.mockResolvedValueOnce({
        content: 'Response',
        toolUsed: null,
        toolResult: null,
      });

      render(<ChatPanel onClose={mockOnClose} />);
      const user = userEvent.setup();

      const input = screen.getByPlaceholderText(/ask a question/i);
      await user.type(input, 'Test{Enter}');

      await waitFor(() => {
        expect(api.sendChatMessage).toHaveBeenCalled();
      });
    });

    it('does not send on Shift+Enter (allows newline)', async () => {
      render(<ChatPanel onClose={mockOnClose} />);
      const user = userEvent.setup();

      const input = screen.getByPlaceholderText(/ask a question/i);
      await user.type(input, 'Line 1{Shift>}{Enter}{/Shift}Line 2');

      expect(api.sendChatMessage).not.toHaveBeenCalled();
    });
  });

  describe('Message display', () => {
    it('displays user messages on the right', () => {
      useGraphStore.setState({
        chatMessages: [
          { id: 1, role: 'user', content: 'Hello', timestamp: new Date() },
        ],
      });

      render(<ChatPanel onClose={mockOnClose} />);

      const message = screen.getByText('Hello').closest('.chat-message');
      expect(message).toHaveClass('user');
    });

    it('displays assistant messages on the left', () => {
      useGraphStore.setState({
        chatMessages: [
          { id: 1, role: 'assistant', content: 'Hi there', timestamp: new Date() },
        ],
      });

      render(<ChatPanel onClose={mockOnClose} />);

      const message = screen.getByText('Hi there').closest('.chat-message');
      expect(message).toHaveClass('assistant');
    });

    it('formats timestamps correctly', () => {
      const testDate = new Date('2024-01-15T10:30:00');
      useGraphStore.setState({
        chatMessages: [
          { id: 1, role: 'user', content: 'Test', timestamp: testDate },
        ],
      });

      render(<ChatPanel onClose={mockOnClose} />);

      expect(screen.getByText(/10:30/)).toBeInTheDocument();
    });
  });
});
