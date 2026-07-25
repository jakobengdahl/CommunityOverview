import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import {
  buildContextMenuUrl,
  NodeContextMenu,
  MultiNodeContextMenu,
  EdgeContextMenu,
  PaneContextMenu,
} from '../src/components/ContextMenus';

const labels = {
  edit: 'Edit',
  hide: 'Hide',
  expand: 'Find related nodes',
  delete: 'Delete',
  nodesSelected: '{count} nodes selected',
  showOnly: 'Show only these',
  selectSameType: 'Select all nodes of the same type',
  hideAll: 'Hide all',
  deleteAll: 'Delete all',
  changeType: 'Change type',
  generalConnection: 'General connection',
  addNote: 'Add note',
  addLabel: 'Add label',
  addArrow: 'Add arrow',
};

describe('buildContextMenuUrl', () => {
  it('substitutes {field} and [field] tokens with URI-encoded values', () => {
    const url = buildContextMenuUrl('https://x.test/?q={name}&r=[id]', {
      name: 'a b',
      id: 'x/y',
    });
    expect(url).toBe('https://x.test/?q=a%20b&r=x%2Fy');
  });

  it('treats missing fields as empty strings', () => {
    expect(buildContextMenuUrl('https://x.test/?q={missing}', {})).toBe('https://x.test/?q=');
  });

  it('returns null for non-http templates or non-strings', () => {
    expect(buildContextMenuUrl('javascript:alert(1)', {})).toBeNull();
    expect(buildContextMenuUrl('/relative', {})).toBeNull();
    expect(buildContextMenuUrl(null, {})).toBeNull();
  });
});

describe('NodeContextMenu', () => {
  const node = { id: 'n1', data: { nodeType: 'Actor', label: 'Alice' } };

  it('renders nothing when menu is null', () => {
    const { container } = render(<NodeContextMenu menu={null} labels={labels} />);
    expect(container.querySelector('.node-context-menu')).toBeNull();
  });

  it('positions the menu from the descriptor coordinates', () => {
    render(<NodeContextMenu menu={{ x: 12, y: 34, node }} labels={labels} onClose={vi.fn()} />);
    const el = document.querySelector('.node-context-menu');
    expect(el.style.left).toBe('12px');
    expect(el.style.top).toBe('34px');
  });

  it('omits edit/hide/expand/delete buttons when their handlers are absent', () => {
    render(<NodeContextMenu menu={{ x: 0, y: 0, node }} labels={labels} onClose={vi.fn()} />);
    expect(screen.queryByRole('button', { name: /edit/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /^🗑️ delete/i })).toBeNull();
    // The "select same type" button is always present.
    expect(screen.getByRole('button', { name: /select all nodes of the same type/i })).toBeTruthy();
  });

  it('calls the edit handler then closes', () => {
    const onEdit = vi.fn();
    const onClose = vi.fn();
    render(
      <NodeContextMenu
        menu={{ x: 0, y: 0, node }}
        labels={labels}
        onEdit={onEdit}
        onClose={onClose}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /edit/i }));
    expect(onEdit).toHaveBeenCalledWith('n1', node.data);
    expect(onClose).toHaveBeenCalled();
  });

  it('invokes selectNodesByType with the node type (without closing itself)', () => {
    const selectNodesByType = vi.fn();
    const onClose = vi.fn();
    render(
      <NodeContextMenu
        menu={{ x: 0, y: 0, node }}
        labels={labels}
        selectNodesByType={selectNodesByType}
        onClose={onClose}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /select all nodes of the same type/i }));
    expect(selectNodesByType).toHaveBeenCalledWith(['Actor']);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('renders schema open_url custom items and opens the built URL', () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => {});
    const onClose = vi.fn();
    const schema = {
      node_types: {
        Actor: {
          context_menu: [
            {
              label: 'Search',
              icon: '🔎',
              action: { type: 'open_url', url: 'https://x.test/?q={label}' },
            },
          ],
        },
      },
    };
    render(
      <NodeContextMenu
        menu={{ x: 0, y: 0, node }}
        labels={labels}
        schema={schema}
        onClose={onClose}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /search/i }));
    expect(openSpy).toHaveBeenCalledWith(
      'https://x.test/?q=Alice',
      '_blank',
      'noopener,noreferrer'
    );
    expect(onClose).toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it('renders schema callback custom items and dispatches the action', () => {
    const onContextMenuAction = vi.fn();
    const onClose = vi.fn();
    const schema = {
      node_types: {
        Actor: {
          context_menu: [{ label: 'Do it', action: { type: 'callback', name: 'do_it' } }],
        },
      },
    };
    render(
      <NodeContextMenu
        menu={{ x: 0, y: 0, node }}
        labels={labels}
        schema={schema}
        onContextMenuAction={onContextMenuAction}
        onClose={onClose}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /do it/i }));
    expect(onContextMenuAction).toHaveBeenCalledWith('do_it', 'n1', node.data);
    expect(onClose).toHaveBeenCalled();
  });
});

