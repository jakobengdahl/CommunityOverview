import { describe, it, expect, vi } from 'vitest';
import { applyIngestedImageOptimistically } from './imageIngestApply.js';

describe('applyIngestedImageOptimistically', () => {
  it('applies the annotation and folds it into the sync baseline', () => {
    const applyRemoteOp = vi.fn();
    const foldLocalOp = vi.fn();
    const annotation = { id: 'ann-1', type: 'image' };

    applyIngestedImageOptimistically({ annotation, applyRemoteOp, foldLocalOp });

    const expectedOp = { op: 'annotation_created', annotation };
    expect(applyRemoteOp).toHaveBeenCalledWith(expectedOp);
    expect(foldLocalOp).toHaveBeenCalledWith(expectedOp);
  });

  it('does nothing when the annotation has no id', () => {
    const applyRemoteOp = vi.fn();
    const foldLocalOp = vi.fn();

    applyIngestedImageOptimistically({ annotation: null, applyRemoteOp, foldLocalOp });
    applyIngestedImageOptimistically({ annotation: {}, applyRemoteOp, foldLocalOp });

    expect(applyRemoteOp).not.toHaveBeenCalled();
    expect(foldLocalOp).not.toHaveBeenCalled();
  });

  it('works without a sync client connected (foldLocalOp omitted)', () => {
    const applyRemoteOp = vi.fn();
    const annotation = { id: 'ann-2', type: 'image' };

    expect(() =>
      applyIngestedImageOptimistically({ annotation, applyRemoteOp, foldLocalOp: undefined })
    ).not.toThrow();
    expect(applyRemoteOp).toHaveBeenCalledWith({ op: 'annotation_created', annotation });
  });
});
