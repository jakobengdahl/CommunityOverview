import { describe, it, expect, vi } from 'vitest';
import { applyIngestedImageOptimistically } from './imageIngestApply.js';

function makeDedup() {
  return { markApplied: vi.fn(), shouldSkip: vi.fn() };
}

describe('applyIngestedImageOptimistically', () => {
  it('applies the annotation, folds it into the sync baseline, and marks it — in that order', () => {
    const applyRemoteOp = vi.fn();
    const foldLocalOp = vi.fn();
    const dedup = makeDedup();
    const annotation = { id: 'ann-1', type: 'image' };

    applyIngestedImageOptimistically({ annotation, applyRemoteOp, foldLocalOp, dedup });

    const expectedOp = { op: 'annotation_created', annotation };
    expect(applyRemoteOp).toHaveBeenCalledWith(expectedOp);
    expect(foldLocalOp).toHaveBeenCalledWith(expectedOp);
    expect(dedup.markApplied).toHaveBeenCalledWith('ann-1');

    // applyRemoteOp must run before markApplied: this call's own dedup check
    // (inside applyRemoteOp, in the real App.jsx wiring) must never see its
    // own id as already marked.
    const applyOrder = applyRemoteOp.mock.invocationCallOrder[0];
    const markOrder = dedup.markApplied.mock.invocationCallOrder[0];
    expect(applyOrder).toBeLessThan(markOrder);
  });

  it('does nothing when the annotation has no id', () => {
    const applyRemoteOp = vi.fn();
    const foldLocalOp = vi.fn();
    const dedup = makeDedup();

    applyIngestedImageOptimistically({ annotation: null, applyRemoteOp, foldLocalOp, dedup });
    applyIngestedImageOptimistically({ annotation: {}, applyRemoteOp, foldLocalOp, dedup });

    expect(applyRemoteOp).not.toHaveBeenCalled();
    expect(foldLocalOp).not.toHaveBeenCalled();
    expect(dedup.markApplied).not.toHaveBeenCalled();
  });

  it('works without a sync client connected (foldLocalOp omitted)', () => {
    const applyRemoteOp = vi.fn();
    const dedup = makeDedup();
    const annotation = { id: 'ann-2', type: 'image' };

    expect(() =>
      applyIngestedImageOptimistically({ annotation, applyRemoteOp, foldLocalOp: undefined, dedup })
    ).not.toThrow();
    expect(applyRemoteOp).toHaveBeenCalledWith({ op: 'annotation_created', annotation });
    expect(dedup.markApplied).toHaveBeenCalledWith('ann-2');
  });
});
