import { describe, it, expect } from 'vitest';
import { MarkerType } from 'reactflow';
import {
  resolveEdgeVisuals,
  DEFAULT_EDGE_STYLE,
  EDGE_MIN_THICKNESS,
  EDGE_MAX_THICKNESS,
} from '../src/utils/constants';

describe('resolveEdgeVisuals', () => {
  it('reproduces the historical default look for edges without visual metadata', () => {
    for (const meta of [undefined, null, {}, 'nonsense', 42]) {
      const v = resolveEdgeVisuals(meta);
      expect(v.style).toEqual({
        stroke: DEFAULT_EDGE_STYLE.stroke,
        strokeWidth: DEFAULT_EDGE_STYLE.strokeWidth,
      });
      expect(v.markerStart).toBeUndefined();
      expect(v.markerEnd).toBeUndefined();
      expect(v.animated).toBe(false);
      expect(v.className).toBeUndefined();
    }
  });

  it('applies a custom stroke colour to the line and to the arrowheads', () => {
    const v = resolveEdgeVisuals({ color: '#ff0000', direction: 'both' });
    expect(v.style.stroke).toBe('#ff0000');
    expect(v.markerEnd.color).toBe('#ff0000');
    expect(v.markerStart.color).toBe('#ff0000');
  });

  it('ignores blank/non-string colours and falls back to the default', () => {
    expect(resolveEdgeVisuals({ color: '   ' }).style.stroke).toBe(DEFAULT_EDGE_STYLE.stroke);
    expect(resolveEdgeVisuals({ color: 123 }).style.stroke).toBe(DEFAULT_EDGE_STYLE.stroke);
  });

  it('clamps thickness into the allowed range and ignores invalid values', () => {
    expect(resolveEdgeVisuals({ thickness: 6 }).style.strokeWidth).toBe(6);
    expect(resolveEdgeVisuals({ thickness: 999 }).style.strokeWidth).toBe(EDGE_MAX_THICKNESS);
    expect(resolveEdgeVisuals({ thickness: 0 }).style.strokeWidth).toBe(
      DEFAULT_EDGE_STYLE.strokeWidth
    );
    expect(resolveEdgeVisuals({ thickness: -3 }).style.strokeWidth).toBe(
      DEFAULT_EDGE_STYLE.strokeWidth
    );
    expect(resolveEdgeVisuals({ thickness: 0.2 }).style.strokeWidth).toBe(EDGE_MIN_THICKNESS);
    expect(resolveEdgeVisuals({ thickness: 'wide' }).style.strokeWidth).toBe(
      DEFAULT_EDGE_STYLE.strokeWidth
    );
  });

  it('maps direction to the correct arrowhead ends', () => {
    const forward = resolveEdgeVisuals({ direction: 'forward' });
    expect(forward.markerEnd).toBeTruthy();
    expect(forward.markerStart).toBeUndefined();

    const backward = resolveEdgeVisuals({ direction: 'backward' });
    expect(backward.markerStart).toBeTruthy();
    expect(backward.markerEnd).toBeUndefined();

    const both = resolveEdgeVisuals({ direction: 'both' });
    expect(both.markerStart).toBeTruthy();
    expect(both.markerEnd).toBeTruthy();

    const none = resolveEdgeVisuals({ direction: 'none' });
    expect(none.markerStart).toBeUndefined();
    expect(none.markerEnd).toBeUndefined();
  });

  it('is case-insensitive and tolerant of surrounding whitespace on direction', () => {
    const v = resolveEdgeVisuals({ direction: '  Forward ' });
    expect(v.markerEnd).toBeTruthy();
    expect(v.markerStart).toBeUndefined();
  });

  it('selects the arrowhead style from the arrow attribute', () => {
    expect(resolveEdgeVisuals({ direction: 'forward', arrow: 'open' }).markerEnd.type).toBe(
      MarkerType.Arrow
    );
    expect(resolveEdgeVisuals({ direction: 'forward', arrow: 'closed' }).markerEnd.type).toBe(
      MarkerType.ArrowClosed
    );
    // Unknown/absent arrow style defaults to the filled (closed) head.
    expect(resolveEdgeVisuals({ direction: 'forward' }).markerEnd.type).toBe(
      MarkerType.ArrowClosed
    );
  });

  it('treats animated and pulse as the animated ("pulse") state', () => {
    for (const meta of [{ animated: true }, { pulse: true }]) {
      const v = resolveEdgeVisuals(meta);
      expect(v.animated).toBe(true);
      expect(v.className).toBe('rf-edge-pulse');
    }
    const staticEdge = resolveEdgeVisuals({ animated: false });
    expect(staticEdge.animated).toBe(false);
    expect(staticEdge.className).toBeUndefined();
  });
});
