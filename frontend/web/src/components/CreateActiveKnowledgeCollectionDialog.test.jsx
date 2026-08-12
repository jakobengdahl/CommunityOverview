/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import CreateActiveKnowledgeCollectionDialog from './CreateActiveKnowledgeCollectionDialog';

const mockUseGraphStore = vi.fn();

vi.mock('../store/graphStore', () => ({
  default: (selector) => mockUseGraphStore(selector),
}));

describe('CreateActiveKnowledgeCollectionDialog', () => {
  it('excludes system types including Skill from the permissions table', () => {
    const state = {
      schema: {
        node_types: {
          Actor: {},
          Skill: {},
          CollectionResponse: {},
          ActiveKnowledgeCollection: {},
        },
      },
    };

    mockUseGraphStore.mockImplementation((selector) => selector(state));

    render(<CreateActiveKnowledgeCollectionDialog onClose={vi.fn()} onSave={vi.fn()} />);

    expect(screen.getByText('Actor')).toBeDefined();
    expect(screen.queryByText('Skill')).toBeNull();
    expect(screen.queryByText('CollectionResponse')).toBeNull();
    expect(screen.queryByText('ActiveKnowledgeCollection')).toBeNull();
  });

  it('drops excluded types from saved permissions in edit mode', () => {
    const onSave = vi.fn();
    const onClose = vi.fn();
    const state = {
      schema: {
        node_types: {
          Actor: {},
          Skill: {},
          CollectionResponse: {},
          ActiveKnowledgeCollection: {},
        },
      },
    };

    mockUseGraphStore.mockImplementation((selector) => selector(state));

    render(
      <CreateActiveKnowledgeCollectionDialog
        onClose={onClose}
        onSave={onSave}
        initialData={{
          node: {
            id: 'akc-1',
            name: 'Existing collection',
            description: 'Existing description',
            aliases: [],
            metadata: {
              short_name: 'existing-collection',
              introduction_text: 'Intro',
              prompt: 'Prompt',
              node_type_permissions: {
                Actor: { create: true, update: true, delete: false },
                Skill: { create: false, update: false, delete: false },
                CollectionResponse: { create: false, update: false, delete: false },
              },
            },
          },
        }}
      />
    );

    expect(screen.queryByText('Skill')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0].metadata.node_type_permissions).toEqual({
      Actor: { create: true, update: true, delete: false },
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('defaults link_results to true and saves it (create mode)', () => {
    const onSave = vi.fn();
    const state = { schema: { node_types: { Actor: {} } } };
    mockUseGraphStore.mockImplementation((selector) => selector(state));

    render(<CreateActiveKnowledgeCollectionDialog onClose={vi.fn()} onSave={onSave} />);

    const checkbox = screen.getByRole('checkbox', {
      name: 'Link created and updated nodes to the response',
    });
    expect(checkbox.checked).toBe(true);

    fireEvent.change(screen.getByPlaceholderText("e.g. 'Q1 Partner Feedback'"), {
      target: { value: 'My collection' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create Collection' }));

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0].metadata.link_results).toBe(true);
  });

  it('reflects link_results=false from initialData and saves the toggled value', () => {
    const onSave = vi.fn();
    const state = { schema: { node_types: { Actor: {} } } };
    mockUseGraphStore.mockImplementation((selector) => selector(state));

    render(
      <CreateActiveKnowledgeCollectionDialog
        onClose={vi.fn()}
        onSave={onSave}
        initialData={{
          node: {
            id: 'akc-1',
            name: 'Existing collection',
            metadata: {
              short_name: 'existing-collection',
              link_results: false,
              node_type_permissions: {},
            },
          },
        }}
      />
    );

    const checkbox = screen.getByRole('checkbox', {
      name: 'Link created and updated nodes to the response',
    });
    expect(checkbox.checked).toBe(false);

    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0].metadata.link_results).toBe(true);
  });

  it('saves an empty tool_allowlist (unrestricted) by default', () => {
    const onSave = vi.fn();
    const state = { schema: { node_types: { Actor: {} } } };
    mockUseGraphStore.mockImplementation((selector) => selector(state));

    render(<CreateActiveKnowledgeCollectionDialog onClose={vi.fn()} onSave={onSave} />);

    fireEvent.change(screen.getByLabelText('Name *'), { target: { value: 'My collection' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create Collection' }));

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0].metadata.tool_allowlist).toEqual([]);
  });

  it('saves only the checked tools when restriction is enabled', () => {
    const onSave = vi.fn();
    const state = { schema: { node_types: { Actor: {} } } };
    mockUseGraphStore.mockImplementation((selector) => selector(state));

    render(<CreateActiveKnowledgeCollectionDialog onClose={vi.fn()} onSave={onSave} />);

    fireEvent.change(screen.getByLabelText('Name *'), { target: { value: 'My collection' } });

    // Enable restriction — the tool checklist appears (all checked by default)
    fireEvent.click(screen.getByRole('checkbox', { name: /Restrict which tools/ }));
    // Uncheck one tool
    fireEvent.click(screen.getByRole('checkbox', { name: 'Add nodes' }));

    fireEvent.click(screen.getByRole('button', { name: 'Create Collection' }));

    const saved = onSave.mock.calls[0][0].metadata.tool_allowlist;
    expect(saved).toContain('search_graph');
    expect(saved).not.toContain('add_nodes');
  });

  it('blocks saving a restricted allowlist with no tools selected', () => {
    const onSave = vi.fn();
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {});
    const state = { schema: { node_types: { Actor: {} } } };
    mockUseGraphStore.mockImplementation((selector) => selector(state));

    render(<CreateActiveKnowledgeCollectionDialog onClose={vi.fn()} onSave={onSave} />);

    fireEvent.change(screen.getByLabelText('Name *'), { target: { value: 'My collection' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /Restrict which tools/ }));

    // Uncheck every tool in the Assistant Tools section (the first checkbox in
    // the section is the restrict toggle — leave it on).
    const section = screen.getByText('Assistant Tools').closest('.form-section');
    within(section)
      .getAllByRole('checkbox')
      .slice(1)
      .forEach((cb) => fireEvent.click(cb));

    fireEvent.click(screen.getByRole('button', { name: 'Create Collection' }));

    expect(onSave).not.toHaveBeenCalled();
    expect(alertSpy).toHaveBeenCalled();
    alertSpy.mockRestore();
  });

  it('loads an existing tool_allowlist in edit mode', () => {
    const onSave = vi.fn();
    const state = { schema: { node_types: { Actor: {} } } };
    mockUseGraphStore.mockImplementation((selector) => selector(state));

    render(
      <CreateActiveKnowledgeCollectionDialog
        onClose={vi.fn()}
        onSave={onSave}
        initialData={{
          node: {
            id: 'akc-1',
            name: 'Existing',
            metadata: {
              short_name: 'existing',
              tool_allowlist: ['search_graph', 'present_form'],
            },
          },
        }}
      />
    );

    // Restriction is on and reflects the stored selection
    expect(screen.getByRole('checkbox', { name: /Restrict which tools/ }).checked).toBe(true);
    expect(screen.getByRole('checkbox', { name: 'Search the graph' }).checked).toBe(true);
    expect(screen.getByRole('checkbox', { name: 'Add nodes' }).checked).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
    expect(onSave.mock.calls[0][0].metadata.tool_allowlist).toEqual([
      'search_graph',
      'present_form',
    ]);
  });
});
