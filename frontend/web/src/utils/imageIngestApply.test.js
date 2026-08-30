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

  it('reports a response that carried no usable annotation, rather than failing silently', async () => {
    const applyRemoteOp = vi.fn();
    const foldLocalOp = vi.fn();

    // A 200 with a missing or malformed `annotation`. Nothing can be rendered
    // from it, and the caller must not go on to mark the ingest delivered:
    // that is how "I picked a file and got neither an image nor an error"
    // happened — the canvas simply never changed and nothing said why.
    await expect(
      applyIngestedImageOptimistically({ annotation: null, applyRemoteOp, foldLocalOp })
    ).resolves.toBe(false);
    await expect(
      applyIngestedImageOptimistically({ annotation: {}, applyRemoteOp, foldLocalOp })
    ).resolves.toBe(false);

    expect(applyRemoteOp).not.toHaveBeenCalled();
    expect(foldLocalOp).not.toHaveBeenCalled();
  });

  it('reports success even when the echo won the race — a lost race is not a failure', async () => {
    // `applied === false` means the confirming SSE echo applied the same op
    // first. The image IS on the canvas, so this must not be reported as a
    // failed upload.
    const annotation = { id: 'ann-3', type: 'image' };
    const applyRemoteOp = vi.fn().mockResolvedValue(false);

    await expect(
      applyIngestedImageOptimistically({ annotation, applyRemoteOp, foldLocalOp: vi.fn() })
    ).resolves.toBe(true);
  });

  it('works without a sync client connected (foldLocalOp omitted)', async () => {
    const applyRemoteOp = vi.fn().mockResolvedValue(true);
    const annotation = { id: 'ann-2', type: 'image' };

    await expect(
      applyIngestedImageOptimistically({ annotation, applyRemoteOp, foldLocalOp: undefined })
    ).resolves.toBe(true);
    expect(applyRemoteOp).toHaveBeenCalledWith({ op: 'annotation_created', annotation });
  });
});
