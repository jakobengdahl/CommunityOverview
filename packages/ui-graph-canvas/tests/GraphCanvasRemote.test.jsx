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
      fitView: vi.fn(), getNodes: () => [], getEdges: () => [],
      setNodes: hoisted.setNodes, setEdges: vi.fn(),
      screenToFlowPosition: ({ x, y }) => ({ x, y }),
      setCenter: vi.fn(), getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
    }),
    useOnSelectionChange: () => {},
    Background: () => null, Controls: () => null, MiniMap: () => null,
    NodeResizer: () => null, SelectionMode: { Partial: 'partial' },
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
    try { result = call[0](seed.map(n => ({ ...n }))); } catch { continue; }
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
        nodes={[]} edges={[]}
        remotePositions={{ 'node-a': { x: 42, y: 99 } }}
        onRemotePositionsApplied={onRemotePositionsApplied}
      />
    );
    const seed = [{ id: 'node-a', type: 'custom', position: { x: 0, y: 0 } }];
    const result = findResult(seed, r => r.find(n => n.id === 'node-a')?.position.x === 42);
    expect(result.find(n => n.id === 'node-a').position).toEqual({ x: 42, y: 99 });
    expect(onRemotePositionsApplied).toHaveBeenCalled();
  });

  it('converts a remote position to relative for a node inside a group', () => {
    render(
      <GraphCanvas nodes={[]} edges={[]} remotePositions={{ 'child': { x: 150, y: 120 } }} />
    );
    const seed = [
      { id: 'grp', type: 'group', position: { x: 100, y: 100 } },
      { id: 'child', type: 'custom', parentId: 'grp', position: { x: 0, y: 0 } },
    ];
    const result = findResult(seed, r => r.find(n => n.id === 'child')?.position.x === 50);
    expect(result.find(n => n.id === 'child').position).toEqual({ x: 50, y: 20 });
  });

  it('upserts a remote overlay annotation', () => {
    const onApplied = vi.fn();
    render(
      <GraphCanvas
        nodes={[]} edges={[]}
        remoteAnnotationOps={[{ action: 'upsert-overlay', overlay: { id: 'note-x', kind: 'note', position: { x: 1, y: 2 }, text: 'hi' } }]}
        onRemoteAnnotationsApplied={onApplied}
      />
    );
    const result = findResult([], r => r.some(n => n.id === 'note-x' && n.type === 'note'));
    expect(result.some(n => n.id === 'note-x' && n.type === 'note')).toBe(true);
    expect(onApplied).toHaveBeenCalled();
  });

  it('applies every op in a queued burst (no coalescing)', () => {
    render(
      <GraphCanvas
        nodes={[]} edges={[]}
        remoteAnnotationOps={[
          { action: 'upsert-overlay', overlay: { id: 'note-1', kind: 'note', position: { x: 0, y: 0 }, text: 'a' } },
          { action: 'upsert-overlay', overlay: { id: 'note-2', kind: 'label', position: { x: 0, y: 0 }, text: 'b' } },
        ]}
      />
    );
    expect(findResult([], r => r.some(n => n.id === 'note-1'))).toBeTruthy();
    expect(findResult([], r => r.some(n => n.id === 'note-2'))).toBeTruthy();
  });

  it('deletes a remote annotation and un-parents its children', () => {
    render(
      <GraphCanvas nodes={[]} edges={[]} remoteAnnotationOps={[{ action: 'delete', id: 'grp' }]} />
    );
    const seed = [
      { id: 'grp', type: 'group', position: { x: 0, y: 0 } },
      { id: 'child', type: 'custom', parentId: 'grp', position: { x: 0, y: 0 } },
    ];
    const result = findResult(seed, r => !r.some(n => n.id === 'grp') && r.some(n => n.id === 'child'));
    expect(result.some(n => n.id === 'grp')).toBe(false);
    expect(result.find(n => n.id === 'child').parentId).toBeUndefined();
  });

  it('reassigns membership from a remote group_membership_changed', () => {
    render(
      <GraphCanvas nodes={[]} edges={[]} remoteAnnotationOps={[{ action: 'membership', groupId: 'grp', members: ['a'] }]} />
    );
    const seed = [
      { id: 'grp', type: 'group', position: { x: 0, y: 0 } },
      { id: 'a', type: 'custom', position: { x: 0, y: 0 } },
      { id: 'b', type: 'custom', parentId: 'grp', position: { x: 0, y: 0 } },
    ];
    const result = findResult(seed, r => r.find(n => n.id === 'a')?.parentId === 'grp');
    expect(result.find(n => n.id === 'a').parentId).toBe('grp');
    expect(result.find(n => n.id === 'b').parentId).toBeUndefined();
  });
});
