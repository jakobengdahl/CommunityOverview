import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// The canvas leg of the group `locked`/`z` round trip. The translators in
// frontend/web/src/utils/sessionAnnotations.js are covered by their own suite;
// what those cannot show is that the flag survives the trip *through* the
// canvas. Three sites carry it — the groupsToRestore effect, the remote
// upsert-group op, and handleSaveView's group serialisation — and dropping any
// one of them puts the flag back at its default on the next autosave, which is
// what made locking a group a silent no-op.

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn(), seededNodes: [] }));

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children }) => <div data-testid="react-flow">{children}</div>;
  return {
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    // Seeded rather than derived: handleSaveView reads the live node list, and
    // a group never arrives through the `nodes` prop (it is restored through
    // its own effect), so the save leg has nothing to serialise otherwise.
    useNodesState: () => [hoisted.seededNodes, hoisted.setNodes, vi.fn()],
    useEdgesState: (initial) => [initial || [], vi.fn(), vi.fn()],
    useReactFlow: () => ({
      fitView: vi.fn(),
      getNodes: () => hoisted.seededNodes,
      getEdges: () => [],
      setNodes: hoisted.setNodes,
      setEdges: vi.fn(),
      screenToFlowPosition: ({ x, y }) => ({ x, y }),
      setCenter: vi.fn(),
      getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
    }),
    useOnSelectionChange: () => {},
    Background: () => <div />,
    Controls: () => <div />,
    MiniMap: () => <div />,
    NodeResizer: () => null,
    SelectionMode: { Partial: 'partial' },
  };
});

// setNodes fires from several effects; apply every captured updater to the
// given starting list and return the first result that contains a group.
function producedGroups(from = []) {
  for (const call of hoisted.setNodes.mock.calls) {
    const updater = call[0];
    const result = typeof updater === 'function' ? updater(from) : updater;
    if (Array.isArray(result) && result.some((n) => n?.type === 'group')) {
      return result.filter((n) => n.type === 'group');
    }
  }
  return null;
}

describe('group lock/layer round trip through the canvas', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.seededNodes = [];
  });

  it('carries locked and z onto a restored group and withholds dragging', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        groupsToRestore={{
          groups: [
            { id: 'g1', label: 'Locked team', position: { x: 4, y: 5 }, locked: true, z: 3 },
          ],
          parentIds: {},
        }}
      />
    );
    const [group] = producedGroups();
    expect(group.data.locked).toBe(true);
    expect(group.data.z).toBe(3);
    // Mirrors overlayToFlowNode's `draggable: !locked`. Without it the lock is
    // cosmetic: the menu refuses, but the box still moves and takes its
    // members with it.
    expect(group.draggable).toBe(false);
  });

  it('leaves an unlocked group draggable and at the base layer', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        groupsToRestore={{
          groups: [{ id: 'g1', label: 'Team', position: { x: 0, y: 0 } }],
          parentIds: {},
        }}
      />
    );
    const [group] = producedGroups();
    expect(group.data.locked).toBe(false);
    expect(group.data.z).toBe(0);
    // Not `true`: ReactFlow reads an explicit boolean as an override of the
    // canvas-wide `nodesDraggable` switch, so an unlocked group must leave the
    // key unset and keep honouring it — otherwise it stays draggable during a
    // freehand stroke, which that switch exists to prevent.
    expect(group.draggable).toBeUndefined();
  });

  it('carries locked and z through a remote upsert-group op', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteAnnotationOps={[
          {
            action: 'upsert-group',
            group: { id: 'g1', label: 'Team', position: { x: 0, y: 0 }, locked: true, z: 2 },
            members: [],
          },
        ]}
      />
    );
    const [group] = producedGroups();
    expect(group.data.locked).toBe(true);
    expect(group.data.z).toBe(2);
    expect(group.draggable).toBe(false);
  });

  it('re-emits locked and z in the save-view snapshot', () => {
    const onSaveView = vi.fn();
    hoisted.seededNodes = [
      {
        id: 'g1',
        type: 'group',
        position: { x: 4, y: 5 },
        data: { label: 'Locked team', description: '', color: '#646cff', locked: true, z: 3 },
        style: { width: 300, height: 200 },
        draggable: false,
      },
    ];
    const { rerender } = render(
      <GraphCanvas nodes={[]} edges={[]} onSaveView={onSaveView} saveViewSignal={0} />
    );
    rerender(<GraphCanvas nodes={[]} edges={[]} onSaveView={onSaveView} saveViewSignal={1} />);
    expect(onSaveView).toHaveBeenCalled();
    // The autosave path. Omitting either field here is what the browser's next
    // save used to do, overwriting a lock nobody had touched.
    expect(onSaveView.mock.calls.at(-1)[0].groups[0]).toEqual(
      expect.objectContaining({ id: 'g1', locked: true, z: 3 })
    );
  });

  it('emits an unlocked group at the base layer when the canvas group has neither', () => {
    const onSaveView = vi.fn();
    hoisted.seededNodes = [
      {
        id: 'g1',
        type: 'group',
        position: { x: 0, y: 0 },
        data: { label: 'Team', description: '', color: '#646cff' },
        style: { width: 300, height: 200 },
      },
    ];
    const { rerender } = render(
      <GraphCanvas nodes={[]} edges={[]} onSaveView={onSaveView} saveViewSignal={0} />
    );
    rerender(<GraphCanvas nodes={[]} edges={[]} onSaveView={onSaveView} saveViewSignal={1} />);
    expect(onSaveView.mock.calls.at(-1)[0].groups[0]).toEqual(
      expect.objectContaining({ locked: false, z: 0 })
    );
  });
});
