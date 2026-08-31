import { describe, it, expect, vi } from 'vitest';

// GraphCanvas.jsx pulls in the full 'reactflow' package at module scope
// (ReactFlow itself plus several hooks) even though reorderNodesForParentChild
// touches none of it — it is a pure array transform. Mocked the same minimal
// way GroupLockRoundTrip.test.jsx already does to import and render the whole
// component; nothing here renders anything, so the mock only has to satisfy
// the module's own top-level `import ReactFlow, { ... } from 'reactflow'`.
vi.mock('reactflow', () => {
  const MockReactFlow = ({ children }) => children ?? null;
  return {
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => children ?? null,
    useNodesState: () => [[], vi.fn(), vi.fn()],
    useEdgesState: (initial) => [initial || [], vi.fn(), vi.fn()],
    useReactFlow: () => ({}),
    useOnSelectionChange: () => {},
    Background: () => null,
    Controls: () => null,
    MiniMap: () => null,
    NodeResizer: () => null,
    SelectionMode: { Partial: 'partial' },
  };
});

const { reorderNodesForParentChild } = await import('../src/components/GraphCanvas');

const group = (id, z, extra = {}) => ({ id, type: 'group', data: { z, ...extra } });
const member = (id, parentId, position) => ({ id, type: 'custom', parentId, position });
const freeNode = (id) => ({ id, type: 'custom' });

describe('reorderNodesForParentChild — group bucket ordering', () => {
  it('keeps every group ahead of every non-group node, whatever z each group carries', () => {
    const nodes = [freeNode('n1'), group('g1', 50), freeNode('n2'), group('g2', -50)];
    const result = reorderNodesForParentChild(nodes);
    const groupIndexes = result.flatMap((n, i) => (n.type === 'group' ? [i] : []));
    const nonGroupIndexes = result.flatMap((n, i) => (n.type !== 'group' ? [i] : []));
    expect(Math.max(...groupIndexes)).toBeLessThan(Math.min(...nonGroupIndexes));
  });

  it('orders groups against each other by data.z, ascending', () => {
    const nodes = [group('g1', 5), group('g2', -3), group('g3', 0)];
    const result = reorderNodesForParentChild(nodes);
    expect(result.map((n) => n.id)).toEqual(['g2', 'g3', 'g1']);
  });

  it('keeps the original relative order on a z tie (the common case: every group at the shared default 0)', () => {
    const nodes = [group('g1', 0), group('g2', 0), group('g3', 0)];
    expect(reorderNodesForParentChild(nodes).map((n) => n.id)).toEqual(['g1', 'g2', 'g3']);
    // Reversed input order must reverse the output too — this pins that the
    // tie-break is the input's own order, not an incidental id/insertion
    // sort that would happen to agree with the case above.
    const reversed = [group('g3', 0), group('g2', 0), group('g1', 0)];
    expect(reorderNodesForParentChild(reversed).map((n) => n.id)).toEqual(['g3', 'g2', 'g1']);
  });

  it('treats a group with no data.z as the shared default, 0', () => {
    const nodes = [{ id: 'g1', type: 'group' }, group('g2', -1)];
    expect(reorderNodesForParentChild(nodes).map((n) => n.id)).toEqual(['g2', 'g1']);
  });

  it('bringing one group forward moves it after the others in the array (paints on top of them)', () => {
    // Mirrors exactly what GroupNode's handleChangeGroupLayer does: bump the
    // target's data.z, then re-run reorderNodesForParentChild.
    const before = [group('g1', 0), group('g2', 0)];
    const afterBump = before.map((n) => (n.id === 'g1' ? { ...n, data: { ...n.data, z: 1 } } : n));
    expect(reorderNodesForParentChild(afterBump).map((n) => n.id)).toEqual(['g2', 'g1']);
  });
});

describe('reorderNodesForParentChild — member invariant under a group reorder', () => {
  // dec-annotation-group-background-layering / smallfix-group-annotation-has-
  // no-layer-control: reordering group backgrounds must never change member
  // order, member positions, or group membership. Two groups, each with a
  // member, reordered by exactly the write handleChangeGroupLayer makes
  // (bump the target group's data.z, then reorder) — every member's
  // position/parentId (and its own envelope, if it carries a z of its own)
  // must come out byte-identical to what went in.
  it('reordering two groups leaves every member position/z/parentId untouched', () => {
    const before = [
      group('g1', 0),
      group('g2', 0),
      member('m1', 'g1', { x: 10, y: 20 }),
      member('m2', 'g2', { x: 30, y: 40 }),
    ];
    const beforeMembers = before.filter((n) => n.type !== 'group');

    // The click: bring g2 to the front among groups (send g1 to the back
    // would be the mirror case) — exactly the write GroupNode's
    // handleChangeGroupLayer performs, via resolveGroupOrderZ('front') on g2.
    const bumped = before.map((n) => (n.id === 'g2' ? { ...n, data: { ...n.data, z: 1 } } : n));
    const after = reorderNodesForParentChild(bumped);

    // Group order changed — the reorder actually happened, so the invariant
    // check below is not vacuously true.
    const groupIdsAfter = after.filter((n) => n.type === 'group').map((n) => n.id);
    expect(groupIdsAfter).toEqual(['g1', 'g2']);

    const afterMembers = after.filter((n) => n.type !== 'group');
    expect(afterMembers).toHaveLength(beforeMembers.length);
    for (const beforeMember of beforeMembers) {
      const afterMember = afterMembers.find((n) => n.id === beforeMember.id);
      expect(afterMember).toBeDefined();
      expect(afterMember.parentId).toBe(beforeMember.parentId);
      expect(afterMember.position).toEqual(beforeMember.position);
      // The whole member node is otherwise untouched too — no field this
      // function does not own (z included, had one been set) was rewritten.
      expect(afterMember).toEqual(beforeMember);
    }
  });

  it('reordering three groups with members leaves membership (parentId) and geometry alone even as paint order fully reverses', () => {
    const before = [
      group('g1', -5),
      group('g2', 0),
      group('g3', 5),
      member('m1', 'g1', { x: 1, y: 1 }),
      member('m2', 'g2', { x: 2, y: 2 }),
      member('m3', 'g3', { x: 3, y: 3 }),
    ];
    const beforeMembers = before.filter((n) => n.type !== 'group');

    // Reverse every group's z, fully reversing paint order — the largest
    // shake-up a sequence of front/back clicks could produce.
    const reversedZ = before.map((n) => {
      if (n.id === 'g1') return { ...n, data: { ...n.data, z: 5 } };
      if (n.id === 'g3') return { ...n, data: { ...n.data, z: -5 } };
      return n;
    });
    const after = reorderNodesForParentChild(reversedZ);

    expect(after.filter((n) => n.type === 'group').map((n) => n.id)).toEqual(['g3', 'g2', 'g1']);

    const afterMembers = after.filter((n) => n.type !== 'group');
    for (const beforeMember of beforeMembers) {
      const afterMember = afterMembers.find((n) => n.id === beforeMember.id);
      expect(afterMember).toEqual(beforeMember);
    }
  });
});
