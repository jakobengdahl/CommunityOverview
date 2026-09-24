import { describe, it, expect, vi } from 'vitest';

// GraphCanvas.jsx pulls in the full 'reactflow' package at module scope
// (ReactFlow itself plus several hooks) even though computeGroupPlacement
// touches none of it — it is a pure placement computation. Mocked the same minimal
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

const { computeGroupPlacement, indexGroupsById } = await import('../src/components/GraphCanvas');

// The pre-Map implementation, frozen verbatim as an oracle: the Map lookup is
// a pure performance refactor, so every input must place exactly as it did.
function computeGroupPlacementBeforeMap(node, currentNodes, groupNodes) {
  const flowNode = currentNodes.find((cn) => cn.id === node.id);
  const pos = flowNode?.position || node.position;

  const absPos = node.parentId
    ? {
        x: pos.x + (groupNodes.find((g) => g.id === node.parentId)?.position.x || 0),
        y: pos.y + (groupNodes.find((g) => g.id === node.parentId)?.position.y || 0),
      }
    : pos;

  let targetGroup = null;
  for (const g of groupNodes) {
    const gb = {
      left: g.position.x,
      right: g.position.x + (g.style?.width || 300),
      top: g.position.y,
      bottom: g.position.y + (g.style?.height || 200),
    };
    if (
      absPos.x >= gb.left &&
      absPos.x <= gb.right &&
      absPos.y >= gb.top &&
      absPos.y <= gb.bottom
    ) {
      targetGroup = g;
      break;
    }
  }

  if (targetGroup && node.parentId !== targetGroup.id) {
    return {
      parentId: targetGroup.id,
      position: { x: absPos.x - targetGroup.position.x, y: absPos.y - targetGroup.position.y },
    };
  }

  if (!targetGroup && node.parentId) {
    const oldParent = groupNodes.find((gn) => gn.id === node.parentId);
    return {
      parentId: undefined,
      position: {
        x: pos.x + (oldParent?.position.x || 0),
        y: pos.y + (oldParent?.position.y || 0),
      },
    };
  }

  return { parentId: node.parentId, position: { x: pos.x, y: pos.y } };
}

const group = (id, x, y, width, height) => ({
  id,
  type: 'group',
  position: { x, y },
  ...(width ? { style: { width, height } } : {}),
});
const node = (id, x, y, parentId) => ({
  id,
  type: 'custom',
  position: { x, y },
  ...(parentId ? { parentId } : {}),
});

function place(n, currentNodes) {
  const groupNodes = currentNodes.filter((cn) => cn.type === 'group');
  return computeGroupPlacement(n, currentNodes, groupNodes, indexGroupsById(groupNodes));
}

function placeBefore(n, currentNodes) {
  const groupNodes = currentNodes.filter((cn) => cn.type === 'group');
  return computeGroupPlacementBeforeMap(n, currentNodes, groupNodes);
}

describe('computeGroupPlacement', () => {
  it('re-parents a free node dropped inside a group, relative to the group origin', () => {
    const g = group('g1', 100, 100);
    const n = node('n1', 150, 130);
    expect(place(n, [g, n])).toEqual({ parentId: 'g1', position: { x: 50, y: 30 } });
  });

  it('resolves a member node to absolute coordinates through its parent when it leaves', () => {
    const g = group('g1', 100, 100);
    const n = node('n1', 900, 900, 'g1');
    expect(place(n, [g, n])).toEqual({ parentId: undefined, position: { x: 1000, y: 1000 } });
  });

  it('moves a member between groups using its old parent to reach absolute space', () => {
    const g1 = group('g1', 0, 0, 100, 100);
    const g2 = group('g2', 500, 500);
    const n = node('n1', 520, 540, 'g1');
    expect(place(n, [g1, g2, n])).toEqual({ parentId: 'g2', position: { x: 20, y: 40 } });
  });

  it('keeps a member in place when it is dragged within its own group', () => {
    const g = group('g1', 100, 100);
    const n = node('n1', 10, 20, 'g1');
    expect(place(n, [g, n])).toEqual({ parentId: 'g1', position: { x: 10, y: 20 } });
  });

  it('treats a parentId naming no known group as an origin-anchored parent', () => {
    const g = group('g1', 100, 100);
    const n = node('n1', 900, 900, 'missing');
    expect(place(n, [g, n])).toEqual({ parentId: undefined, position: { x: 900, y: 900 } });
  });

  it('resolves a duplicated group id to its first occurrence, as Array.find did', () => {
    const first = group('dup', 100, 100);
    const second = group('dup', 5000, 5000);
    const n = node('n1', 2000, 2000, 'dup');
    const nodes = [first, second, n];
    expect(indexGroupsById([first, second]).get('dup')).toBe(first);
    expect(place(n, nodes)).toEqual({ parentId: undefined, position: { x: 2100, y: 2100 } });
    expect(place(n, nodes)).toEqual(placeBefore(n, nodes));
  });

  it('places every node of a generated canvas exactly as the pre-Map implementation', () => {
    // Deterministic LCG so a failure reproduces; no Math.random in the oracle run.
    let seed = 20260924;
    const rand = (max) => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      // High bits: an LCG's low bits cycle with a short period.
      return Math.floor((seed / 2147483648) * max);
    };

    const outcomes = { enter: 0, exit: 0, stay: 0 };
    for (let round = 0; round < 50; round += 1) {
      const groupCount = 1 + rand(8);
      const groups = Array.from({ length: groupCount }, (_, i) =>
        group(
          `g${rand(groupCount + 2)}-${i % 3 === 0 ? 'dup' : i}`,
          rand(2000) - 500,
          rand(2000) - 500,
          rand(2) ? 100 + rand(600) : undefined,
          200 + rand(400)
        )
      );
      const parentPool = [...groups.map((g) => g.id), 'missing', undefined, undefined];
      const members = Array.from({ length: 30 }, (_, i) =>
        node(`n${i}`, rand(2400) - 600, rand(2400) - 600, parentPool[rand(parentPool.length)])
      );
      const currentNodes = [...groups, ...members];

      for (const n of members) {
        const placed = place(n, currentNodes);
        expect(placed).toEqual(placeBefore(n, currentNodes));
        if (placed.parentId === n.parentId) outcomes.stay += 1;
        else if (placed.parentId) outcomes.enter += 1;
        else outcomes.exit += 1;
      }
    }
    // Guards the sweep itself: it must reach every branch to prove anything.
    expect(outcomes.enter).toBeGreaterThan(0);
    expect(outcomes.exit).toBeGreaterThan(0);
    expect(outcomes.stay).toBeGreaterThan(0);
  });
});
