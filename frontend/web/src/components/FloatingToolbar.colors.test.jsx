import { describe, it, expect } from 'vitest';
import { resolveColor, COLOR_MAP, DEFAULT_COLOR } from './FloatingToolbar';

describe('resolveColor', () => {
  it('uses the schema color for a type the legacy map does not cover', () => {
    const schema = { node_types: { Questionnaire: { color: '#7C3AED' } } };
    expect(resolveColor('Questionnaire', schema)).toBe('#7C3AED');
  });

  it('lets the schema recolor a legacy type name the map already covers', () => {
    const schema = { node_types: { Capability: { color: '#F59E0B' } } };
    expect(COLOR_MAP.Capability).toBe('#F97316');
    expect(resolveColor('Capability', schema)).toBe('#F59E0B');
  });

  it('falls back to the legacy color for a covered type the schema leaves uncolored', () => {
    expect(resolveColor('Capability', { node_types: { Capability: {} } })).toBe(
      COLOR_MAP.Capability
    );
    expect(resolveColor('Capability', null)).toBe(COLOR_MAP.Capability);
  });

  it('falls back to neutral gray when neither source declares a color', () => {
    expect(resolveColor('Questionnaire', { node_types: { Questionnaire: {} } })).toBe(
      DEFAULT_COLOR
    );
    expect(resolveColor('Questionnaire', null)).toBe(DEFAULT_COLOR);
  });

  it('treats Object prototype member names as uncolored node types', () => {
    for (const nodeType of ['toString', 'constructor', 'hasOwnProperty', '__proto__']) {
      expect(resolveColor(nodeType, { node_types: {} })).toBe(DEFAULT_COLOR);
    }
  });
});
