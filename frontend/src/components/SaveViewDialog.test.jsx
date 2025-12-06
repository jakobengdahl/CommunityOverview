/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import SaveViewDialog from './SaveViewDialog';
import useGraphStore from '../store/graphStore';

// Mock API
vi.mock('../services/api', () => ({
  executeTool: vi.fn().mockResolvedValue({ success: true })
}));

describe('SaveViewDialog', () => {
  const mockOnClose = vi.fn();
  const mockOnSave = vi.fn();

  beforeEach(() => {
    useGraphStore.setState({
      nodes: [
        { id: '1', type: 'Actor', name: 'Test Actor', communities: ['eSam'] }
      ],
      edges: [
        { id: 'e1', source: '1', target: '2', type: 'BELONGS_TO' }
      ],
      hiddenNodeIds: []
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('does not render when isOpen is false', () => {
    render(<SaveViewDialog isOpen={false} onClose={mockOnClose} onSave={mockOnSave} />);
    expect(screen.queryByText('Save Current View')).toBeNull();
  });

  it('renders when isOpen is true', () => {
    render(<SaveViewDialog isOpen={true} onClose={mockOnClose} onSave={mockOnSave} />);
    expect(screen.getByText('Save Current View')).toBeDefined();
  });

  it('displays input field for view name', () => {
    render(<SaveViewDialog isOpen={true} onClose={mockOnClose} onSave={mockOnSave} />);
    expect(screen.getByPlaceholderText(/Enter view name/i)).toBeDefined();
  });

  it('displays save and cancel buttons', () => {
    render(<SaveViewDialog isOpen={true} onClose={mockOnClose} onSave={mockOnSave} />);
    expect(screen.getByRole('button', { name: /Save View/i })).toBeDefined();
    expect(screen.getByRole('button', { name: /Cancel/i })).toBeDefined();
  });

  it('calls onClose when cancel button is clicked', () => {
    render(<SaveViewDialog isOpen={true} onClose={mockOnClose} onSave={mockOnSave} />);

    const cancelButton = screen.getByRole('button', { name: /Cancel/i });
    fireEvent.click(cancelButton);

    expect(mockOnClose).toHaveBeenCalled();
  });

  it('disables save button when view name is empty', () => {
    render(<SaveViewDialog isOpen={true} onClose={mockOnClose} onSave={mockOnSave} />);

    const saveButton = screen.getByRole('button', { name: /Save View/i });
    expect(saveButton.disabled).toBe(true);
  });

  it('enables save button when view name is entered', () => {
    render(<SaveViewDialog isOpen={true} onClose={mockOnClose} onSave={mockOnSave} />);

    const input = screen.getByPlaceholderText(/Enter view name/i);
    fireEvent.change(input, { target: { value: 'My Test View' } });

    const saveButton = screen.getByRole('button', { name: /Save View/i });
    expect(saveButton.disabled).toBe(false);
  });

  it('calls onSave with view name when save button is clicked', async () => {
    render(<SaveViewDialog isOpen={true} onClose={mockOnClose} onSave={mockOnSave} />);

    const input = screen.getByPlaceholderText(/Enter view name/i);
    fireEvent.change(input, { target: { value: 'My Test View' } });

    const saveButton = screen.getByRole('button', { name: /Save View/i });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockOnSave).toHaveBeenCalledWith('My Test View');
    });
  });

  it('calls onClose after successful save', async () => {
    render(<SaveViewDialog isOpen={true} onClose={mockOnClose} onSave={mockOnSave} />);

    const input = screen.getByPlaceholderText(/Enter view name/i);
    fireEvent.change(input, { target: { value: 'My Test View' } });

    const saveButton = screen.getByRole('button', { name: /Save View/i });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockOnClose).toHaveBeenCalled();
    });
  });

  it('shows saving state while save is in progress', async () => {
    mockOnSave.mockImplementation(() => new Promise(resolve => setTimeout(resolve, 100)));

    render(<SaveViewDialog isOpen={true} onClose={mockOnClose} onSave={mockOnSave} />);

    const input = screen.getByPlaceholderText(/Enter view name/i);
    fireEvent.change(input, { target: { value: 'My Test View' } });

    const saveButton = screen.getByRole('button', { name: /Save View/i });
    fireEvent.click(saveButton);

    // Check for "Saving..." text
    await waitFor(() => {
      expect(screen.getByText('Saving...')).toBeDefined();
    });
  });

  it('displays error message when save fails', async () => {
    mockOnSave.mockRejectedValueOnce(new Error('Save failed'));

    render(<SaveViewDialog isOpen={true} onClose={mockOnClose} onSave={mockOnSave} />);

    const input = screen.getByPlaceholderText(/Enter view name/i);
    fireEvent.change(input, { target: { value: 'My Test View' } });

    const saveButton = screen.getByRole('button', { name: /Save View/i });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(screen.getByText(/Save failed/i)).toBeDefined();
    });
  });

  it('clears input after successful save', async () => {
    render(<SaveViewDialog isOpen={true} onClose={mockOnClose} onSave={mockOnSave} />);

    const input = screen.getByPlaceholderText(/Enter view name/i);
    fireEvent.change(input, { target: { value: 'My Test View' } });

    const saveButton = screen.getByRole('button', { name: /Save View/i });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(input.value).toBe('');
    });
  });
});
