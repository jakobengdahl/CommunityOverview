import { describe, it, expect, afterEach, vi } from 'vitest';

import { generateVisualizationSessionId } from './api';

/**
 * Feed getRandomValues a scripted sequence of Uint16 draws so the mapping from
 * raw randomness to digit groups can be asserted exactly. Extra draws past the
 * script are an error: the function must not silently fall back to anything
 * less uniform when it runs out.
 */
function scriptDraws(values) {
  let next = 0;
  vi.spyOn(crypto, 'getRandomValues').mockImplementation((buf) => {
    for (let i = 0; i < buf.length; i += 1) {
      if (next >= values.length) throw new Error('scripted draws exhausted');
      buf[i] = values[next];
      next += 1;
    }
    return buf;
  });
  return () => next;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('generateVisualizationSessionId', () => {
  it('formats four groups of four decimal digits', () => {
    expect(generateVisualizationSessionId()).toMatch(/^\d{4}-\d{4}-\d{4}-\d{4}$/);
  });

  it('zero-pads a group whose draw is smaller than four digits', () => {
    scriptDraws([7, 42, 500, 1234]);
    expect(generateVisualizationSessionId()).toBe('0007-0042-0500-1234');
  });

  // The regression. This id is the capability guarding a shared session (design
  // D7), so its digit groups have to be uniform. A Uint16 spans 65536 values, so
  // a bare `n % 10000` maps six draws onto 0000-5535 and only five onto
  // 5536-9999. Draws at or above 60000 must therefore be discarded rather than
  // folded: 60000 would land on 0000 and 65535 on 5535, over-weighting the low
  // half of every group.
  it('discards draws in the biased tail instead of folding them', () => {
    // Drawn a batch of four at a time: the first batch yields only 1111, since
    // 60000/65535/63000 are all in the tail. Folded instead of discarded they
    // would have produced 0000-5535-3000-1111.
    const drawsUsed = scriptDraws([60000, 65535, 63000, 1111, 2222, 3333, 4444, 5555]);
    expect(generateVisualizationSessionId()).toBe('1111-2222-3333-4444');
    expect(drawsUsed()).toBe(8);
  });

  it('keeps drawing until it has four unbiased groups', () => {
    // A whole batch rejected: the loop must refill rather than emit short.
    scriptDraws([60000, 60001, 60002, 60003, 9999, 8888, 7777, 6666]);
    expect(generateVisualizationSessionId()).toBe('9999-8888-7777-6666');
  });

  it('maps the largest accepted draw to the top of the group range', () => {
    scriptDraws([59999, 50000, 10000, 0]);
    expect(generateVisualizationSessionId()).toBe('9999-0000-0000-0000');
  });

  it('mints a distinct id on each call', () => {
    const ids = new Set(Array.from({ length: 200 }, () => generateVisualizationSessionId()));
    expect(ids.size).toBe(200);
  });
});
