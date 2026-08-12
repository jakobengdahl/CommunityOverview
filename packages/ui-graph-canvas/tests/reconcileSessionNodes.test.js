import { describe, it, expect } from 'vitest';
import { reconcileSessionNodes } from '../src/utils/graphLayout';

// A React Flow node as GraphCanvas builds it: `position` is the position it will
// render at, and `data._savedPosition` marks a position loaded from the server
// (authoritative). Helper to keep the fixtures terse.
const rfNode = (id, position, savedPosition = null) => ({
  id,
  type: 'custom',
  position,
  data: savedPosition ? { _savedPosition: savedPosition } : {},
});

describe('reconcileSessionNodes — in-session updates', () => {
  it('keeps a node at its live (dragged) position across an incremental update', () => {
    // Node was dragged to (500, 300); an unrelated update (e.g. highlight) then
    // re-sends it with its stored position. The live position must win.
    const prev = [rfNode('a', { x: 500, y: 300 }, { x: 10, y: 10 })];
    const incoming = [rfNode('a', { x: 10, y: 10 }, { x: 10, y: 10 })];

    const [result] = reconcileSessionNodes({
      prevNodes: prev,
      incomingNodes: incoming,
      sessionChanged: false,
    });

    expect(result.position).toEqual({ x: 500, y: 300 });
  });
});

describe('reconcileSessionNodes — session switch', () => {
  it("uses the new session's saved position for a node shared between sessions", () => {
    // Node "a" sits at (500, 300) in the session being left, and at (20, 40) in
    // the session being opened. After the switch it must land at the new
    // session's coordinates, not the old ones (the reported bug).
    const prev = [rfNode('a', { x: 500, y: 300 }, { x: 500, y: 300 })];
    const incoming = [rfNode('a', { x: 20, y: 40 }, { x: 20, y: 40 })];

    const [result] = reconcileSessionNodes({
      prevNodes: prev,
      incomingNodes: incoming,
      sessionChanged: true,
    });

    expect(result.position).toEqual({ x: 20, y: 40 });
  });

  it('would leak the old position without the session-switch flag (guards the fix)', () => {
    const prev = [rfNode('a', { x: 500, y: 300 }, { x: 500, y: 300 })];
    const incoming = [rfNode('a', { x: 20, y: 40 }, { x: 20, y: 40 })];

    const [leaked] = reconcileSessionNodes({
      prevNodes: prev,
      incomingNodes: incoming,
      sessionChanged: false,
    });

    // Without the flag the stale coordinates survive — this is exactly what the
    // switch path must avoid, and asserting it here pins the flag's purpose.
    expect(leaked.position).toEqual({ x: 500, y: 300 });
  });

  it("does not anchor a switched-in node to the previous session's layout", () => {
    // Disjoint sessions: none of the incoming nodes were present before. On a
    // switch they must keep their own saved positions, never be re-placed
    // relative to the session just left.
    const prev = [rfNode('old', { x: 900, y: 900 }, { x: 900, y: 900 })];
    const incoming = [
      rfNode('x', { x: 0, y: 0 }, { x: 0, y: 0 }),
      rfNode('y', { x: 100, y: 100 }, { x: 100, y: 100 }),
    ];

    const result = reconcileSessionNodes({
      prevNodes: prev,
      incomingNodes: incoming,
      sessionChanged: true,
    });

    expect(result.map((n) => n.position)).toEqual([
      { x: 0, y: 0 },
      { x: 100, y: 100 },
    ]);
  });
});
