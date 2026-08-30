import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import {
  buildContextMenuUrl,
  NodeContextMenu,
  MultiNodeContextMenu,
  EdgeContextMenu,
  PaneContextMenu,
  NearbyObjectMenuSection,
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
  align: 'Align',
  alignLeft: 'Align left',
  alignCenterHorizontal: 'Align horizontal centers',
  alignRight: 'Align right',
  alignTop: 'Align top',
  alignCenterVertical: 'Align vertical middles',
  alignBottom: 'Align bottom',
  distribute: 'Distribute',
  distributeHorizontal: 'Distribute horizontally',
  distributeVertical: 'Distribute vertically',
  hideAll: 'Hide all',
  deleteAll: 'Delete all',
  dimNode: 'Dim node',
  restoreNode: 'Restore node',
  dimSelected: 'Dim selected',
  restoreSelected: 'Restore selected',
  dimIncidentEdges: 'Dim incident edges',
  restoreIncidentEdges: 'Restore incident edges',
  dimEdge: 'Dim connection',
  restoreEdge: 'Restore connection',
  changeType: 'Change type',
  generalConnection: 'General connection',
  addNote: 'Add note',
  addLabel: 'Add label',
  addArrow: 'Add arrow',
  annotationNearbyMenu: 'Add nearby',
  annotationNearbyLabel: 'Label',
  annotationNearbyIcon: 'Icon',
  annotationNearbyText: 'Text',
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

  // task-annotation-render-direct-manipulation / task-annotation-responsive-
  // bottom-toolbox's "Nearby object menu" contract entry point
  // (docs/ANNOTATION_CONTRACT.md "Human authoring surfaces").
  describe('"Nearby object menu" section', () => {
    it('omits the section entirely when onAttachNearby is absent', () => {
      render(<NodeContextMenu menu={{ x: 0, y: 0, node }} labels={labels} onClose={vi.fn()} />);
      expect(screen.queryByText('Add nearby')).toBeNull();
      expect(screen.queryByRole('button', { name: '+ Label' })).toBeNull();
    });

    it('offers exactly the three attachable kinds — label, icon, text — not arrow or vote dot', () => {
      render(
        <NodeContextMenu
          menu={{ x: 0, y: 0, node }}
          labels={labels}
          onAttachNearby={vi.fn()}
          onClose={vi.fn()}
        />
      );
      expect(screen.getByRole('button', { name: '+ Label' })).toBeTruthy();
      expect(screen.getByRole('button', { name: '+ Icon' })).toBeTruthy();
      expect(screen.getByRole('button', { name: '+ Text' })).toBeTruthy();
      expect(screen.queryByRole('button', { name: '+ Arrow' })).toBeNull();
      // vote_dot is not attachable any more (task-annotation-vote-dot-simplify)
      // and is not offered here, though it remains a valid *target* for the
      // three kinds that are — see the "target candidacy" test below.
      expect(screen.queryByRole('button', { name: '+ Vote dot' })).toBeNull();
    });

    it('calls onAttachNearby with the target node id and the picked kind, then closes', () => {
      const onAttachNearby = vi.fn();
      const onClose = vi.fn();
      render(
        <NodeContextMenu
          menu={{ x: 0, y: 0, node }}
          labels={labels}
          onAttachNearby={onAttachNearby}
          onClose={onClose}
        />
      );
      fireEvent.click(screen.getByRole('button', { name: '+ Icon' }));
      expect(onAttachNearby).toHaveBeenCalledWith('n1', 'icon');
      expect(onClose).toHaveBeenCalled();
    });
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

  describe('dim/restore (task-session-focus-dimming-controls)', () => {
    it('omits the dim buttons when the handlers are absent', () => {
      render(<NodeContextMenu menu={{ x: 0, y: 0, node }} labels={labels} onClose={vi.fn()} />);
      expect(screen.queryByRole('button', { name: /dim node/i })).toBeNull();
      expect(screen.queryByRole('button', { name: /dim incident edges/i })).toBeNull();
    });

    it('offers to dim the node, then to restore it once dimmed', () => {
      const onDimNodes = vi.fn();
      const onRestoreNodes = vi.fn();
      const onClose = vi.fn();
      const { rerender } = render(
        <NodeContextMenu
          menu={{ x: 0, y: 0, node }}
          labels={labels}
          dimmedNodeIds={[]}
          onDimNodes={onDimNodes}
          onRestoreNodes={onRestoreNodes}
          onClose={onClose}
        />
      );
      fireEvent.click(screen.getByRole('button', { name: /dim node/i }));
      expect(onDimNodes).toHaveBeenCalledWith(['n1']);
      expect(onClose).toHaveBeenCalled();

      rerender(
        <NodeContextMenu
          menu={{ x: 0, y: 0, node }}
          labels={labels}
          dimmedNodeIds={['n1']}
          onDimNodes={onDimNodes}
          onRestoreNodes={onRestoreNodes}
          onClose={onClose}
        />
      );
      fireEvent.click(screen.getByRole('button', { name: /restore node/i }));
      expect(onRestoreNodes).toHaveBeenCalledWith(['n1']);
    });

    it('offers to dim every edge incident to the node, only when it has any', () => {
      const onDimEdges = vi.fn();
      const onClose = vi.fn();
      const { rerender } = render(
        <NodeContextMenu
          menu={{ x: 0, y: 0, node }}
          labels={labels}
          graphEdges={[]}
          onDimEdges={onDimEdges}
          onRestoreEdges={vi.fn()}
          onClose={onClose}
        />
      );
      expect(screen.queryByRole('button', { name: /dim incident edges/i })).toBeNull();

      rerender(
        <NodeContextMenu
          menu={{ x: 0, y: 0, node }}
          labels={labels}
          graphEdges={[
            { id: 'e1', source: 'n1', target: 'other' },
            { id: 'e2', source: 'other', target: 'n1' },
            { id: 'e3', source: 'other', target: 'other2' },
          ]}
          onDimEdges={onDimEdges}
          onRestoreEdges={vi.fn()}
          onClose={onClose}
        />
      );
      fireEvent.click(screen.getByRole('button', { name: /dim incident edges/i }));
      expect(onDimEdges).toHaveBeenCalledWith(['e1', 'e2']);
    });

    it('reads incident edges as already-dimmed only when every one of them is', () => {
      const onRestoreEdges = vi.fn();
      const graphEdges = [
        { id: 'e1', source: 'n1', target: 'other' },
        { id: 'e2', source: 'other', target: 'n1' },
      ];
      render(
        <NodeContextMenu
          menu={{ x: 0, y: 0, node }}
          labels={labels}
          graphEdges={graphEdges}
          dimmedEdgeIds={['e1']}
          onDimEdges={vi.fn()}
          onRestoreEdges={onRestoreEdges}
          onClose={vi.fn()}
        />
      );
      // Only e1 dimmed, not e2 — still offers "dim", not "restore".
      expect(screen.getByRole('button', { name: /dim incident edges/i })).toBeTruthy();

      render(
        <NodeContextMenu
          menu={{ x: 0, y: 0, node }}
          labels={labels}
          graphEdges={graphEdges}
          dimmedEdgeIds={['e1', 'e2']}
          onDimEdges={vi.fn()}
          onRestoreEdges={onRestoreEdges}
          onClose={vi.fn()}
        />
      );
      fireEvent.click(screen.getByRole('button', { name: /restore incident edges/i }));
      expect(onRestoreEdges).toHaveBeenCalledWith(['e1', 'e2']);
    });
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

  it('omits the align and distribute triggers when their callbacks are absent', () => {
    render(<MultiNodeContextMenu menu={{ x: 0, y: 0, nodes }} labels={labels} onClose={vi.fn()} />);
    expect(screen.queryByRole('button', { name: /^align$/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /^distribute$/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /align left/i })).toBeNull();
  });

  it('groups the align actions behind their own submenu trigger and calls onAlign with the chosen mode', () => {
    const onAlign = vi.fn();
    render(
      <MultiNodeContextMenu
        menu={{ x: 0, y: 0, nodes }}
        labels={labels}
        onAlign={onAlign}
        onClose={vi.fn()}
      />
    );
    // Not offered at the root level...
    expect(screen.queryByRole('button', { name: /align left/i })).toBeNull();
    // ...only once the Align trigger opens its panel.
    const openAlign = () => fireEvent.click(screen.getByRole('button', { name: /^align$/i }));
    openAlign();
    fireEvent.click(screen.getByRole('button', { name: /^align left$/i }));
    expect(onAlign).toHaveBeenNthCalledWith(1, 'left');
    openAlign();
    fireEvent.click(screen.getByRole('button', { name: /align horizontal centers/i }));
    expect(onAlign).toHaveBeenNthCalledWith(2, 'centerX');
    openAlign();
    fireEvent.click(screen.getByRole('button', { name: /^align right$/i }));
    expect(onAlign).toHaveBeenNthCalledWith(3, 'right');
    openAlign();
    fireEvent.click(screen.getByRole('button', { name: /^align top$/i }));
    expect(onAlign).toHaveBeenNthCalledWith(4, 'top');
    openAlign();
    fireEvent.click(screen.getByRole('button', { name: /align vertical middles/i }));
    expect(onAlign).toHaveBeenNthCalledWith(5, 'centerY');
    openAlign();
    fireEvent.click(screen.getByRole('button', { name: /^align bottom$/i }));
    expect(onAlign).toHaveBeenNthCalledWith(6, 'bottom');
  });

  it('groups the distribute actions behind their own submenu trigger and calls onDistribute with the chosen axis', () => {
    const onDistribute = vi.fn();
    render(
      <MultiNodeContextMenu
        menu={{ x: 0, y: 0, nodes }}
        labels={labels}
        onDistribute={onDistribute}
        onClose={vi.fn()}
      />
    );
    const openDistribute = () =>
      fireEvent.click(screen.getByRole('button', { name: /^distribute$/i }));
    openDistribute();
    fireEvent.click(screen.getByRole('button', { name: /distribute horizontally/i }));
    expect(onDistribute).toHaveBeenNthCalledWith(1, 'horizontal');
    openDistribute();
    fireEvent.click(screen.getByRole('button', { name: /distribute vertically/i }));
    expect(onDistribute).toHaveBeenNthCalledWith(2, 'vertical');
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

  it('uses filtered action nodes for callbacks while the header counts the full selection', () => {
    const onShowOnly = vi.fn();
    const onHideMultiple = vi.fn();
    const onDeleteMultiple = vi.fn();
    const onDimNodes = vi.fn();
    render(
      <MultiNodeContextMenu
        menu={{ x: 0, y: 0, nodes, actionNodes: [nodes[0]] }}
        labels={labels}
        onShowOnly={onShowOnly}
        onHideMultiple={onHideMultiple}
        onDeleteMultiple={onDeleteMultiple}
        onDimNodes={onDimNodes}
        onRestoreNodes={vi.fn()}
        onClose={vi.fn()}
      />
    );

    expect(screen.getByText('2 nodes selected')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /show only/i }));
    expect(onShowOnly).toHaveBeenCalledWith(['a']);

    fireEvent.click(screen.getByRole('button', { name: /hide all/i }));
    expect(onHideMultiple).toHaveBeenCalledWith(['a']);

    fireEvent.click(screen.getByRole('button', { name: /dim selected/i }));
    expect(onDimNodes).toHaveBeenCalledWith(['a']);

    fireEvent.click(screen.getByRole('button', { name: /delete all/i }));
    expect(onDeleteMultiple).toHaveBeenCalledWith(['a']);
  });

  describe('dim/restore (task-session-focus-dimming-controls)', () => {
    it('dims the whole selection, then offers to restore it once every node is dimmed', () => {
      const onDimNodes = vi.fn();
      const onRestoreNodes = vi.fn();
      const { rerender } = render(
        <MultiNodeContextMenu
          menu={{ x: 0, y: 0, nodes }}
          labels={labels}
          onDimNodes={onDimNodes}
          onRestoreNodes={onRestoreNodes}
          onClose={vi.fn()}
        />
      );
      fireEvent.click(screen.getByRole('button', { name: /dim selected/i }));
      expect(onDimNodes).toHaveBeenCalledWith(['a', 'b']);

      rerender(
        <MultiNodeContextMenu
          menu={{ x: 0, y: 0, nodes }}
          labels={labels}
          dimmedNodeIds={['a', 'b']}
          onDimNodes={onDimNodes}
          onRestoreNodes={onRestoreNodes}
          onClose={vi.fn()}
        />
      );
      fireEvent.click(screen.getByRole('button', { name: /restore selected/i }));
      expect(onRestoreNodes).toHaveBeenCalledWith(['a', 'b']);
    });

    it('dims the de-duplicated union of edges incident to any selected node', () => {
      const onDimEdges = vi.fn();
      render(
        <MultiNodeContextMenu
          menu={{ x: 0, y: 0, nodes }}
          labels={labels}
          graphEdges={[
            { id: 'e1', source: 'a', target: 'other' },
            { id: 'e2', source: 'other', target: 'b' },
            { id: 'e3', source: 'a', target: 'b' }, // incident to both — must not duplicate
            { id: 'e4', source: 'other', target: 'other2' },
          ]}
          onDimEdges={onDimEdges}
          onRestoreEdges={vi.fn()}
          onClose={vi.fn()}
        />
      );
      fireEvent.click(screen.getByRole('button', { name: /dim incident edges/i }));
      expect(onDimEdges).toHaveBeenCalledWith(['e1', 'e2', 'e3']);
    });
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

  describe('dim/restore (task-session-focus-dimming-controls)', () => {
    it('dims the single edge, then offers to restore it once dimmed', () => {
      const onDimEdges = vi.fn();
      const onRestoreEdges = vi.fn();
      const { rerender } = render(
        <EdgeContextMenu
          menu={{ x: 0, y: 0, edge, edgeIds: ['e1'] }}
          labels={labels}
          relationshipTypes={[]}
          onDimEdges={onDimEdges}
          onRestoreEdges={onRestoreEdges}
          onClose={vi.fn()}
        />
      );
      fireEvent.click(screen.getByRole('button', { name: /dim connection/i }));
      expect(onDimEdges).toHaveBeenCalledWith(['e1']);

      rerender(
        <EdgeContextMenu
          menu={{ x: 0, y: 0, edge, edgeIds: ['e1'] }}
          labels={labels}
          relationshipTypes={[]}
          dimmedEdgeIds={['e1']}
          onDimEdges={onDimEdges}
          onRestoreEdges={onRestoreEdges}
          onClose={vi.fn()}
        />
      );
      fireEvent.click(screen.getByRole('button', { name: /restore connection/i }));
      expect(onRestoreEdges).toHaveBeenCalledWith(['e1']);
    });

    it('acts on the whole multi-edge selection when the clicked edge is part of one', () => {
      const onDimEdges = vi.fn();
      render(
        <EdgeContextMenu
          menu={{ x: 0, y: 0, edge, edgeIds: ['e1', 'e2', 'e3'] }}
          labels={labels}
          relationshipTypes={[]}
          onDimEdges={onDimEdges}
          onRestoreEdges={vi.fn()}
          onClose={vi.fn()}
        />
      );
      fireEvent.click(screen.getByRole('button', { name: /dim connection/i }));
      expect(onDimEdges).toHaveBeenCalledWith(['e1', 'e2', 'e3']);
    });

    it('falls back to just the clicked edge when no menu.edgeIds is given', () => {
      const onDimEdges = vi.fn();
      render(
        <EdgeContextMenu
          menu={{ x: 0, y: 0, edge }}
          labels={labels}
          relationshipTypes={[]}
          onDimEdges={onDimEdges}
          onRestoreEdges={vi.fn()}
          onClose={vi.fn()}
        />
      );
      fireEvent.click(screen.getByRole('button', { name: /dim connection/i }));
      expect(onDimEdges).toHaveBeenCalledWith(['e1']);
    });
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

describe('root menu viewport-edge clamping (c3174865-f36e-4eb2-befa-cb10784babf0)', () => {
  // Simulates a rendered menu that overflows the viewport by a fixed amount on
  // each axis, the same technique the Submenu flip test above uses — the exact
  // pixel size of the menu doesn't matter, only how far its edges sit past the
  // viewport bounds.
  function mockOverflow(overflowX, overflowY) {
    const original = Element.prototype.getBoundingClientRect;
    Element.prototype.getBoundingClientRect = () => ({
      x: 0,
      y: 0,
      width: 200,
      height: 100,
      top: 0,
      left: 0,
      right: window.innerWidth + overflowX,
      bottom: window.innerHeight + overflowY,
      toJSON() {},
    });
    return () => {
      Element.prototype.getBoundingClientRect = original;
    };
  }

  const node = { id: 'n1', data: { nodeType: 'Actor' } };
  const edge = { id: 'e1', label: 'RELATES_TO', data: {} };

  it('leaves NodeContextMenu at the raw descriptor position when it fits the viewport', () => {
    render(<NodeContextMenu menu={{ x: 12, y: 34, node }} labels={labels} onClose={vi.fn()} />);
    const el = document.querySelector('.node-context-menu');
    expect(el.style.left).toBe('12px');
    expect(el.style.top).toBe('34px');
  });

  it('clamps NodeContextMenu back inside the viewport when it would overflow right/bottom', () => {
    const restore = mockOverflow(50, 30);
    try {
      render(<NodeContextMenu menu={{ x: 900, y: 700, node }} labels={labels} onClose={vi.fn()} />);
      const el = document.querySelector('.node-context-menu');
      expect(el.style.left).toBe('850px');
      expect(el.style.top).toBe('670px');
    } finally {
      restore();
    }
  });

  it('clamps MultiNodeContextMenu back inside the viewport when it would overflow', () => {
    const restore = mockOverflow(40, 20);
    try {
      const nodes = [{ id: 'a', data: { nodeType: 'Actor' } }];
      render(
        <MultiNodeContextMenu menu={{ x: 900, y: 700, nodes }} labels={labels} onClose={vi.fn()} />
      );
      const el = document.querySelector('.multi-node-context-menu');
      expect(el.style.left).toBe('860px');
      expect(el.style.top).toBe('680px');
    } finally {
      restore();
    }
  });

  it('clamps EdgeContextMenu back inside the viewport when it would overflow', () => {
    const restore = mockOverflow(60, 10);
    try {
      render(
        <EdgeContextMenu
          menu={{ x: 900, y: 700, edge }}
          labels={labels}
          relationshipTypes={[]}
          onClose={vi.fn()}
        />
      );
      const el = document.querySelector('.edge-context-menu');
      expect(el.style.left).toBe('840px');
      expect(el.style.top).toBe('690px');
    } finally {
      restore();
    }
  });

  it('clamps PaneContextMenu back inside the viewport when it would overflow', () => {
    const restore = mockOverflow(25, 15);
    try {
      render(
        <PaneContextMenu
          menu={{ x: 900, y: 700, flowPosition: { x: 0, y: 0 } }}
          labels={labels}
          createAnnotation={vi.fn()}
        />
      );
      const el = document.querySelector('.pane-context-menu');
      expect(el.style.left).toBe('875px');
      expect(el.style.top).toBe('685px');
    } finally {
      restore();
    }
  });

  it('never clamps to a negative coordinate when the overflow exceeds the raw position', () => {
    const restore = mockOverflow(500, 500);
    try {
      render(<NodeContextMenu menu={{ x: 10, y: 10, node }} labels={labels} onClose={vi.fn()} />);
      const el = document.querySelector('.node-context-menu');
      expect(el.style.left).toBe('0px');
      expect(el.style.top).toBe('0px');
    } finally {
      restore();
    }
  });

  it('only adjusts the axis that actually overflows', () => {
    const restore = mockOverflow(45, 0);
    try {
      render(<NodeContextMenu menu={{ x: 900, y: 200, node }} labels={labels} onClose={vi.fn()} />);
      const el = document.querySelector('.node-context-menu');
      expect(el.style.left).toBe('855px');
      expect(el.style.top).toBe('200px');
    } finally {
      restore();
    }
  });
});

// NearbyObjectMenuSection is the shared piece NodeContextMenu (tested via the
// `annotationNearby*`-keyed `cml` labels above) and every annotation-kind
// context menu (NoteNode/LabelNode/ArrowNode/GenericAnnotationNode/
// FreehandAnnotationNode, which each pass AnnotationContext's own short-keyed
// `labels` object instead) render it from. Testing it in isolation, with the
// short-key `labels` shape those five components actually pass, is what
// catches a caller passing the wrong key scheme without going through five
// separate component render trees.
describe('NearbyObjectMenuSection', () => {
  const shortLabels = {
    nearbyMenu: 'Add nearby',
    nearbyLabel: 'Label',
    nearbyIcon: 'Icon',
    nearbyText: 'Text',
  };

  it('renders nothing when onAttach is absent', () => {
    const { container } = render(
      <NearbyObjectMenuSection labels={shortLabels} onAttach={undefined} />
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders the three kinds and calls onAttach with the picked kind', () => {
    const onAttach = vi.fn();
    render(<NearbyObjectMenuSection labels={shortLabels} onAttach={onAttach} />);
    expect(screen.getByText('Add nearby')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '+ Vote dot' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '+ Icon' }));
    expect(onAttach).toHaveBeenCalledWith('icon');
  });
});
