import { describe, it, expect } from 'vitest';
import { resolveRotatedResizeGeometry } from '../src/utils/annotations';

// smallfix-annotation-rotated-resize-handles: NodeResizer's own math treats
// the resized box as axis-aligned. This pins the correction that keeps the
// dragged handle's *local* opposite corner fixed (not its global one) so a
// rotated note/frame/shape/image grows along its own axes.
describe('resolveRotatedResizeGeometry', () => {
  it('matches plain axis-aligned resize math when rotation is 0', () => {
    // Growing the bottom-right corner of an unrotated 100x50 box to 150x80:
    // the top-left corner (x, y) must stay put, exactly like NodeResizer's
    // own un-inverted case.
    const start = { x: 10, y: 20, width: 100, height: 50 };
    const end = { x: 10, y: 20, width: 150, height: 80 };
    const result = resolveRotatedResizeGeometry({ start, end, rotation: 0 });
    expect(result).toEqual({ x: 10, y: 20, width: 150, height: 80 });
  });

  it('matches plain axis-aligned resize math for an inverted (top-left) handle at 0 rotation', () => {
    // Dragging the top-left handle of a 100x50 box at (10,20), growing it to
    // 150x80: NodeResizer keeps the *bottom-right* corner (110,70) fixed and
    // reports x = startX - (width - startWidth), y likewise - exactly what
    // `end` holds here. At rotation 0 the function must reproduce that same
    // position unchanged (its rotation term is the identity).
    const start = { x: 10, y: 20, width: 100, height: 50 };
    const end = { x: -40, y: -10, width: 150, height: 80 };
    const result = resolveRotatedResizeGeometry({ start, end, rotation: 0 });
    expect(result).toEqual({ x: -40, y: -10, width: 150, height: 80 });
  });

  it('keeps the local anchor corner fixed on screen for a 180deg rotated box', () => {
    // A 100x50 box at (0,0), rotated 180deg about its centre (50,25): its
    // local top-left corner sits at the *global* point (100,50). Growing the
    // box (as if dragging its local bottom-right handle, non-inverted) to
    // 150x80 must keep that global point fixed, which means the box's
    // top-left (x, y) shifts up-left by exactly the growth (180deg has no
    // floating-point cos/sin error, so this is an exact check).
    const start = { x: 0, y: 0, width: 100, height: 50 };
    const end = { x: 0, y: 0, width: 150, height: 80 };
    const result = resolveRotatedResizeGeometry({ start, end, rotation: 180 });
    expect(result).toEqual({ x: -50, y: -30, width: 150, height: 80 });
  });

  it('moves the box off its unrotated position for a 90deg rotated corner drag', () => {
    const start = { x: 0, y: 0, width: 100, height: 50 };
    const end = { x: 0, y: 0, width: 150, height: 80 };
    const result = resolveRotatedResizeGeometry({ start, end, rotation: 90 });
    // Computed by hand (see the function's doc comment for the derivation):
    // the box's centre must move so the local anchor corner - which is no
    // longer at global top-left once rotated - stays fixed on screen.
    expect(result.x).toBeCloseTo(-40, 9);
    expect(result.y).toBeCloseTo(10, 9);
    expect(result.width).toBe(150);
    expect(result.height).toBe(80);
    // And this must differ from the (wrong) unrotated answer NodeResizer
    // would have applied on its own - otherwise the fix is a no-op.
    expect(result.x).not.toBeCloseTo(0, 6);
  });

  it('is a no-op when the gesture produced no net size or position change', () => {
    const start = { x: 5, y: 5, width: 100, height: 50 };
    const result = resolveRotatedResizeGeometry({ start, end: { ...start }, rotation: 37 });
    expect(result.x).toBeCloseTo(5, 9);
    expect(result.y).toBeCloseTo(5, 9);
    expect(result.width).toBe(100);
    expect(result.height).toBe(50);
  });

  it('handles a single-axis (edge-handle) resize the same way regardless of rotation', () => {
    // A pure width-only change (top/bottom position never moves) must not be
    // perturbed by the sign convention picked for the axis that did not
    // change - the height terms cancel identically old vs new.
    const start = { x: 0, y: 0, width: 100, height: 50 };
    const end = { x: 0, y: 0, width: 160, height: 50 };
    const result = resolveRotatedResizeGeometry({ start, end, rotation: 45 });
    expect(result.width).toBe(160);
    expect(result.height).toBe(50);
  });
});
