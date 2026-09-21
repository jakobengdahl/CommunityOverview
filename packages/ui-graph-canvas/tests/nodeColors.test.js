import { describe, it, expect } from 'vitest';
import { getNodeColor } from '../src/utils/constants';

describe('getNodeColor', () => {
  it('returns the registered color for a known node type', () => {
    expect(getNodeColor('Actor')).toBe('#3B82F6');
  });

  it('keeps generic renderer node types in the shared table', () => {
    expect(getNodeColor('Dataset')).toBe('#06B6D4');
    expect(getNodeColor('ActiveKnowledgeCollection')).toBe('#F59E0B');
    expect(getNodeColor('Group')).toBe('#646cff');
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
