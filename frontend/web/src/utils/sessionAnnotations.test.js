import { describe, it, expect } from 'vitest';
import {
  annotationDocumentToLegacyMetadata,
  annotationsToGroups,
  groupsToAnnotations,
  legacyMetadataToAnnotationDocument,
} from './sessionAnnotations';

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

  it('migrates legacy saved-view metadata into a v1 annotation document', () => {
    const document = legacyMetadataToAnnotationDocument({
      groups: [{ id: 'g1', label: 'Team', position: { x: 0, y: 0 } }],
      parentIds: { n1: 'g1' },
      annotations: [{ id: 'note-1', kind: 'note', position: { x: 1, y: 2 }, text: 'hello' }],
    });
    expect(document.schema_version).toBe(1);
    expect(document.annotations.map((a) => a.type).sort()).toEqual(['group', 'note']);
    expect(document.annotations.find((a) => a.id === 'g1').member_node_ids).toEqual(['n1']);
  });

  it('exports a v1 document as backward-compatible saved-view metadata', () => {
    const metadata = annotationDocumentToLegacyMetadata([
      { id: 'g1', type: 'group', label: 'Team', member_node_ids: ['n1'], position: { x: 0, y: 0 } },
      { id: 'label-1', type: 'label', text: 'L', position: { x: 1, y: 1 } },
    ]);
    expect(metadata.annotation_schema_version).toBe(1);
    expect(metadata.groups).toHaveLength(1);
    expect(metadata.annotations).toEqual([
      expect.objectContaining({ id: 'label-1', kind: 'label', text: 'L' }),
    ]);
  });
});
