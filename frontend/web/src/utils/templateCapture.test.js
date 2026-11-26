import { describe, it, expect, vi } from 'vitest';
import { prepareTemplateCapture } from './templateCapture';

describe('template capture autosaves', () => {
  it.each([
    [true, true],
    [true, false],
    [false, true],
    [false, false],
  ])(
    'restores only cancelled timers on failure (annotation %s, canvas %s)',
    async (annotationPending, canvasPending) => {
      const error = new Error('flush failed');
      const scheduleAnnotation = vi.fn();
      const scheduleCanvas = vi.fn();
      await expect(
        prepareTemplateCapture({
          prepare: () => Promise.reject(error),
          isCurrent: () => true,
          annotationPending,
          canvasPending,
          scheduleAnnotation,
          scheduleCanvas,
        })
      ).rejects.toBe(error);
      expect(scheduleAnnotation).toHaveBeenCalledTimes(Number(annotationPending));
      expect(scheduleCanvas).toHaveBeenCalledTimes(Number(canvasPending));
    }
  );
  it('does not restore timers after a session switch or a successful capture', async () => {
    const scheduleAnnotation = vi.fn();
    const scheduleCanvas = vi.fn();
    const options = {
      annotationPending: true,
      canvasPending: true,
      scheduleAnnotation,
      scheduleCanvas,
    };
    await expect(
      prepareTemplateCapture({
        ...options,
        isCurrent: () => false,
        prepare: () => Promise.reject(new Error()),
      })
    ).rejects.toThrow();
    await expect(
      prepareTemplateCapture({
        ...options,
        isCurrent: () => true,
        prepare: () => Promise.resolve('source'),
      })
    ).resolves.toBe('source');
    expect(scheduleAnnotation).not.toHaveBeenCalled();
    expect(scheduleCanvas).not.toHaveBeenCalled();
  });
});
