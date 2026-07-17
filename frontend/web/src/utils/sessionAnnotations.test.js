import { describe, it, expect } from 'vitest';
import { annotationsToGroups, groupsToAnnotations } from './sessionAnnotations';

describe('group description round-trip (R12)', () => {
  it('carries description through groupsToAnnotations', () => {
    const [ann] = groupsToAnnotations(
      [
        {
          id: 'g1',
          label: 'Team',
          description: 'Drag nodes here to group them',
          position: { x: 0, y: 0 },
        },
      ],
      {}
    );
    expect(ann.description).toBe('Drag nodes here to group them');
  });

  it('defaults to an empty string when the canvas group has no description', () => {
    const [ann] = groupsToAnnotations([{ id: 'g1', label: 'Team', position: { x: 0, y: 0 } }], {});
    expect(ann.description).toBe('');
  });

  it('carries description through annotationsToGroups', () => {
    const { groups } = annotationsToGroups([
      { id: 'g1', kind: 'group', label: 'Team', description: 'Hi there', position: { x: 0, y: 0 } },
    ]);
    expect(groups[0].description).toBe('Hi there');
  });

  it('defaults to an empty string when the server annotation has no description', () => {
    const { groups } = annotationsToGroups([
      { id: 'g1', kind: 'group', label: 'Team', position: { x: 0, y: 0 } },
    ]);
    expect(groups[0].description).toBe('');
  });

  it('round-trips a description through both directions unchanged', () => {
    const { groups, parentIds } = annotationsToGroups([
      {
        id: 'g1',
        kind: 'group',
        label: 'Team',
        description: 'Round trip',
        position: { x: 1, y: 2 },
      },
    ]);
    const [ann] = groupsToAnnotations(groups, parentIds);
    expect(ann.description).toBe('Round trip');
  });
});
