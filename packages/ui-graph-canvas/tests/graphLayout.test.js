import { describe, it, expect } from 'vitest';
import {
  arrangeNodes,
  positionNewNodes,
  alignNodes,
  distributeNodes,
} from '../src/utils/graphLayout';

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

describe('alignNodes', () => {
  const boxes = [
    { id: 'a', position: { x: 0, y: 0 }, width: 100, height: 50 },
    { id: 'b', position: { x: 200, y: 100 }, width: 50, height: 20 },
    { id: 'c', position: { x: 50, y: 300 }, width: 200, height: 10 },
  ];

  it('returns an empty map below 2 nodes', () => {
    expect(alignNodes([boxes[0]], 'left').size).toBe(0);
    expect(alignNodes([], 'left').size).toBe(0);
  });

  it('aligns left edges to the minimum left edge', () => {
    const result = alignNodes(boxes, 'left');
    expect(result.get('a')).toEqual({ x: 0, y: 0 });
    expect(result.get('b')).toEqual({ x: 0, y: 100 });
    expect(result.get('c')).toEqual({ x: 0, y: 300 });
  });

  it('aligns right edges to the maximum right edge', () => {
    // Right edges: a=100, b=250, c=250 -> target 250.
    const result = alignNodes(boxes, 'right');
    expect(result.get('a')).toEqual({ x: 150, y: 0 });
    expect(result.get('b')).toEqual({ x: 200, y: 100 });
    expect(result.get('c')).toEqual({ x: 50, y: 300 });
  });

  it('aligns horizontal centers to the midpoint of the overall extent', () => {
    // Overall extent: minLeft=0, maxRight=250 -> centre 125.
    const result = alignNodes(boxes, 'centerX');
    expect(result.get('a')).toEqual({ x: 75, y: 0 });
    expect(result.get('b')).toEqual({ x: 100, y: 100 });
    expect(result.get('c')).toEqual({ x: 25, y: 300 });
  });

  it('aligns top edges to the minimum top edge', () => {
    const result = alignNodes(boxes, 'top');
    expect(result.get('a')).toEqual({ x: 0, y: 0 });
    expect(result.get('b')).toEqual({ x: 200, y: 0 });
    expect(result.get('c')).toEqual({ x: 50, y: 0 });
  });

  it('aligns bottom edges to the maximum bottom edge', () => {
    // Bottom edges: a=50, b=120, c=310 -> target 310.
    const result = alignNodes(boxes, 'bottom');
    expect(result.get('a')).toEqual({ x: 0, y: 260 });
    expect(result.get('b')).toEqual({ x: 200, y: 290 });
    expect(result.get('c')).toEqual({ x: 50, y: 300 });
  });

  it('aligns vertical middles to the midpoint of the overall extent', () => {
    // Overall extent: minTop=0, maxBottom=310 -> centre 155.
    const result = alignNodes(boxes, 'centerY');
    expect(result.get('a')).toEqual({ x: 0, y: 130 });
    expect(result.get('b')).toEqual({ x: 200, y: 145 });
    expect(result.get('c')).toEqual({ x: 50, y: 150 });
  });

  it('treats a node with no measured size as a zero-width/height point', () => {
    const points = [
      { id: 'p1', position: { x: 10, y: 10 } },
      { id: 'p2', position: { x: 90, y: 40 } },
    ];
    const result = alignNodes(points, 'left');
    expect(result.get('p1')).toEqual({ x: 10, y: 10 });
    expect(result.get('p2')).toEqual({ x: 10, y: 40 });
  });
});

describe('distributeNodes', () => {
  it('returns an empty map below 3 nodes', () => {
    const two = [
      { id: 'a', position: { x: 0, y: 0 }, width: 100, height: 100 },
      { id: 'b', position: { x: 500, y: 0 }, width: 100, height: 100 },
    ];
    expect(distributeNodes(two, 'horizontal').size).toBe(0);
    expect(distributeNodes([], 'horizontal').size).toBe(0);
  });

  it('spreads three boxes with equal horizontal gaps, keeping the outer two fixed', () => {
    const boxes = [
      { id: 'a', position: { x: 0, y: 5 }, width: 100, height: 10 },
      { id: 'b', position: { x: 1000, y: 5 }, width: 100, height: 10 },
      { id: 'c', position: { x: 400, y: 5 }, width: 100, height: 10 },
    ];
    const result = distributeNodes(boxes, 'horizontal');
    // Outer two (by centre) keep their x; the middle one moves to close the
    // gap to exactly what the outer two's span allows.
    expect(result.get('a').x).toBe(0);
    expect(result.get('b').x).toBe(1000);
    expect(result.get('c').x).toBe(500);
    // y is untouched by a horizontal distribute.
    expect(result.get('a').y).toBe(5);
    expect(result.get('c').y).toBe(5);
    // Equal-gap invariant: gap(a→c) === gap(c→b).
    const gapAC = result.get('c').x - (result.get('a').x + 100);
    const gapCB = result.get('b').x - (result.get('c').x + 100);
    expect(gapAC).toBeCloseTo(gapCB, 9);
  });

  it('spreads boxes with equal vertical gaps, keeping the outer two fixed', () => {
    const boxes = [
      { id: 'a', position: { x: 5, y: 0 }, width: 10, height: 100 },
      { id: 'b', position: { x: 5, y: 900 }, width: 10, height: 50 },
      { id: 'c', position: { x: 5, y: 300 }, width: 10, height: 20 },
    ];
    const result = distributeNodes(boxes, 'vertical');
    expect(result.get('a').y).toBe(0);
    expect(result.get('b').y).toBe(900);
    // x is untouched by a vertical distribute.
    expect(result.get('a').x).toBe(5);
    const gapAC = result.get('c').y - (result.get('a').y + 100);
    const gapCB = result.get('b').y - (result.get('c').y + 20);
    expect(gapAC).toBeCloseTo(gapCB, 9);
  });

  it('orders by bounding-box centre, not by input array order or raw position', () => {
    // 'far' sits at a smaller x than 'near', but its centre (accounting for
    // its own width) is further right — distribution must follow centres.
    const boxes = [
      { id: 'near', position: { x: 100, y: 0 }, width: 20, height: 10 },
      { id: 'mid', position: { x: 500, y: 0 }, width: 20, height: 10 },
      { id: 'far', position: { x: 0, y: 0 }, width: 300, height: 10 }, // centre 150
    ];
    const result = distributeNodes(boxes, 'horizontal');
    // 'far' (centre 150) sorts after 'near' (centre 110), so 'near' — not
    // 'far' — is the fixed left endpoint despite 'far' having the smaller x.
    expect(result.get('near').x).toBe(100);
    expect(result.get('mid').x).toBe(500);
    expect(result.get('far').x).toBe(160);
  });
});
