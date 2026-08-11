import { describe, it, expect } from 'vitest';
import { arrangeNodes, positionNewNodes } from '../src/utils/graphLayout';

describe('positionNewNodes — incremental placement', () => {
  it('places a new connected node near its existing neighbour, not at the origin', () => {
    const existing = [{ id: 'a', position: { x: 1000, y: 1000 }, data: { type: 'Actor' } }];
    const fresh = [{ id: 'b', position: { x: 0, y: 0 }, data: { type: 'Theme' } }];
    const edges = [{ id: 'e', source: 'a', target: 'b' }];

    const [placed] = positionNewNodes(fresh, existing, edges);

    // Near the neighbour (within a couple of node cells), and clearly not the
    // default origin (~400,300) it would fall back to without an anchor.
    const dist = Math.hypot(placed.position.x - 1000, placed.position.y - 1000);
    expect(dist).toBeLessThan(500);
    expect(placed.position).not.toEqual({ x: 0, y: 0 });
  });
});

describe('arrangeNodes', () => {
  const nodes = [
    { id: 'a', position: { x: 0, y: 0 } },
    { id: 'b', position: { x: 300, y: 0 } },
    { id: 'c', position: { x: 0, y: 300 } },
  ];

  it('returns a position for each node keyed by id', () => {
    const result = arrangeNodes(nodes, [], 'cluster');
    expect(result.size).toBe(3);
    expect(result.has('a')).toBe(true);
  });

  it('lays nodes out in a single row for horizontal mode', () => {
    const result = arrangeNodes(nodes, [], 'horizontal');
    const ys = [...result.values()].map((p) => p.y);
    expect(new Set(ys).size).toBe(1); // all share one row
    const xs = [...result.values()].map((p) => p.x).sort((m, n) => m - n);
    expect(xs[0]).toBeLessThan(xs[2]);
  });

  it('lays nodes out in a single column for vertical mode', () => {
    const result = arrangeNodes(nodes, [], 'vertical');
    const xs = [...result.values()].map((p) => p.x);
    expect(new Set(xs).size).toBe(1); // all share one column
  });

  it('keeps the arrangement centred on the selection centroid', () => {
    const result = arrangeNodes(nodes, [], 'horizontal');
    const centroid = { x: (0 + 300 + 0) / 3, y: (0 + 0 + 300) / 3 };
    const xs = [...result.values()].map((p) => p.x);
    const ys = [...result.values()].map((p) => p.y);
    const center = {
      x: (Math.min(...xs) + Math.max(...xs)) / 2,
      y: (Math.min(...ys) + Math.max(...ys)) / 2,
    };
    expect(center.x).toBeCloseTo(centroid.x, 5);
    expect(center.y).toBeCloseTo(centroid.y, 5);
  });

  it('returns an empty map for an empty selection', () => {
    expect(arrangeNodes([], [], 'cluster').size).toBe(0);
  });

  describe('tidy (auto-tidy) mode', () => {
    it('groups nodes by type into one column per type when there is no hierarchy', () => {
      const typed = [
        { id: 'g1', position: { x: 0, y: 0 }, data: { nodeType: 'Goal' } },
        { id: 'g2', position: { x: 500, y: 500 }, data: { nodeType: 'Goal' } },
        { id: 't1', position: { x: 900, y: 100 }, data: { nodeType: 'Task' } },
      ];
      const result = arrangeNodes(typed, [], 'tidy');
      expect(result.size).toBe(3);
      // The two Goals share a column (same x); the Task sits in a different column.
      expect(result.get('g1').x).toBeCloseTo(result.get('g2').x, 5);
      expect(result.get('t1').x).not.toBeCloseTo(result.get('g1').x, 5);
      // Same-type nodes are stacked on distinct rows, so nothing overlaps.
      expect(result.get('g1').y).not.toBeCloseTo(result.get('g2').y, 5);
    });

    it('lays out a hierarchical selection as a tree using internal edges', () => {
      const hierNodes = [
        { id: 'root', position: { x: 0, y: 0 }, data: { nodeType: 'Goal' } },
        { id: 'child1', position: { x: 400, y: 0 }, data: { nodeType: 'Task' } },
        { id: 'child2', position: { x: 800, y: 0 }, data: { nodeType: 'Task' } },
      ];
      const edges = [
        { source: 'root', target: 'child1' },
        { source: 'root', target: 'child2' },
      ];
      const result = arrangeNodes(hierNodes, edges, 'tidy');
      expect(result.size).toBe(3);
      // Tree (TB) puts the root above its children.
      expect(result.get('root').y).toBeLessThan(result.get('child1').y);
      expect(result.get('root').y).toBeLessThan(result.get('child2').y);
    });

    it('keeps the tidied arrangement centred on the selection centroid', () => {
      const typed = [
        { id: 'a', position: { x: 0, y: 0 }, data: { nodeType: 'Goal' } },
        { id: 'b', position: { x: 300, y: 0 }, data: { nodeType: 'Task' } },
        { id: 'c', position: { x: 0, y: 300 }, data: { nodeType: 'Risk' } },
      ];
      const result = arrangeNodes(typed, [], 'tidy');
      const centroid = { x: (0 + 300 + 0) / 3, y: (0 + 0 + 300) / 3 };
      const xs = [...result.values()].map((p) => p.x);
      const ys = [...result.values()].map((p) => p.y);
      const center = {
        x: (Math.min(...xs) + Math.max(...xs)) / 2,
        y: (Math.min(...ys) + Math.max(...ys)) / 2,
      };
      expect(center.x).toBeCloseTo(centroid.x, 5);
      expect(center.y).toBeCloseTo(centroid.y, 5);
    });
  });
});
