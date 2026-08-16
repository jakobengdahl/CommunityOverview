import { describe, it, expect } from 'vitest';
import { getNodeColor } from '../src/utils/constants';

describe('getNodeColor', () => {
  it('returns the registered color for a known node type', () => {
    expect(getNodeColor('Actor')).toBe('#3B82F6');
  });

  it('returns the neutral default for an unregistered node type', () => {
    expect(getNodeColor('Questionnaire')).toBe('#9CA3AF');
  });

  it('treats Object prototype member names as unregistered node types', () => {
    for (const nodeType of ['toString', 'constructor', 'hasOwnProperty', '__proto__']) {
      expect(getNodeColor(nodeType)).toBe('#9CA3AF');
    }
  });
});