describe('MultiNodeContextMenu', () => {
  const nodes = [
    { id: 'a', data: { nodeType: 'Actor' } },
    { id: 'b', data: { nodeType: 'Theme' } },
  ];

  it('renders the selected-count header with the count substituted', () => {
    render(<MultiNodeContextMenu menu={{ x: 0, y: 0, nodes }} labels={labels} onClose={vi.fn()} />);
    expect(screen.getByText('2 nodes selected')).toBeTruthy();
  });

  it('falls back to per-node onHide when onHideMultiple is absent', () => {
    const onHide = vi.fn();
    const onClose = vi.fn();
    render(
      <MultiNodeContextMenu
        menu={{ x: 0, y: 0, nodes }}
        labels={labels}
        onHide={onHide}
        onClose={onClose}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /hide all/i }));
    expect(onHide).toHaveBeenCalledTimes(2);
    expect(onHide).toHaveBeenCalledWith('a');
    expect(onHide).toHaveBeenCalledWith('b');
    expect(onClose).toHaveBeenCalled();
  });

  it('prefers onDeleteMultiple over per-node onDelete', () => {
    const onDeleteMultiple = vi.fn();
    const onDelete = vi.fn();
    render(
      <MultiNodeContextMenu
        menu={{ x: 0, y: 0, nodes }}
        labels={labels}
        onDeleteMultiple={onDeleteMultiple}
        onDelete={onDelete}
        onClose={vi.fn()}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /delete all/i }));
    expect(onDeleteMultiple).toHaveBeenCalledWith(['a', 'b']);
    expect(onDelete).not.toHaveBeenCalled();
  });
});

describe('EdgeContextMenu', () => {
  const edge = { id: 'e1', label: 'RELATES_TO', data: {} };
  const relationshipTypes = [
    { type: 'RELATES_TO', description: '' },
    { type: 'WORKS_FOR', description: 'employment' },
  ];

  it('renders nothing when menu is null', () => {
    const { container } = render(
      <EdgeContextMenu menu={null} labels={labels} relationshipTypes={[]} />
    );
    expect(container.querySelector('.edge-context-menu')).toBeNull();
  });

  it('sets a new edge type then closes', () => {
    const onSetEdgeType = vi.fn();
    const onClose = vi.fn();
    render(
      <EdgeContextMenu
        menu={{ x: 0, y: 0, edge }}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onSetEdgeType={onSetEdgeType}
        onClose={onClose}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /^WORKS_FOR$/ }));
    expect(onSetEdgeType).toHaveBeenCalledWith('e1', 'WORKS_FOR');
    expect(onClose).toHaveBeenCalled();
  });

  it('hides the type picker when no relationship types are configured', () => {
    render(
      <EdgeContextMenu
        menu={{ x: 0, y: 0, edge }}
        labels={labels}
        relationshipTypes={[]}
        onSetEdgeType={vi.fn()}
        onClose={vi.fn()}
      />
    );
    expect(screen.queryByText('Change type')).toBeNull();
  });
});

describe('PaneContextMenu', () => {
  it('creates the requested annotation kind at the descriptor position', () => {
    const createAnnotation = vi.fn();
    const flowPosition = { x: 5, y: 6 };
    render(
      <PaneContextMenu
        menu={{ x: 0, y: 0, flowPosition }}
        labels={labels}
        createAnnotation={createAnnotation}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /add note/i }));
    fireEvent.click(screen.getByRole('button', { name: /add label/i }));
    fireEvent.click(screen.getByRole('button', { name: /add arrow/i }));
    expect(createAnnotation).toHaveBeenNthCalledWith(1, 'note', flowPosition);
    expect(createAnnotation).toHaveBeenNthCalledWith(2, 'label', flowPosition);
    expect(createAnnotation).toHaveBeenNthCalledWith(3, 'arrow', flowPosition);
  });

  it('attaches the forwarded menuRef to the menu element (outside-click dismiss relies on it)', () => {
    const menuRef = { current: null };
    render(
      <PaneContextMenu
        menu={{ x: 0, y: 0, flowPosition: { x: 0, y: 0 } }}
        labels={labels}
        menuRef={menuRef}
        createAnnotation={vi.fn()}
      />
    );
    expect(menuRef.current).toBe(document.querySelector('.pane-context-menu'));
  });
});
