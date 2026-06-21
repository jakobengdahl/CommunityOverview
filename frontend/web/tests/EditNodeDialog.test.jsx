import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import EditNodeDialog from '../src/components/EditNodeDialog';
import useGraphStore from '../src/store/graphStore';

describe('EditNodeDialog', () => {
  const mockOnClose = vi.fn();
  const mockOnSave = vi.fn();

  const mockNode = {
    id: 'test-node-1',
    data: {
      name: 'Test Node',
      type: 'Actor',
      description: 'A test node description',
      summary: 'Test summary',
      tags: ['tag1', 'tag2'],
    },
  };

  beforeEach(() => {
    // Reset store with default state (no schema loaded)
    useGraphStore.setState({
      schema: null,
      presentation: null,
      configLoaded: false,
    });
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('Rendering', () => {
    it('renders the dialog with correct title', () => {
      render(
        <EditNodeDialog
          node={mockNode}
          onClose={mockOnClose}
          onSave={mockOnSave}
        />
      );

      expect(screen.getByText('Edit Actor')).toBeInTheDocument();
    });

    it('populates form with node data', () => {
      render(
        <EditNodeDialog
          node={mockNode}
          onClose={mockOnClose}
          onSave={mockOnSave}
        />
      );

      expect(screen.getByDisplayValue('Test Node')).toBeInTheDocument();
      expect(screen.getByDisplayValue('A test node description')).toBeInTheDocument();
      expect(screen.getByDisplayValue('Test summary')).toBeInTheDocument();
      expect(screen.getByDisplayValue('tag1, tag2')).toBeInTheDocument();
    });

    it('shows default node types when schema not loaded', () => {
      render(
        <EditNodeDialog
          node={mockNode}
          onClose={mockOnClose}
          onSave={mockOnSave}
        />
      );

      const typeSelect = screen.getByLabelText('Type');

      // Default types should be present
      expect(typeSelect).toContainHTML('Actor');
      expect(typeSelect).toContainHTML('Initiative');
      expect(typeSelect).toContainHTML('Community');
      expect(typeSelect).toContainHTML('Resource');
    });
  });

  describe('Dynamic node types from schema', () => {
    it('displays node types from schema when loaded', () => {
      // Set up schema with custom node types
      useGraphStore.setState({
        schema: {
          node_types: {
            CustomType: {
              fields: ['name', 'description'],
              description: 'A custom type',
              color: '#FF0000',
              static: false,
            },
            AnotherType: {
              fields: ['name'],
              description: 'Another type',
              color: '#00FF00',
              static: false,
            },
            SavedView: {
              fields: ['name'],
              description: 'Saved views',
              color: '#6B7280',
              static: true,
            },
          },
        },
        configLoaded: true,
      });

      render(
        <EditNodeDialog
          node={mockNode}
          onClose={mockOnClose}
          onSave={mockOnSave}
        />
      );

      const typeSelect = screen.getByLabelText('Type');

      // Custom types should be present
      expect(typeSelect).toContainHTML('CustomType');
      expect(typeSelect).toContainHTML('AnotherType');

      // Static types (like SavedView) should NOT be in the dropdown
      expect(typeSelect).not.toContainHTML('SavedView');
    });

    it('excludes static types from dropdown', () => {
      useGraphStore.setState({
        schema: {
          node_types: {
            Actor: {
              fields: ['name', 'description'],
              description: 'Actors',
              color: '#3B82F6',
              static: false,
            },
            SavedView: {
              fields: ['name'],
              description: 'Saved views',
              color: '#6B7280',
              static: true,
            },
            VisualizationView: {
              fields: ['name'],
              description: 'Legacy views',
              color: '#6B7280',
              static: true,
            },
          },
        },
        configLoaded: true,
      });

      render(
        <EditNodeDialog
          node={mockNode}
          onClose={mockOnClose}
          onSave={mockOnSave}
        />
      );

      const typeSelect = screen.getByLabelText('Type');

      // Non-static type should be present
      expect(typeSelect).toContainHTML('Actor');

      // Static types should not be in dropdown
      const options = screen.getAllByRole('option');
      const optionTexts = options.map(o => o.textContent);
      expect(optionTexts).not.toContain('SavedView');
      expect(optionTexts).not.toContain('VisualizationView');
    });

    it('updates dropdown when schema changes', async () => {
      const { rerender } = render(
        <EditNodeDialog
          node={mockNode}
          onClose={mockOnClose}
          onSave={mockOnSave}
        />
      );

      // Initially default types
      let options = screen.getAllByRole('option');
      expect(options.length).toBeGreaterThan(1);

      // Update schema
      useGraphStore.setState({
        schema: {
          node_types: {
            NewType: {
              fields: ['name'],
              description: 'New type',
              color: '#FF00FF',
              static: false,
            },
          },
        },
        configLoaded: true,
      });

      // Rerender to pick up state changes
      rerender(
        <EditNodeDialog
          node={mockNode}
          onClose={mockOnClose}
          onSave={mockOnSave}
        />
      );

      const typeSelect = screen.getByLabelText('Type');
      expect(typeSelect).toContainHTML('NewType');
    });
  });

  describe('Form interactions', () => {
    it('calls onClose when cancel button clicked', async () => {
      render(
        <EditNodeDialog
          node={mockNode}
          onClose={mockOnClose}
          onSave={mockOnSave}
        />
      );

      fireEvent.click(screen.getByText('Cancel'));
      expect(mockOnClose).toHaveBeenCalled();
    });

    it('calls onClose when close button clicked', async () => {
      render(
        <EditNodeDialog
          node={mockNode}
          onClose={mockOnClose}
          onSave={mockOnSave}
        />
      );

      fireEvent.click(screen.getByText('×'));
      expect(mockOnClose).toHaveBeenCalled();
    });

    it('calls onClose when overlay clicked', async () => {
      render(
        <EditNodeDialog
          node={mockNode}
          onClose={mockOnClose}
          onSave={mockOnSave}
        />
      );

      const overlay = document.querySelector('.edit-dialog-overlay');
      fireEvent.click(overlay);
      expect(mockOnClose).toHaveBeenCalled();
    });

    it('calls onSave with form data when save button clicked', async () => {
      render(
        <EditNodeDialog
          node={mockNode}
          onClose={mockOnClose}
          onSave={mockOnSave}
        />
      );
      const user = userEvent.setup();

      // Change name
      const nameInput = screen.getByLabelText('Name');
      await user.clear(nameInput);
      await user.type(nameInput, 'Updated Name');

      // Submit form
      fireEvent.click(screen.getByText('Save'));

      expect(mockOnSave).toHaveBeenCalledWith(
        expect.objectContaining({
          name: 'Updated Name',
          type: 'Actor',
          tags: ['tag1', 'tag2'],
        })
      );
    });

    it('parses tags correctly', async () => {
      render(
        <EditNodeDialog
          node={mockNode}
          onClose={mockOnClose}
          onSave={mockOnSave}
        />
      );
      const user = userEvent.setup();

      // Update tags
      const tagsInput = screen.getByLabelText(/tags/i);
      await user.clear(tagsInput);
      await user.type(tagsInput, 'new, tags, here');

      fireEvent.click(screen.getByText('Save'));

      expect(mockOnSave).toHaveBeenCalledWith(
        expect.objectContaining({
          tags: ['new', 'tags', 'here'],
        })
      );
    });

    it('handles empty tags correctly', async () => {
      render(
        <EditNodeDialog
          node={mockNode}
          onClose={mockOnClose}
          onSave={mockOnSave}
        />
      );
      const user = userEvent.setup();

      // Clear tags
      const tagsInput = screen.getByLabelText(/tags/i);
      await user.clear(tagsInput);

      fireEvent.click(screen.getByText('Save'));

      expect(mockOnSave).toHaveBeenCalledWith(
        expect.objectContaining({
          tags: [],
        })
      );
    });
  });

  describe('Type selection', () => {
    it('allows changing node type', async () => {
      useGraphStore.setState({
        schema: {
          node_types: {
            Actor: { fields: ['name'], description: 'Actors', color: '#3B82F6', static: false },
            Initiative: { fields: ['name'], description: 'Initiatives', color: '#10B981', static: false },
          },
        },
        configLoaded: true,
      });

      render(
        <EditNodeDialog
          node={mockNode}
          onClose={mockOnClose}
          onSave={mockOnSave}
        />
      );
      const user = userEvent.setup();

      const typeSelect = screen.getByLabelText('Type');
      await user.selectOptions(typeSelect, 'Initiative');

      fireEvent.click(screen.getByText('Save'));

      expect(mockOnSave).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'Initiative',
        })
      );
    });

    it('preserves selected type value', async () => {
      useGraphStore.setState({
        schema: {
          node_types: {
            Actor: { fields: ['name'], description: 'Actors', color: '#3B82F6', static: false },
            CustomType: { fields: ['name'], description: 'Custom', color: '#FF0000', static: false },
          },
        },
        configLoaded: true,
      });

      const customNode = {
        ...mockNode,
        data: {
          ...mockNode.data,
          type: 'CustomType',
        },
      };

      render(
        <EditNodeDialog
          node={customNode}
          onClose={mockOnClose}
          onSave={mockOnSave}
        />
      );

      const typeSelect = screen.getByLabelText('Type');
      expect(typeSelect).toHaveValue('CustomType');
    });
  });
});
