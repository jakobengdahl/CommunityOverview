import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children }) => <div data-testid="react-flow">{children}</div>;
  return {
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    useNodesState: (initial) => [initial || [], hoisted.setNodes, vi.fn()],
    useEdgesState: (initial) => [initial || [], vi.fn(), vi.fn()],
    useReactFlow: () => ({
      fitView: vi.fn(),
      getNodes: () => [],
      getEdges: () => [],
      setNodes: hoisted.setNodes,
      setEdges: vi.fn(),
      screenToFlowPosition: ({ x, y }) => ({ x, y }),
      setCenter: vi.fn(),
      getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
    }),
    useOnSelectionChange: () => {},
    Background: () => null,
    Controls: () => null,
    MiniMap: () => null,
    NodeResizer: () => null,
    SelectionMode: { Partial: 'partial' },
  };
});

// Several effects call setNodes (including the input-sync effect that resets to
// the empty `nodes` prop). Apply each captured updater to a fresh copy of `seed`
// independently and return the first result that satisfies `predicate`, isolating
// the remote-apply updater under test from the others.
function findResult(seed, predicate) {
  for (const call of hoisted.setNodes.mock.calls) {
    if (typeof call[0] !== 'function') continue;
    let result;
    try {
      result = call[0](seed.map((n) => ({ ...n })));
    } catch {
      continue;
    }
    if (Array.isArray(result) && predicate(result)) return result;
  }
  return null;
}

