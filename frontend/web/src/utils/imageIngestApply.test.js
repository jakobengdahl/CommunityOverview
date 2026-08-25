import { describe, it, expect, vi } from 'vitest';
import { applyIngestedImageOptimistically } from './imageIngestApply.js';

describe('applyIngestedImageOptimistically', () => {
  it('folds the op into the sync baseline when applyRemoteOp reports it won the render race', async () => {
    const annotation = { id: 'ann-1', type: 'image' };
    const applyRemoteOp = vi.fn().mockResolvedValue(true);
    const foldLocalOp = vi.fn();

    await applyIngestedImageOptimistically({ annotation, applyRemoteOp, foldLocalOp });

    const expectedOp = { op: 'annotation_created', annotation };
    expect(applyRemoteOp).toHaveBeenCalledWith(expectedOp);
    expect(foldLocalOp).toHaveBeenCalledWith(expectedOp);
  });

  it('does not fold when applyRemoteOp reports the echo already won the race', async () => {
    const annotation = { id: 'ann-1', type: 'image' };
    const applyRemoteOp = vi.fn().mockResolvedValue(false);
    const foldLocalOp = vi.fn();

    await applyIngestedImageOptimistically({ annotation, applyRemoteOp, foldLocalOp });

    expect(applyRemoteOp).toHaveBeenCalledWith({ op: 'annotation_created', annotation });
    // The echo already folded this same op into the baseline itself; folding
    // it again here would risk reverting an edit made since.
    expect(foldLocalOp).not.toHaveBeenCalled();
  });

  it('does nothing when the annotation has no id', async () => {
    const applyRemoteOp = vi.fn();
    const foldLocalOp = vi.fn();

    await applyIngestedImageOptimistically({ annotation: null, applyRemoteOp, foldLocalOp });
    await applyIngestedImageOptimistically({ annotation: {}, applyRemoteOp, foldLocalOp });

    expect(applyRemoteOp).not.toHaveBeenCalled();
    expect(foldLocalOp).not.toHaveBeenCalled();
  });

  it('works without a sync client connected (foldLocalOp omitted)', async () => {
    const applyRemoteOp = vi.fn().mockResolvedValue(true);
    const annotation = { id: 'ann-2', type: 'image' };

    await expect(
      applyIngestedImageOptimistically({ annotation, applyRemoteOp, foldLocalOp: undefined })
    ).resolves.toBeUndefined();
    expect(applyRemoteOp).toHaveBeenCalledWith({ op: 'annotation_created', annotation });
  });
});
