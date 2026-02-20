import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SubtypeInput from '../src/components/SubtypeInput';

describe('SubtypeInput', () => {
  const mockOnChange = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('Rendering', () => {
    it('renders with default label', () => {
      render(<SubtypeInput value={[]} onChange={mockOnChange} />);
      expect(screen.getByText('Subtypes')).toBeInTheDocument();
    });

    it('renders with custom label', () => {
      render(<SubtypeInput value={[]} onChange={mockOnChange} label="Classification" />);
      expect(screen.getByText('Classification')).toBeInTheDocument();
    });

    it('renders existing subtypes as chips', () => {
      render(
        <SubtypeInput
          value={['Government agency', 'Municipality']}
          onChange={mockOnChange}
        />
      );
      expect(screen.getByText('Government agency')).toBeInTheDocument();
      expect(screen.getByText('Municipality')).toBeInTheDocument();
    });

    it('renders input with placeholder when empty', () => {
      render(<SubtypeInput value={[]} onChange={mockOnChange} />);
      expect(screen.getByPlaceholderText('Type to add...')).toBeInTheDocument();
    });

    it('hides placeholder when subtypes exist', () => {
      render(<SubtypeInput value={['Test']} onChange={mockOnChange} />);
      expect(screen.queryByPlaceholderText('Type to add...')).not.toBeInTheDocument();
    });
  });

  describe('Adding subtypes', () => {
    it('adds subtype on Enter key', async () => {
      const user = userEvent.setup();
      render(
        <SubtypeInput value={[]} onChange={mockOnChange} existingSubtypes={[]} />
      );

      const input = screen.getByRole('textbox');
      await user.type(input, 'Government agency');
      await user.keyboard('{Enter}');

      expect(mockOnChange).toHaveBeenCalledWith(['Government agency']);
    });

    it('adds subtype on comma', async () => {
      const user = userEvent.setup();
      render(
        <SubtypeInput value={[]} onChange={mockOnChange} existingSubtypes={[]} />
      );

      const input = screen.getByRole('textbox');
      await user.type(input, 'Municipality');
      await user.keyboard(',');

      expect(mockOnChange).toHaveBeenCalledWith(['Municipality']);
    });

    it('does not add empty subtype', async () => {
      const user = userEvent.setup();
      render(
        <SubtypeInput value={[]} onChange={mockOnChange} existingSubtypes={[]} />
      );

      const input = screen.getByRole('textbox');
      await user.keyboard('{Enter}');

      expect(mockOnChange).not.toHaveBeenCalled();
    });

    it('does not add duplicate subtype', async () => {
      const user = userEvent.setup();
      render(
        <SubtypeInput
          value={['Government agency']}
          onChange={mockOnChange}
          existingSubtypes={[]}
        />
      );

      const input = screen.getByRole('textbox');
      await user.type(input, 'Government agency');
      await user.keyboard('{Enter}');

      expect(mockOnChange).not.toHaveBeenCalled();
    });
  });

  describe('Case normalization', () => {
    it('normalizes case to match existing subtype', async () => {
      const user = userEvent.setup();
      render(
        <SubtypeInput
          value={[]}
          onChange={mockOnChange}
          existingSubtypes={['Government agency', 'Municipality']}
        />
      );

      const input = screen.getByRole('textbox');
      await user.type(input, 'government agency');
      await user.keyboard('{Enter}');

      // Should use the existing casing
      expect(mockOnChange).toHaveBeenCalledWith(['Government agency']);
    });
  });

  describe('Removing subtypes', () => {
    it('removes subtype when clicking remove button', async () => {
      const user = userEvent.setup();
      render(
        <SubtypeInput
          value={['Government agency', 'Municipality']}
          onChange={mockOnChange}
        />
      );

      const removeButtons = screen.getAllByText('×');
      await user.click(removeButtons[0]);

      expect(mockOnChange).toHaveBeenCalledWith(['Municipality']);
    });

    it('removes last subtype on Backspace when input is empty', async () => {
      const user = userEvent.setup();
      render(
        <SubtypeInput
          value={['Government agency', 'Municipality']}
          onChange={mockOnChange}
        />
      );

      const input = screen.getByRole('textbox');
      await user.click(input);
      await user.keyboard('{Backspace}');

      expect(mockOnChange).toHaveBeenCalledWith(['Government agency']);
    });
  });

  describe('Suggestions', () => {
    it('shows suggestions on focus', async () => {
      const user = userEvent.setup();
      render(
        <SubtypeInput
          value={[]}
          onChange={mockOnChange}
          existingSubtypes={['Government agency', 'Municipality']}
        />
      );

      const input = screen.getByRole('textbox');
      await user.click(input);

      expect(screen.getByText('Government agency')).toBeInTheDocument();
      expect(screen.getByText('Municipality')).toBeInTheDocument();
    });

    it('filters suggestions based on input', async () => {
      const user = userEvent.setup();
      render(
        <SubtypeInput
          value={[]}
          onChange={mockOnChange}
          existingSubtypes={['Government agency', 'Municipality', 'Steering group']}
        />
      );

      const input = screen.getByRole('textbox');
      await user.type(input, 'gov');

      expect(screen.getByText('Government agency')).toBeInTheDocument();
      expect(screen.queryByText('Municipality')).not.toBeInTheDocument();
      expect(screen.queryByText('Steering group')).not.toBeInTheDocument();
    });

    it('excludes already-selected subtypes from suggestions', async () => {
      const user = userEvent.setup();
      render(
        <SubtypeInput
          value={['Government agency']}
          onChange={mockOnChange}
          existingSubtypes={['Government agency', 'Municipality']}
        />
      );

      const input = screen.getByRole('textbox');
      await user.click(input);

      expect(screen.queryByRole('listitem', { name: 'Government agency' })).not.toBeInTheDocument();
      // Municipality should be shown as suggestion (in the list)
      const listItems = screen.getAllByRole('listitem');
      expect(listItems).toHaveLength(1);
      expect(listItems[0]).toHaveTextContent('Municipality');
    });

    it('selects suggestion with Enter', async () => {
      const user = userEvent.setup();
      render(
        <SubtypeInput
          value={[]}
          onChange={mockOnChange}
          existingSubtypes={['Government agency', 'Municipality']}
        />
      );

      const input = screen.getByRole('textbox');
      await user.click(input);
      await user.keyboard('{Enter}');

      expect(mockOnChange).toHaveBeenCalledWith(['Government agency']);
    });

    it('navigates suggestions with arrow keys', async () => {
      const user = userEvent.setup();
      render(
        <SubtypeInput
          value={[]}
          onChange={mockOnChange}
          existingSubtypes={['Government agency', 'Municipality']}
        />
      );

      const input = screen.getByRole('textbox');
      await user.click(input);
      await user.keyboard('{ArrowDown}');
      await user.keyboard('{Enter}');

      expect(mockOnChange).toHaveBeenCalledWith(['Municipality']);
    });
  });
});
