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
  selectRelated: 'Select related nodes',
  viewHistory: 'View change history',
  organize: 'Organize',
  autoTidy: 'Auto-tidy',
  organizeCluster: 'Cluster',
  organizeHorizontal: 'List horizontally',
  organizeVertical: 'List vertically',
  organizeTree: 'Arrange as tree',
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

  it('omits the select-related button when no handler is given', () => {
    render(<NodeContextMenu menu={{ x: 0, y: 0, node }} labels={labels} onClose={vi.fn()} />);
    expect(screen.queryByRole('button', { name: /select related nodes/i })).toBeNull();
  });

  it('invokes onSelectRelated with the node id', () => {
    const onSelectRelated = vi.fn();
    render(
      <NodeContextMenu
        menu={{ x: 0, y: 0, node }}
        labels={labels}
        onSelectRelated={onSelectRelated}
        onClose={vi.fn()}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /select related nodes/i }));
    expect(onSelectRelated).toHaveBeenCalledWith('n1');
  });

  it('omits the view-history button when no handler is given', () => {
    render(<NodeContextMenu menu={{ x: 0, y: 0, node }} labels={labels} onClose={vi.fn()} />);
    expect(screen.queryByRole('button', { name: /view change history/i })).toBeNull();
  });

  it('invokes onViewHistory with the node id and data then closes', () => {
    const onViewHistory = vi.fn();
    const onClose = vi.fn();
    render(
      <NodeContextMenu
        menu={{ x: 0, y: 0, node }}
        labels={labels}
        onViewHistory={onViewHistory}
        onClose={onClose}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /view change history/i }));
    expect(onViewHistory).toHaveBeenCalledWith('n1', node.data);
    expect(onClose).toHaveBeenCalled();
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

  it('focuses the first item on open and ArrowDown/ArrowUp rove focus, wrapping at the ends', () => {
    render(
      <NodeContextMenu
        menu={{ x: 0, y: 0, node }}
        labels={labels}
        onEdit={vi.fn()}
        onHide={vi.fn()}
        onClose={vi.fn()}
      />
    );
    const editBtn = screen.getByRole('button', { name: /edit/i });
    const hideBtn = screen.getByRole('button', { name: /hide/i });
    const selectBtn = screen.getByRole('button', { name: /select all nodes of the same type/i });
    expect(document.activeElement).toBe(editBtn);
    fireEvent.keyDown(editBtn, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(hideBtn);
    fireEvent.keyDown(hideBtn, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(selectBtn);
    fireEvent.keyDown(selectBtn, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(editBtn);
    fireEvent.keyDown(editBtn, { key: 'ArrowUp' });
    expect(document.activeElement).toBe(selectBtn);
  });

  it('Home/End jump focus to the first/last root item', () => {
    render(
      <NodeContextMenu
        menu={{ x: 0, y: 0, node }}
        labels={labels}
        onEdit={vi.fn()}
        onHide={vi.fn()}
        onDelete={vi.fn()}
        onClose={vi.fn()}
      />
    );
    const editBtn = screen.getByRole('button', { name: /edit/i });
    const deleteBtn = screen.getByRole('button', { name: /delete/i });
    fireEvent.keyDown(editBtn, { key: 'End' });
    expect(document.activeElement).toBe(deleteBtn);
    fireEvent.keyDown(deleteBtn, { key: 'Home' });
    expect(document.activeElement).toBe(editBtn);
  });

  it('restores focus to the previously-focused element once the menu closes', () => {
    const trigger = document.createElement('button');
    document.body.appendChild(trigger);
    trigger.focus();

    const { rerender } = render(
      <NodeContextMenu menu={null} labels={labels} onEdit={vi.fn()} onClose={vi.fn()} />
    );
    expect(document.activeElement).toBe(trigger);

    rerender(
      <NodeContextMenu
        menu={{ x: 0, y: 0, node }}
        labels={labels}
        onEdit={vi.fn()}
        onClose={vi.fn()}
      />
    );
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /edit/i }));

    rerender(<NodeContextMenu menu={null} labels={labels} onEdit={vi.fn()} onClose={vi.fn()} />);
    expect(document.activeElement).toBe(trigger);
    document.body.removeChild(trigger);
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

  it('omits the organize section when onOrganize is absent', () => {
    render(<MultiNodeContextMenu menu={{ x: 0, y: 0, nodes }} labels={labels} onClose={vi.fn()} />);
    expect(screen.queryByText('Organize')).toBeNull();
    expect(screen.queryByRole('button', { name: /^cluster$/i })).toBeNull();
  });

  it('groups the organize actions behind a submenu trigger, not the root menu', () => {
    render(
      <MultiNodeContextMenu
        menu={{ x: 0, y: 0, nodes }}
        labels={labels}
        onOrganize={vi.fn()}
        onClose={vi.fn()}
      />
    );
    // The five layout actions are not offered directly at the root level...
    expect(screen.queryByRole('button', { name: /^cluster$/i })).toBeNull();
    // ...they appear once the "Organize" trigger opens its panel.
    fireEvent.click(screen.getByRole('button', { name: /^organize$/i }));
    expect(screen.getByRole('button', { name: /^cluster$/i })).toBeTruthy();
  });

  it('renders the organize options and calls onOrganize with the chosen mode', () => {
    const onOrganize = vi.fn();
    render(
      <MultiNodeContextMenu
        menu={{ x: 0, y: 0, nodes }}
        labels={labels}
        onOrganize={onOrganize}
        onClose={vi.fn()}
      />
    );
    const openOrganize = () => fireEvent.click(screen.getByRole('button', { name: /^organize$/i }));
    openOrganize();
    fireEvent.click(screen.getByRole('button', { name: /auto-tidy/i }));
    expect(onOrganize).toHaveBeenNthCalledWith(1, 'tidy');
    openOrganize();
    fireEvent.click(screen.getByRole('button', { name: /^cluster$/i }));
    expect(onOrganize).toHaveBeenNthCalledWith(2, 'cluster');
    openOrganize();
    fireEvent.click(screen.getByRole('button', { name: /list horizontally/i }));
    expect(onOrganize).toHaveBeenNthCalledWith(3, 'horizontal');
    openOrganize();
    fireEvent.click(screen.getByRole('button', { name: /list vertically/i }));
    expect(onOrganize).toHaveBeenNthCalledWith(4, 'vertical');
    openOrganize();
    fireEvent.click(screen.getByRole('button', { name: /arrange as tree/i }));
    expect(onOrganize).toHaveBeenNthCalledWith(5, 'tree');
  });

  it('returns focus to the Organize trigger after picking an action, instead of stranding it on <body>', () => {
    // The multi-node menu deliberately stays open after an Organize pick (so
    // the user can apply several arrangements in a row) — unlike the edge
    // "Change type" picker, nothing here closes the root menu for us.
    render(
      <MultiNodeContextMenu
        menu={{ x: 0, y: 0, nodes }}
        labels={labels}
        onOrganize={vi.fn()}
        onClose={vi.fn()}
      />
    );
    const organizeBtn = screen.getByRole('button', { name: /^organize$/i });
    fireEvent.click(organizeBtn);
    fireEvent.click(screen.getByRole('button', { name: /^cluster$/i }));
    expect(document.activeElement).toBe(organizeBtn);
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

  it('keeps every edge type out of the root menu until Change type is opened', () => {
    render(
      <EdgeContextMenu
        menu={{ x: 0, y: 0, edge }}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onSetEdgeType={vi.fn()}
        onClose={vi.fn()}
      />
    );
    expect(screen.queryByRole('button', { name: /^WORKS_FOR$/ })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /^change type$/i }));
    expect(screen.getByRole('button', { name: /^WORKS_FOR$/ })).toBeTruthy();
  });

  it('opens the Change type submenu from the keyboard (Enter or ArrowRight), not just a click', () => {
    render(
      <EdgeContextMenu
        menu={{ x: 0, y: 0, edge }}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onSetEdgeType={vi.fn()}
        onClose={vi.fn()}
      />
    );
    const trigger = screen.getByRole('button', { name: /^change type$/i });
    fireEvent.keyDown(trigger, { key: 'ArrowRight' });
    expect(screen.getByRole('button', { name: /^WORKS_FOR$/ })).toBeTruthy();
  });

  it('sets a new edge type then closes the whole menu', () => {
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
    fireEvent.click(screen.getByRole('button', { name: /^change type$/i }));
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

  it('ignores relationship type entries with no usable type name', () => {
    render(
      <EdgeContextMenu
        menu={{ x: 0, y: 0, edge }}
        labels={labels}
        relationshipTypes={[{ type: '' }, { description: 'no type at all' }, ...relationshipTypes]}
        onSetEdgeType={vi.fn()}
        onClose={vi.fn()}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /^change type$/i }));
    expect(screen.getByRole('button', { name: /^WORKS_FOR$/ })).toBeTruthy();
    // Only "General connection" + "WORKS_FOR" — the two malformed entries are dropped.
    const panelButtons = document.querySelectorAll('.edge-type-list button');
    expect(panelButtons.length).toBe(2);
  });

  it("disables the edge's current type as a selectable choice and does not offer it as an action", () => {
    const onSetEdgeType = vi.fn();
    render(
      <EdgeContextMenu
        menu={{ x: 0, y: 0, edge }}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onSetEdgeType={onSetEdgeType}
        onClose={vi.fn()}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /^change type$/i }));
    const currentTypeBtn = screen.getByRole('button', { name: /general connection/i });
    expect(currentTypeBtn).toBeDisabled();
    fireEvent.click(currentTypeBtn);
    expect(onSetEdgeType).not.toHaveBeenCalled();
  });

  it('focuses the first enabled submenu item on open, skipping the disabled current type', () => {
    render(
      <EdgeContextMenu
        menu={{ x: 0, y: 0, edge }}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onSetEdgeType={vi.fn()}
        onClose={vi.fn()}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /^change type$/i }));
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /^WORKS_FOR$/ }));
  });

  it('Escape closes only the submenu, returns focus to its trigger, and leaves the edge menu open', () => {
    render(
      <EdgeContextMenu
        menu={{ x: 0, y: 0, edge }}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onSetEdgeType={vi.fn()}
        onClose={vi.fn()}
      />
    );
    const trigger = screen.getByRole('button', { name: /^change type$/i });
    fireEvent.click(trigger);
    const worksForBtn = screen.getByRole('button', { name: /^WORKS_FOR$/ });
    fireEvent.keyDown(worksForBtn, { key: 'Escape' });
    expect(screen.queryByRole('button', { name: /^WORKS_FOR$/ })).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(document.querySelector('.edge-context-menu')).not.toBeNull();
  });

  it('ArrowDown/ArrowUp rove focus among the submenu items once open', () => {
    render(
      <EdgeContextMenu
        menu={{ x: 0, y: 0, edge }}
        labels={labels}
        relationshipTypes={[...relationshipTypes, { type: 'MENTIONS', description: '' }]}
        onSetEdgeType={vi.fn()}
        onClose={vi.fn()}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /^change type$/i }));
    const worksForBtn = screen.getByRole('button', { name: /^WORKS_FOR$/ });
    const mentionsBtn = screen.getByRole('button', { name: /^MENTIONS$/ });
    expect(document.activeElement).toBe(worksForBtn);
    fireEvent.keyDown(worksForBtn, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(mentionsBtn);
    fireEvent.keyDown(mentionsBtn, { key: 'ArrowUp' });
    expect(document.activeElement).toBe(worksForBtn);
  });

  it('ArrowDown/ArrowUp rove focus among the root-level items, wrapping at the ends', () => {
    render(
      <EdgeContextMenu
        menu={{ x: 0, y: 0, edge }}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onSetEdgeType={vi.fn()}
        onEditEdge={vi.fn()}
        onHideEdge={vi.fn()}
        onClose={vi.fn()}
      />
    );
    const changeTypeBtn = screen.getByRole('button', { name: /^change type$/i });
    const editBtn = screen.getByRole('button', { name: /edit/i });
    const hideBtn = screen.getByRole('button', { name: /hide/i });
    expect(document.activeElement).toBe(changeTypeBtn);
    fireEvent.keyDown(changeTypeBtn, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(editBtn);
    fireEvent.keyDown(editBtn, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(hideBtn);
    fireEvent.keyDown(hideBtn, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(changeTypeBtn);
    fireEvent.keyDown(changeTypeBtn, { key: 'ArrowUp' });
    expect(document.activeElement).toBe(hideBtn);
  });

  it('moves focus into the menu on open and restores it to the previously-focused element on close', () => {
    const trigger = document.createElement('button');
    document.body.appendChild(trigger);
    trigger.focus();
    expect(document.activeElement).toBe(trigger);

    const { rerender } = render(
      <EdgeContextMenu
        menu={null}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onClose={vi.fn()}
      />
    );
    expect(document.activeElement).toBe(trigger);

    rerender(
      <EdgeContextMenu
        menu={{ x: 0, y: 0, edge }}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onSetEdgeType={vi.fn()}
        onClose={vi.fn()}
      />
    );
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /^change type$/i }));

    rerender(
      <EdgeContextMenu
        menu={null}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onClose={vi.fn()}
      />
    );
    expect(document.activeElement).toBe(trigger);
    document.body.removeChild(trigger);
  });

  it('does not steal focus back on close if focus already moved elsewhere on the page', () => {
    const { rerender } = render(
      <EdgeContextMenu
        menu={{ x: 0, y: 0, edge }}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onSetEdgeType={vi.fn()}
        onClose={vi.fn()}
      />
    );
    // Something outside the menu (e.g. a search box the host closes menus for)
    // takes focus while the menu is still open.
    const elsewhere = document.createElement('input');
    document.body.appendChild(elsewhere);
    elsewhere.focus();
    expect(document.activeElement).toBe(elsewhere);

    rerender(
      <EdgeContextMenu
        menu={null}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onClose={vi.fn()}
      />
    );
    // Closing must not yank focus back to the menu's original trigger.
    expect(document.activeElement).toBe(elsewhere);
    document.body.removeChild(elsewhere);
  });

  it('re-focuses the first item when the menu retargets to a different edge without closing first', () => {
    const otherEdge = { id: 'e2', label: 'WORKS_FOR', data: {} };
    const { rerender } = render(
      <EdgeContextMenu
        menu={{ x: 0, y: 0, edge }}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onSetEdgeType={vi.fn()}
        onEditEdge={vi.fn()}
        onClose={vi.fn()}
      />
    );
    const changeTypeBtn = screen.getByRole('button', { name: /^change type$/i });
    expect(document.activeElement).toBe(changeTypeBtn);
    fireEvent.keyDown(changeTypeBtn, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /edit/i }));

    // GraphCanvas retargets an open menu straight to a new edge — a new
    // descriptor object, never passing through `menu: null`.
    rerender(
      <EdgeContextMenu
        menu={{ x: 10, y: 10, edge: otherEdge }}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onSetEdgeType={vi.fn()}
        onEditEdge={vi.fn()}
        onClose={vi.fn()}
      />
    );
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /^change type$/i }));
  });

  it('collapses an open Change type submenu when the menu retargets to a different edge', () => {
    const otherEdge = { id: 'e2', label: 'WORKS_FOR', data: {} };
    const { rerender } = render(
      <EdgeContextMenu
        menu={{ x: 0, y: 0, edge }}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onSetEdgeType={vi.fn()}
        onClose={vi.fn()}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /^change type$/i }));
    expect(document.querySelector('.context-submenu-panel')).not.toBeNull();

    // Retargeting straight to a different edge (no intervening close) must not
    // leave the previous target's submenu looking open for the new target.
    rerender(
      <EdgeContextMenu
        menu={{ x: 10, y: 10, edge: otherEdge }}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onSetEdgeType={vi.fn()}
        onClose={vi.fn()}
      />
    );
    expect(document.querySelector('.context-submenu-panel')).toBeNull();
  });

  it('flips the submenu panel to the opposite side/edge when it would overflow the viewport', () => {
    const originalRect = Element.prototype.getBoundingClientRect;
    Element.prototype.getBoundingClientRect = () => ({
      x: 0,
      y: 0,
      width: 200,
      height: 200,
      top: 0,
      left: 0,
      right: window.innerWidth + 50,
      bottom: window.innerHeight + 50,
      toJSON() {},
    });
    try {
      render(
        <EdgeContextMenu
          menu={{ x: 0, y: 0, edge }}
          labels={labels}
          relationshipTypes={relationshipTypes}
          onSetEdgeType={vi.fn()}
          onClose={vi.fn()}
        />
      );
      fireEvent.click(screen.getByRole('button', { name: /^change type$/i }));
      const panel = document.querySelector('.context-submenu-panel');
      expect(panel).not.toBeNull();
      expect(panel.className).toContain('context-submenu-panel-flip-x');
      expect(panel.className).toContain('context-submenu-panel-flip-y');
    } finally {
      Element.prototype.getBoundingClientRect = originalRect;
    }
  });

  it('does not flip the submenu panel when it fits within the viewport', () => {
    render(
      <EdgeContextMenu
        menu={{ x: 0, y: 0, edge }}
        labels={labels}
        relationshipTypes={relationshipTypes}
        onSetEdgeType={vi.fn()}
        onClose={vi.fn()}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /^change type$/i }));
    const panel = document.querySelector('.context-submenu-panel');
    expect(panel.className).not.toContain('flip');
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
