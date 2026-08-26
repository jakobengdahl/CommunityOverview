import { describe, it, expect } from 'vitest';
import { MarkerType } from 'reactflow';
import {
  resolveEdgeVisuals,
  resolveEdgeOpacity,
  DEFAULT_EDGE_STYLE,
  EDGE_MIN_THICKNESS,
  EDGE_MAX_THICKNESS,
  DIMMED_EDGE_OPACITY_CEILING,
  ACCESSIBLE_MIN_EDGE_OPACITY,
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

// task-session-focus-dimming-controls: a dimmed edge always composes below
// the session's global edge-intensity baseline, never at an independent
// fixed opacity — raising the baseline back up must not outshine a
// deliberately-dimmed edge.
describe('resolveEdgeOpacity', () => {
  it('renders a non-dimmed edge at the global intensity baseline', () => {
    expect(resolveEdgeOpacity(1, false)).toBe(1);
    expect(resolveEdgeOpacity(0.6, false)).toBe(0.6);
  });

  it('caps a dimmed edge at the dimmed ceiling even at full intensity', () => {
    expect(resolveEdgeOpacity(1, true)).toBe(DIMMED_EDGE_OPACITY_CEILING);
  });

  it('keeps a dimmed edge at or below the baseline, never above it', () => {
    // Above the ceiling, dimming still tracks (caps at) the baseline.
    expect(resolveEdgeOpacity(0.9, true)).toBeLessThan(0.9);
    expect(resolveEdgeOpacity(0.9, true)).toBe(DIMMED_EDGE_OPACITY_CEILING);
    // Between the accessible floor and the ceiling, dimming equals the
    // (already-low) baseline rather than reducing it further.
    const midIntensity = 0.18;
    expect(resolveEdgeOpacity(midIntensity, true)).toBe(midIntensity);
  });

  it('never returns an opacity below the accessibility floor', () => {
    expect(resolveEdgeOpacity(0, true)).toBe(ACCESSIBLE_MIN_EDGE_OPACITY);
    expect(resolveEdgeOpacity(0, false)).toBe(0);
  });

  it('clamps an out-of-range or non-finite intensity to the [0,1] baseline', () => {
    expect(resolveEdgeOpacity(5, false)).toBe(1);
    expect(resolveEdgeOpacity(-2, false)).toBe(0);
    expect(resolveEdgeOpacity(NaN, false)).toBe(1);
    expect(resolveEdgeOpacity(undefined, false)).toBe(1);
  });
});
