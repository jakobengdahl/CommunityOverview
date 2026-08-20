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
  beforeEach(() => {
    // Reset store state before each test
    useGraphStore.setState({
      chatMessages: [],
      nodes: [],
      edges: [],
      chatPanelOpen: true,
    });
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('Rendering', () => {
    it('renders the chat panel with header', () => {
      render(<ChatPanel />);

      expect(screen.getByText('Graph assistant')).toBeInTheDocument();
    });

    it('renders input field and buttons', () => {
      render(<ChatPanel />);

      expect(screen.getByPlaceholderText(/question|fråga|action|åtgärd/i)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /upload|ladda upp/i })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /send|skicka/i })).toBeInTheDocument();
    });

    it('shows minimized bar when panel is closed', () => {
      useGraphStore.setState({ chatPanelOpen: false });
      render(<ChatPanel />);

      expect(screen.getByText('Graph assistant')).toBeInTheDocument();
      // Should not have the full chat input
      expect(
        screen.queryByPlaceholderText(/question|fråga|action|åtgärd/i)
      ).not.toBeInTheDocument();
    });

    it('toggles between open and minimized when collapse button clicked', () => {
      render(<ChatPanel />);

      // Click collapse/minimize button
      fireEvent.click(screen.getByTitle('Minimize'));

      // Now should be in minimized state
      expect(
        screen.queryByPlaceholderText(/question|fråga|action|åtgärd/i)
      ).not.toBeInTheDocument();
      expect(localStorage.getItem('community-graph:ui:ai-assistant-collapsed')).toBe('true');
    });
  });

  describe('Message sending', () => {
    it('disables send button when input is empty', () => {
      render(<ChatPanel />);

      const sendButton = screen.getByRole('button', { name: /send|skicka/i });
      expect(sendButton).toBeDisabled();
    });

    it('enables send button when input has text', async () => {
      render(<ChatPanel />);
      const user = userEvent.setup();

      const input = screen.getByPlaceholderText(/question|fråga|action|åtgärd/i);
      await user.type(input, 'Hello');

      const sendButton = screen.getByRole('button', { name: /send|skicka/i });
      expect(sendButton).not.toBeDisabled();
    });

    it('sends message and displays response', async () => {
      api.sendChatMessage.mockResolvedValueOnce({
        content: 'I found 3 nodes.',
        toolUsed: 'search_graph',
        toolResult: { nodes: [], edges: [], total: 3 },
      });

      render(<ChatPanel />);
      const user = userEvent.setup();

      const input = screen.getByPlaceholderText(/question|fråga|action|åtgärd/i);
      await user.type(input, 'Search for AI');
      await user.click(screen.getByRole('button', { name: /send|skicka/i }));

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

      render(<ChatPanel />);
      const user = userEvent.setup();

      const input = screen.getByPlaceholderText(/question|fråga|action|åtgärd/i);
      await user.type(input, 'Search');
      await user.click(screen.getByRole('button', { name: /send|skicka/i }));

      // Check that the send button shows a processing state
      await waitFor(() => {
        const sendButton = screen.getByRole('button', { name: /processing|bearbetar/i });
        expect(sendButton).toBeDisabled();
      });
    });

    it('displays error message on API failure', async () => {
      api.sendChatMessage.mockRejectedValueOnce(new Error('Network error'));

      render(<ChatPanel />);
      const user = userEvent.setup();

      const input = screen.getByPlaceholderText(/question|fråga|action|åtgärd/i);
      await user.type(input, 'Search');
      await user.click(screen.getByRole('button', { name: /send|skicka/i }));

      await waitFor(() => {
        expect(screen.getByText(/fel: network error|error: network error/i)).toBeInTheDocument();
      });
    });

    it('clears input after sending', async () => {
      api.sendChatMessage.mockResolvedValueOnce({
        content: 'Response',
        toolUsed: null,
        toolResult: null,
      });

      render(<ChatPanel />);
      const user = userEvent.setup();

      const input = screen.getByPlaceholderText(/question|fråga|action|åtgärd/i);
      await user.type(input, 'Test message');
      await user.click(screen.getByRole('button', { name: /send|skicka/i }));

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

      render(<ChatPanel />);
      const user = userEvent.setup();

      await user.type(screen.getByPlaceholderText(/question|fråga|action|åtgärd/i), 'Add a node');
      await user.click(screen.getByRole('button', { name: /send|skicka/i }));

      await waitFor(() => {
        expect(screen.getByText(/proposed addition|föreslaget tillägg/i)).toBeInTheDocument();
        expect(screen.getByText('AI Strategy Project')).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /approve|godkänn/i })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /reject|avvisa/i })).toBeInTheDocument();
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

      render(<ChatPanel />);
      const user = userEvent.setup();

      await user.type(screen.getByPlaceholderText(/question|fråga|action|åtgärd/i), 'Add AI node');
      await user.click(screen.getByRole('button', { name: /send|skicka/i }));

      await waitFor(() => {
        expect(
          screen.getByText(/similar nodes found|liknande noder hittades/i)
        ).toBeInTheDocument();
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

      render(<ChatPanel />);
      const user = userEvent.setup();

      await user.type(screen.getByPlaceholderText(/question|fråga|action|åtgärd/i), 'Add node');
      await user.click(screen.getByRole('button', { name: /send|skicka/i }));

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /approve|godkänn/i })).toBeInTheDocument();
      });

      await user.click(screen.getByRole('button', { name: /approve|godkänn/i }));

      await waitFor(() => {
        expect(screen.getByText(/yes, add the node|ja, lägg till noden/i)).toBeInTheDocument();
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

      render(<ChatPanel />);
      const user = userEvent.setup();

      await user.type(screen.getByPlaceholderText(/question|fråga|action|åtgärd/i), 'Delete nodes');
      await user.click(screen.getByRole('button', { name: /send|skicka/i }));

      await waitFor(() => {
        expect(
          screen.getAllByText(/confirm deletion|bekräfta borttagning/i).length
        ).toBeGreaterThan(0);
        expect(screen.getByText(/Node 1/)).toBeInTheDocument();
        expect(screen.getByText(/Node 2/)).toBeInTheDocument();
        expect(screen.getByText(/cannot be undone|kan inte ångras/i)).toBeInTheDocument();
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

      render(<ChatPanel />);

      const fileInput = document.querySelector('input[type="file"]');
      const file = new File(['test content'], 'document.pdf', { type: 'application/pdf' });
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

      render(<ChatPanel />);

      const fileInput = document.querySelector('input[type="file"]');
      const file = new File(['test'], 'test.txt', { type: 'text/plain' });
      fireEvent.change(fileInput, { target: { files: [file] } });

      await waitFor(() => {
        expect(screen.getByText('test.txt')).toBeInTheDocument();
      });

      fireEvent.click(screen.getByTitle(/remove file|ta bort fil/i));

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

      render(<ChatPanel />);
      const user = userEvent.setup();

      const fileInput = document.querySelector('input[type="file"]');
      const file = new File(['test'], 'report.pdf', { type: 'application/pdf' });
      fireEvent.change(fileInput, { target: { files: [file] } });

      await waitFor(() => {
        expect(screen.getByText('report.pdf')).toBeInTheDocument();
      });

      await user.type(
        screen.getByPlaceholderText(/describe what you want to do|beskriv vad du vill göra/i),
        'Analyze this'
      );
      await user.click(screen.getByRole('button', { name: /send|skicka/i }));

      await waitFor(() => {
        const messages = api.sendChatMessage.mock.calls[0][0];
        const lastMessage = messages[messages.length - 1];
        expect(lastMessage.content).toContain('report.pdf');
        expect(lastMessage.content).toContain('Report content here');
      });
    });

    it('shows error when upload fails', async () => {
      api.uploadFile.mockRejectedValueOnce(new Error('Upload failed'));

      render(<ChatPanel />);

      const fileInput = document.querySelector('input[type="file"]');
      const file = new File(['test'], 'bad.pdf', { type: 'application/pdf' });
      fireEvent.change(fileInput, { target: { files: [file] } });

      await waitFor(() => {
        expect(screen.getByText(/Upload failed/i)).toBeInTheDocument();
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

      render(<ChatPanel />);
      const user = userEvent.setup();

      const input = screen.getByPlaceholderText(/question|fråga|action|åtgärd/i);
      await user.type(input, 'Test{Enter}');

      await waitFor(() => {
        expect(api.sendChatMessage).toHaveBeenCalled();
      });
    });

    it('does not send on Shift+Enter (allows newline)', async () => {
      render(<ChatPanel />);
      const user = userEvent.setup();

      const input = screen.getByPlaceholderText(/question|fråga|action|åtgärd/i);
      await user.type(input, 'Line 1{Shift>}{Enter}{/Shift}Line 2');

      expect(api.sendChatMessage).not.toHaveBeenCalled();
    });
  });

  describe('Message display', () => {
    it('displays user messages on the right', () => {
      useGraphStore.setState({
        chatMessages: [{ id: 1, role: 'user', content: 'Hello', timestamp: new Date() }],
        chatPanelOpen: true,
      });

      render(<ChatPanel />);

      const message = screen.getByText('Hello').closest('.chat-message');
      expect(message).toHaveClass('user');
    });

    it('displays assistant messages on the left', () => {
      useGraphStore.setState({
        chatMessages: [{ id: 1, role: 'assistant', content: 'Hi there', timestamp: new Date() }],
        chatPanelOpen: true,
      });

      render(<ChatPanel />);

      const message = screen.getByText('Hi there').closest('.chat-message');
      expect(message).toHaveClass('assistant');
    });

    it('formats timestamps correctly', () => {
      const testDate = new Date('2024-01-15T10:30:00');
      useGraphStore.setState({
        chatMessages: [{ id: 1, role: 'user', content: 'Test', timestamp: testDate }],
        chatPanelOpen: true,
      });

      render(<ChatPanel />);

      expect(screen.getByText(/10:30/)).toBeInTheDocument();
    });
  });

  describe('extra_actions alongside present_form', () => {
    it('executes mark_nodes from extra_actions while the form still renders', async () => {
      const setNodeMarksSpy = vi.fn();
      useGraphStore.setState({ setNodeMarks: setNodeMarksSpy });

      api.sendChatMessage.mockResolvedValueOnce({
        content: 'Please fill in the form.',
        toolUsed: 'present_form',
        toolResult: {
          action: 'present_form',
          form: {
            fields: [{ id: 'role', label: 'Role', type: 'radio', options: ['A', 'B'] }],
          },
          extra_actions: [
            { action: 'mark_nodes', marks: [{ node_id: 'n1', color: '#EF4444', label: 'High' }] },
          ],
        },
      });

      render(<ChatPanel />);
      const user = userEvent.setup();

      await user.type(screen.getByPlaceholderText(/question|fråga|action|åtgärd/i), 'go');
      await user.click(screen.getByRole('button', { name: /send|skicka/i }));

      await waitFor(() => {
        // Form must still render
        expect(screen.getByText('Role')).toBeInTheDocument();
        // mark_nodes side-effect must have run
        expect(setNodeMarksSpy).toHaveBeenCalledWith([
          { node_id: 'n1', color: '#EF4444', label: 'High' },
        ]);
      });
    });

    it('executes clear_visualization from extra_actions while the form still renders', async () => {
      const clearSpy = vi.fn();
      useGraphStore.setState({
        nodes: [{ id: 'n1' }],
        clearVisualization: clearSpy,
      });

      api.sendChatMessage.mockResolvedValueOnce({
        content: 'Canvas cleared. Please answer:',
        toolUsed: 'present_form',
        toolResult: {
          action: 'present_form',
          form: {
            fields: [{ id: 'q', label: 'Q', type: 'text' }],
          },
          extra_actions: [{ action: 'clear_visualization', success: true }],
        },
      });

      render(<ChatPanel />);
      const user = userEvent.setup();

      await user.type(screen.getByPlaceholderText(/question|fråga|action|åtgärd/i), 'start');
      await user.click(screen.getByRole('button', { name: /send|skicka/i }));

      await waitFor(() => {
        // Form must still render
        expect(screen.getByText('Q')).toBeInTheDocument();
        // clear_visualization side-effect must have run
        expect(clearSpy).toHaveBeenCalled();
      });
    });

    it('does not fail when extra_actions is absent (backward compatibility)', async () => {
      api.sendChatMessage.mockResolvedValueOnce({
        content: 'Marks applied.',
        toolUsed: 'mark_nodes',
        toolResult: { action: 'mark_nodes', marks: [] },
      });

      render(<ChatPanel />);
      const user = userEvent.setup();

      await user.type(screen.getByPlaceholderText(/question|fråga|action|åtgärd/i), 'mark');
      await user.click(screen.getByRole('button', { name: /send|skicka/i }));

      await waitFor(() => {
        expect(screen.getByText('Marks applied.')).toBeInTheDocument();
      });
    });
  });

  describe('Visualization intent (add vs replace)', () => {
    const ACTOR = { id: 'a1', type: 'Actor', name: 'SCB' };

    const setupSpies = () => {
      const addSpy = vi.fn();
      const updateSpy = vi.fn();
      const clearSpy = vi.fn();
      useGraphStore.setState({
        nodes: [{ id: 'existing', type: 'Goal', name: 'Existing' }],
        edges: [],
        addNodesToVisualization: addSpy,
        updateVisualization: updateSpy,
        clearVisualization: clearSpy,
      });
      return { addSpy, updateSpy, clearSpy };
    };

    const send = async (toolResult) => {
      api.sendChatMessage.mockResolvedValueOnce({
        content: 'Done.',
        toolUsed: 'search_graph',
        toolResult,
      });
      render(<ChatPanel />);
      const user = userEvent.setup();
      await user.type(screen.getByPlaceholderText(/question|fråga|action|åtgärd/i), 'go');
      await user.click(screen.getByRole('button', { name: /send|skicka/i }));
      await waitFor(() => expect(screen.getByText('Done.')).toBeInTheDocument());
    };

    it('add_to_visualization adds nodes and never clears the view', async () => {
      const { addSpy, updateSpy, clearSpy } = setupSpies();
      await send({ action: 'add_to_visualization', nodes: [ACTOR], edges: [] });
      expect(addSpy).toHaveBeenCalledTimes(1);
      expect(updateSpy).not.toHaveBeenCalled();
      expect(clearSpy).not.toHaveBeenCalled();
    });

    it('a plain additive request (no explicit action) adds and never clears', async () => {
      // Regression: a node-returning search with no explicit action must default
      // to additive placement, not silently replace the whole view.
      const { addSpy, updateSpy, clearSpy } = setupSpies();
      await send({ nodes: [ACTOR], edges: [] });
      expect(addSpy).toHaveBeenCalledTimes(1);
      expect(updateSpy).not.toHaveBeenCalled();
      expect(clearSpy).not.toHaveBeenCalled();
    });

    it('replace_visualization replaces the whole view', async () => {
      const { addSpy, updateSpy } = setupSpies();
      await send({ action: 'replace_visualization', nodes: [ACTOR], edges: [] });
      expect(updateSpy).toHaveBeenCalledTimes(1);
      expect(addSpy).not.toHaveBeenCalled();
    });

    it('clear-and-add (clear_visualization carrying nodes) clears then shows the nodes', async () => {
      // "clear the view and show X": the backend emits clear_visualization with
      // the searched nodes. The view must be cleared AND the nodes rendered.
      const { addSpy, updateSpy, clearSpy } = setupSpies();
      await send({ action: 'clear_visualization', nodes: [ACTOR], edges: [] });
      expect(clearSpy).toHaveBeenCalledTimes(1);
      expect(updateSpy).toHaveBeenCalledWith([ACTOR], []);
      expect(addSpy).not.toHaveBeenCalled();
    });

    it('an empty replace_visualization clears the view and renders nothing', async () => {
      const { updateSpy, clearSpy } = setupSpies();
      await send({ action: 'replace_visualization', nodes: [], edges: [] });
      expect(clearSpy).toHaveBeenCalledTimes(1);
      expect(updateSpy).not.toHaveBeenCalled();
    });

    it('update_in_visualization merges the edited node in place, never clears', async () => {
      const addSpy = vi.fn();
      const updateSpy = vi.fn();
      const clearSpy = vi.fn();
      useGraphStore.setState({
        nodes: [{ id: 'a1', type: 'Actor', name: 'Old name' }],
        edges: [],
        addNodesToVisualization: addSpy,
        updateVisualization: updateSpy,
        clearVisualization: clearSpy,
      });
      await send({
        action: 'update_in_visualization',
        nodes: [{ id: 'a1', type: 'Actor', name: 'New name' }],
        edges: [],
      });
      expect(updateSpy).toHaveBeenCalledTimes(1);
      const [nodesArg] = updateSpy.mock.calls[0];
      expect(nodesArg.map((n) => n.id)).toEqual(['a1']);
      expect(nodesArg.find((n) => n.id === 'a1').name).toBe('New name');
      expect(addSpy).not.toHaveBeenCalled();
      expect(clearSpy).not.toHaveBeenCalled();
    });
  });

  describe('Collection form', () => {
    it('renders an assistant present_form and submits structured answers', async () => {
      useGraphStore.setState({
        chatMessages: [
          {
            id: 'form-msg',
            role: 'assistant',
            content: 'Please answer:',
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
        ],
        chatPanelOpen: true,
      });

      api.sendChatMessage.mockResolvedValue({
        content: 'Saved, thank you.',
        toolUsed: 'save_collection_response',
        toolResult: null,
      });

      render(<ChatPanel collectionShortName="feedback" />);

      expect(screen.getByText('Q1')).toBeInTheDocument();
      fireEvent.click(screen.getByLabelText('Manager'));
      fireEvent.click(screen.getByRole('button', { name: 'Submit' }));

      await waitFor(() => expect(api.sendChatMessage).toHaveBeenCalledTimes(1));

      const history = api.sendChatMessage.mock.calls[0][0];
      const lastMsg = history[history.length - 1];
      expect(lastMsg.role).toBe('user');
      expect(lastMsg.content).toContain('save_collection_response');
      expect(lastMsg.content).toContain('"field_id":"role"');
      expect(lastMsg.content).toContain('Manager');
    });
  });
});