describe('GraphCanvas remote apply (design step 6)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('applies a remote absolute position to a matching node', () => {
    const onRemotePositionsApplied = vi.fn();
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remotePositions={{ 'node-a': { x: 42, y: 99 } }}
        onRemotePositionsApplied={onRemotePositionsApplied}
      />
    );
    const seed = [{ id: 'node-a', type: 'custom', position: { x: 0, y: 0 } }];
    const result = findResult(seed, (r) => r.find((n) => n.id === 'node-a')?.position.x === 42);
    expect(result.find((n) => n.id === 'node-a').position).toEqual({ x: 42, y: 99 });
    expect(onRemotePositionsApplied).toHaveBeenCalled();
  });

  it('applies a remote position once its node mounts, even if it arrived first', () => {
    // Race in the backlog: node_moved for a node reaches the client before the
    // paired nodes_added has resolved its async node-details fetch, so the
    // position effect fires while the node doesn't exist in ReactFlow yet.
    const { rerender } = render(
      <GraphCanvas nodes={[]} edges={[]} remotePositions={{ 'late-node': { x: 33, y: 44 } }} />
    );
    // The node now mounts (its nodes_added resolved) and the parent has since
    // cleared remotePositions, as App.jsx does once notified. Only the mount
    // itself should be enough to apply the position that arrived too early.
    hoisted.setNodes.mockClear();
    rerender(
      <GraphCanvas
        nodes={[{ id: 'late-node', type: 'Actor', name: 'Late' }]}
        edges={[]}
        remotePositions={null}
      />
    );
    const seed = [{ id: 'late-node', type: 'custom', position: { x: 0, y: 0 } }];
    const result = findResult(seed, (r) => r.find((n) => n.id === 'late-node')?.position.x === 33);
    expect(result?.find((n) => n.id === 'late-node').position).toEqual({ x: 33, y: 44 });
  });

  it('applies a grouped node position verbatim (already relative — no re-offset)', () => {
    // The emit side stores n.position, which ReactFlow keeps relative to the
    // parent for a grouped node, so the apply side must not subtract the parent
    // position again.
    render(<GraphCanvas nodes={[]} edges={[]} remotePositions={{ child: { x: 50, y: 20 } }} />);
    const seed = [
      { id: 'grp', type: 'group', position: { x: 100, y: 100 } },
      { id: 'child', type: 'custom', parentId: 'grp', position: { x: 0, y: 0 } },
    ];
    const result = findResult(seed, (r) => r.find((n) => n.id === 'child')?.position.x === 50);
    expect(result.find((n) => n.id === 'child').position).toEqual({ x: 50, y: 20 });
  });

  it('upserts a remote overlay annotation', () => {
    const onApplied = vi.fn();
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteAnnotationOps={[
          {
            action: 'upsert-overlay',
            overlay: { id: 'note-x', kind: 'note', position: { x: 1, y: 2 }, text: 'hi' },
          },
        ]}
        onRemoteAnnotationsApplied={onApplied}
      />
    );
    const result = findResult([], (r) => r.some((n) => n.id === 'note-x' && n.type === 'note'));
    expect(result.some((n) => n.id === 'note-x' && n.type === 'note')).toBe(true);
    expect(onApplied).toHaveBeenCalled();
  });

  it('applies every op in a queued burst (no coalescing)', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteAnnotationOps={[
          {
            action: 'upsert-overlay',
            overlay: { id: 'note-1', kind: 'note', position: { x: 0, y: 0 }, text: 'a' },
          },
          {
            action: 'upsert-overlay',
            overlay: { id: 'note-2', kind: 'label', position: { x: 0, y: 0 }, text: 'b' },
          },
        ]}
      />
    );
    expect(findResult([], (r) => r.some((n) => n.id === 'note-1'))).toBeTruthy();
    expect(findResult([], (r) => r.some((n) => n.id === 'note-2'))).toBeTruthy();
  });

  it('deletes a remote annotation and un-parents its children', () => {
    render(
      <GraphCanvas nodes={[]} edges={[]} remoteAnnotationOps={[{ action: 'delete', id: 'grp' }]} />
    );
    const seed = [
      { id: 'grp', type: 'group', position: { x: 0, y: 0 } },
      { id: 'child', type: 'custom', parentId: 'grp', position: { x: 0, y: 0 } },
    ];
    const result = findResult(
      seed,
      (r) => !r.some((n) => n.id === 'grp') && r.some((n) => n.id === 'child')
    );
    expect(result.some((n) => n.id === 'grp')).toBe(false);
    expect(result.find((n) => n.id === 'child').parentId).toBeUndefined();
  });

  it('injects remoteSelections into the matching node data (step 7)', () => {
    render(
      <GraphCanvas
        nodes={[{ id: 'node-a', type: 'Actor', name: 'A' }]}
        edges={[]}
        remoteSelections={{ 'node-a': { clientId: 'c2', color: '#e6194b', displayName: 'Ada' } }}
      />
    );
    const result = findResult([], (r) =>
      r.some((n) => n.id === 'node-a' && n.data?.remoteSelection?.displayName === 'Ada')
    );
    expect(result.find((n) => n.id === 'node-a').data.remoteSelection).toEqual({
      clientId: 'c2',
      color: '#e6194b',
      displayName: 'Ada',
    });
  });

  it('stamps a live remote claim onto an annotation node and refuses local dragging (task-annotation-shared-session-realtime)', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteSelections={{ 'note-1': { clientId: 'c2', color: '#e6194b', displayName: 'Ada' } }}
      />
    );
    const seed = [
      { id: 'note-1', type: 'note', position: { x: 0, y: 0 }, data: {}, draggable: true },
    ];
    const result = findResult(
      seed,
      (r) => r.find((n) => n.id === 'note-1')?.data?.remoteSelection?.displayName === 'Ada'
    );
    const note = result.find((n) => n.id === 'note-1');
    expect(note.data.remoteSelection).toEqual({
      clientId: 'c2',
      color: '#e6194b',
      displayName: 'Ada',
    });
    expect(note.draggable).toBe(false);
  });

  it('leaves a graph ("custom") node untouched by the annotation remote-claim effect', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteSelections={{
          'graph-node-a': { clientId: 'c2', color: '#e6194b', displayName: 'Ada' },
        }}
      />
    );
    const seed = [
      { id: 'graph-node-a', type: 'custom', position: { x: 0, y: 0 }, data: {}, draggable: true },
    ];
    // The annotation-claim effect's updater must be a no-op for a 'custom' node
    // (graph nodes get their remoteSelection marker from the separate
    // reactFlowNodes memo, not this effect) — applying every captured updater
    // to the seed must never flip draggable to false via this path.
    for (const call of hoisted.setNodes.mock.calls) {
      if (typeof call[0] !== 'function') continue;
      let result;
      try {
        result = call[0](seed.map((n) => ({ ...n, data: { ...n.data } })));
      } catch {
        continue;
      }
      if (!Array.isArray(result)) continue;
      const node = result.find((n) => n.id === 'graph-node-a');
      if (node) expect(node.draggable).not.toBe(false);
    }
  });

  it('restores dragging once a remote claim is released', () => {
    const { rerender } = render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteSelections={{ 'note-1': { clientId: 'c2', color: '#e6194b', displayName: 'Ada' } }}
      />
    );
    hoisted.setNodes.mockClear();
    rerender(<GraphCanvas nodes={[]} edges={[]} remoteSelections={{}} />);
    const seed = [
      {
        id: 'note-1',
        type: 'note',
        position: { x: 0, y: 0 },
        data: { remoteSelection: { clientId: 'c2', color: '#e6194b', displayName: 'Ada' } },
        draggable: false,
      },
    ];
    const result = findResult(
      seed,
      (r) => r.find((n) => n.id === 'note-1')?.data?.remoteSelection == null
    );
    const note = result.find((n) => n.id === 'note-1');
    expect(note.data.remoteSelection).toBeNull();
    expect(note.draggable).toBe(true);
  });

  it('keeps an anchored arrow non-draggable even once its remote claim clears (isArrowAnchored still applies)', () => {
    const { rerender } = render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteSelections={{ 'arrow-1': { clientId: 'c2', color: '#e6194b', displayName: 'Ada' } }}
      />
    );
    hoisted.setNodes.mockClear();
    rerender(<GraphCanvas nodes={[]} edges={[]} remoteSelections={{}} />);
    const seed = [
      {
        id: 'arrow-1',
        type: 'arrow',
        position: { x: 0, y: 0 },
        data: {
          startAnchor: 'graph-node-a',
          remoteSelection: { clientId: 'c2', color: '#e6194b', displayName: 'Ada' },
        },
        draggable: false,
      },
    ];
    const result = findResult(
      seed,
      (r) => r.find((n) => n.id === 'arrow-1')?.data?.remoteSelection == null
    );
    expect(result.find((n) => n.id === 'arrow-1').draggable).toBe(false);
  });

  it('reassigns membership from a remote group_membership_changed', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteAnnotationOps={[{ action: 'membership', groupId: 'grp', members: ['a'] }]}
      />
    );
    const seed = [
      { id: 'grp', type: 'group', position: { x: 0, y: 0 } },
      { id: 'a', type: 'custom', position: { x: 0, y: 0 } },
      { id: 'b', type: 'custom', parentId: 'grp', position: { x: 0, y: 0 } },
    ];
    const result = findResult(seed, (r) => r.find((n) => n.id === 'a')?.parentId === 'grp');
    expect(result.find((n) => n.id === 'a').parentId).toBe('grp');
    expect(result.find((n) => n.id === 'b').parentId).toBeUndefined();
  });
});
