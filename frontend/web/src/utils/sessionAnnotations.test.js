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

// task-annotation-doubleclick-to-edit-text: a shape's optional caption text
// must survive the host-level overlay <-> server-document round trip too,
// not only the canvas-node round trip overlaySerialization.test.js covers —
// this is the layer legacyMetadataToAnnotationDocument/
// annotationDocumentToLegacyMetadata actually use for session save/restore.
describe("shape caption round-trip", () => {
  it('carries a caption through legacyMetadataToAnnotationDocument -> annotationDocumentToLegacyMetadata', () => {
    const overlay = { id: 'shape-1', kind: 'shape', position: { x: 0, y: 0 }, shape: 'hexagon', text: 'Step 1' };
    const document = legacyMetadataToAnnotationDocument({ annotations: [overlay] });
    const stored = document.annotations.find((a) => a.id === overlay.id);
    expect(stored.text).toBe('Step 1');

    const metadata = annotationDocumentToLegacyMetadata(document);
    const roundTripped = metadata.annotations.find((a) => a.id === overlay.id);
    expect(roundTripped.text).toBe('Step 1');
  });

  it('defaults to an empty caption when the shape overlay has none', () => {
    const document = legacyMetadataToAnnotationDocument({
      annotations: [{ id: 'shape-2', kind: 'shape', position: { x: 0, y: 0 }, shape: 'circle' }],
    });
    expect(document.annotations[0].text).toBe('');
  });
});

// task-annotation-render-direct-manipulation: label/text/icon/vote_dot
// attachments now round-trip between the server annotation document and the
// canvas overlay shape, not only through the JS annotation model.
describe('attachment round-trip through the server annotation document', () => {
  const attachment = { target_id: 'node-1', target_type: 'node', offset: { x: 4, y: -6 } };

  it.each(['label', 'text', 'icon', 'vote_dot'])(
    'carries an attachment through legacyMetadataToAnnotationDocument -> annotationDocumentToLegacyMetadata for %s',
    (kind) => {
      const overlay = {
        id: `${kind}-1`,
        kind,
        position: { x: 0, y: 0 },
        text: kind === 'text' || kind === 'label' ? 'hi' : undefined,
        icon: kind === 'icon' ? 'flag' : undefined,
        value: kind === 'vote_dot' ? 1 : undefined,
        attachment,
      };
      const document = legacyMetadataToAnnotationDocument({ annotations: [overlay] });
      const stored = document.annotations.find((a) => a.id === overlay.id);
      expect(stored.attachment).toEqual(attachment);

      const metadata = annotationDocumentToLegacyMetadata(document);
      const roundTripped = metadata.annotations.find((a) => a.id === overlay.id);
      expect(roundTripped.attachment).toEqual(attachment);
    }
  );

  it('leaves attachment unset when the overlay has none', () => {
    const document = legacyMetadataToAnnotationDocument({
      annotations: [{ id: 'label-2', kind: 'label', position: { x: 0, y: 0 }, text: 'no attach' }],
    });
    expect(document.annotations[0].attachment).toBeUndefined();
  });
});

// smallfix-annotation-unsized-generic-geometry-clobber: icon/vote_dot/text
// used to lose their geometry.w/h on this round trip and get re-materialised
// at createAnnotation's 160x96 default by the next autosave. frame/shape/
// image already carried size through; this locks in that all six generic
// kinds now behave the same way.
describe('geometry w/h round-trip for generic overlay kinds', () => {
  const overlayFor = (kind) => ({
    id: `${kind}-1`,
    kind,
    position: { x: 0, y: 0 },
    text: kind === 'text' ? 'hi' : undefined,
    shape: kind === 'shape' ? 'circle' : undefined,
    icon: kind === 'icon' ? 'flag' : undefined,
    value: kind === 'vote_dot' ? 1 : undefined,
    image: kind === 'image' ? { url: 'https://example.test/x.png' } : undefined,
    size: { w: 32, h: 41 },
  });

  it.each(['icon', 'vote_dot', 'text', 'frame', 'shape', 'image'])(
    'preserves an explicit non-default size for %s through overlay -> document -> overlay',
    (kind) => {
      const overlay = overlayFor(kind);
      const document = legacyMetadataToAnnotationDocument({ annotations: [overlay] });
      const stored = document.annotations.find((a) => a.id === overlay.id);
      expect(stored.geometry.w).toBe(32);
      expect(stored.geometry.h).toBe(41);

      const metadata = annotationDocumentToLegacyMetadata(document);
      const roundTripped = metadata.annotations.find((a) => a.id === overlay.id);
      expect(roundTripped.size).toEqual({ w: 32, h: 41 });
    }
  );

  it.each(['icon', 'vote_dot', 'text'])(
    'no longer falls back to the 160x96 default for %s when a size was set',
    (kind) => {
      const overlay = overlayFor(kind);
      const document = legacyMetadataToAnnotationDocument({ annotations: [overlay] });
      const metadata = annotationDocumentToLegacyMetadata(document);
      const roundTripped = metadata.annotations.find((a) => a.id === overlay.id);
      expect(roundTripped.size).not.toEqual({ w: 160, h: 96 });
    }
  );
});
