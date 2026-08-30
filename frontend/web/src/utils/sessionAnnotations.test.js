import { describe, it, expect } from 'vitest';
import {
  annotationDocumentToLegacyMetadata,
  annotationsToGroups,
  groupsToAnnotations,
  legacyMetadataToAnnotationDocument,
  annotationsToOverlays,
  overlaysToAnnotations,
} from './sessionAnnotations';

// smallfix-annotation-version-dropped-by-browser-pipeline: this is the exact
// pipeline the independent review traced and reproduced —
// useSharedSession.js's serverStateToMirror pipes a raw server annotation
// through annotationsToOverlays then overlaysToAnnotations before it is ever
// handed to the sync client's baseline. Neither function used to read or
// write `version`/`field_versions`, so a browser's own base_version was
// always `undefined` on every real annotation_updated op it sent (JSON.
// stringify drops the key), landing every real write on the server's
// no-base_version legacy fallback instead of the same-field-conflict check
// dec-annotation-field-patches-and-conflicts describes. These pin that a
// server-assigned version now survives the full round trip, for every v1
// annotation kind, not only note/label.
describe('version/field_versions survive the server <-> overlay round trip', () => {
  it.each([
    { type: 'note', kind: 'note', extra: { text: 'hi' } },
    { type: 'label', kind: 'label', extra: { text: 'L' } },
    { type: 'line', kind: 'arrow', extra: { from: { x: 0, y: 0 }, to: { x: 10, y: 0 } } },
    { type: 'shape', kind: 'shape', extra: { shape: 'rectangle' } },
    { type: 'text', kind: 'text', extra: { text: 'hi' } },
    { type: 'icon', kind: 'icon', extra: { icon: 'flag' } },
    {
      type: 'freehand',
      kind: 'freehand',
      extra: {
        points: [
          { x: 0, y: 0 },
          { x: 5, y: 5 },
        ],
      },
    },
  ])(
    'carries version through annotationsToOverlays for a $type annotation',
    ({ type, kind, extra }) => {
      const serverAnnotation = {
        id: `${type}-1`,
        type,
        kind: type,
        position: { x: 0, y: 0 },
        geometry: { x: 0, y: 0, w: 10, h: 10, rotation: 0 },
        version: 7,
        field_versions: { text: 7 },
        ...extra,
      };
      const [overlay] = annotationsToOverlays([serverAnnotation]);
      expect(overlay).toBeTruthy();
      expect(overlay.kind).toBe(kind);
      expect(overlay.version).toBe(7);
      expect(overlay.field_versions).toEqual({ text: 7 });
    }
  );

  it('round-trips version through annotationsToOverlays then back through overlaysToAnnotations (the exact hydration pipeline)', () => {
    const serverAnnotation = {
      id: 'shape-1',
      type: 'shape',
      kind: 'shape',
      shape: 'rectangle',
      position: { x: 0, y: 0 },
      geometry: { x: 0, y: 0, w: 160, h: 96, rotation: 0 },
      style: {},
      z: 0,
      locked: false,
      version: 7,
      field_versions: { shape: 5 },
    };
    // This is exactly useSharedSession.js's serverStateToMirror pipeline.
    const overlays = annotationsToOverlays([serverAnnotation]);
    const annotations = overlaysToAnnotations(overlays);
    expect(annotations).toHaveLength(1);
    expect(annotations[0].version).toBe(7);
    expect(annotations[0].field_versions).toEqual({ shape: 5 });
  });

  it('round-trips version through the group translators (annotationsToGroups / groupsToAnnotations)', () => {
    const { groups, parentIds } = annotationsToGroups([
      { id: 'g1', kind: 'group', label: 'Team', position: { x: 0, y: 0 }, version: 3 },
    ]);
    expect(groups[0].version).toBe(3);
    const [ann] = groupsToAnnotations(groups, parentIds);
    expect(ann.version).toBe(3);
  });

  it('does not invent a version for a brand-new, never-synced overlay', () => {
    const overlay = { id: 'note-new', kind: 'note', position: { x: 0, y: 0 }, text: 'new' };
    const [annotation] = overlaysToAnnotations([overlay]);
    expect(annotation.version).toBeUndefined();
    expect(annotation.field_versions).toBeUndefined();
  });
});

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
describe('shape caption round-trip', () => {
  it('carries a caption through legacyMetadataToAnnotationDocument -> annotationDocumentToLegacyMetadata', () => {
    const overlay = {
      id: 'shape-1',
      kind: 'shape',
      position: { x: 0, y: 0 },
      shape: 'hexagon',
      text: 'Step 1',
    };
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

// task-annotation-render-direct-manipulation: label/text/icon attachments
// round-trip between the server annotation document and the canvas overlay
// shape, not only through the JS annotation model. `vote_dot` used to be a
// fourth member of this list; task-annotation-vote-dot-simplify retired its
// attachment behaviour — see the dedicated test below for what it does now.
describe('attachment round-trip through the server annotation document', () => {
  const attachment = { target_id: 'node-1', target_type: 'node', offset: { x: 4, y: -6 } };

  it.each(['label', 'text', 'icon'])(
    'carries an attachment through legacyMetadataToAnnotationDocument -> annotationDocumentToLegacyMetadata for %s',
    (kind) => {
      const overlay = {
        id: `${kind}-1`,
        kind,
        position: { x: 0, y: 0 },
        text: kind === 'text' || kind === 'label' ? 'hi' : undefined,
        icon: kind === 'icon' ? 'flag' : undefined,
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

  // Explicit regression for task-annotation-vote-dot-simplify: a vote_dot
  // overlay carrying a stale `attachment` (from before this change — no
  // migration was written) must not have it resurface on either leg of this
  // round trip.
  it('drops a vote_dot attachment on both legs of the round trip, not just one', () => {
    const overlay = {
      id: 'vote-dot-1',
      kind: 'vote_dot',
      position: { x: 0, y: 0 },
      color: '#3b82f6',
      attachment,
    };
    const document = legacyMetadataToAnnotationDocument({ annotations: [overlay] });
    const stored = document.annotations.find((a) => a.id === overlay.id);
    expect(stored.attachment).toBeUndefined();

    const metadata = annotationDocumentToLegacyMetadata(document);
    const roundTripped = metadata.annotations.find((a) => a.id === overlay.id);
    expect(roundTripped.attachment).toBeUndefined();
    expect(roundTripped.color).toBe('#3b82f6');
  });
});

// smallfix-annotation-unsized-generic-geometry-clobber: icon/vote_dot/text
// used to lose their geometry.w/h on this round trip and get re-materialised
// at createAnnotation's 160x96 default by the next autosave. shape/image
// already carried size through; this locks in that all five generic
// kinds now behave the same way.
describe('geometry w/h round-trip for generic overlay kinds', () => {
  const overlayFor = (kind) => ({
    id: `${kind}-1`,
    kind,
    position: { x: 0, y: 0 },
    text: kind === 'text' ? 'hi' : undefined,
    shape: kind === 'shape' ? 'circle' : undefined,
    icon: kind === 'icon' ? 'flag' : undefined,
    image: kind === 'image' ? { url: 'https://example.test/x.png' } : undefined,
    size: { w: 32, h: 41 },
  });

  it.each(['icon', 'vote_dot', 'text', 'shape', 'image'])(
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

// A group has always been lockable over MCP (create_group_annotation takes
// `locked`), but both group translators dropped the flag, so it never reached
// the canvas and the browser's next autosave diffed it back to its default —
// the exact failure docs/ANNOTATION_CONTRACT.md warns about for envelope
// fields. `z` is carried for the same reason; nothing reads it for groups yet
// (their paint order is array order), so this preserves the value without
// offering a control for it.
describe('group envelope round-trip (locked, z)', () => {
  it('carries locked and z from the server annotation onto the canvas group', () => {
    const { groups } = annotationsToGroups([
      { id: 'g1', kind: 'group', label: 'Team', position: { x: 0, y: 0 }, locked: true, z: 3 },
    ]);
    expect(groups[0].locked).toBe(true);
    expect(groups[0].z).toBe(3);
  });

  it('defaults an unlocked group at the base layer when the server omits both', () => {
    const { groups } = annotationsToGroups([
      { id: 'g1', kind: 'group', label: 'Team', position: { x: 0, y: 0 } },
    ]);
    expect(groups[0].locked).toBe(false);
    expect(groups[0].z).toBe(0);
  });

  it('carries locked and z back from the canvas group to the annotation', () => {
    const [ann] = groupsToAnnotations(
      [{ id: 'g1', label: 'Team', position: { x: 0, y: 0 }, locked: true, z: 2 }],
      {}
    );
    expect(ann.locked).toBe(true);
    expect(ann.z).toBe(2);
  });

  // The autosave path: a locked group loaded from the server is re-serialised
  // on every save. Before this round-trip existed the save wrote locked=false
  // back, silently unlocking a group nobody had touched.
  it('survives the save/restore round trip instead of reverting to unlocked', () => {
    const { groups, parentIds } = annotationsToGroups([
      { id: 'g1', kind: 'group', label: 'Team', position: { x: 1, y: 2 }, locked: true, z: 5 },
    ]);
    const [ann] = groupsToAnnotations(groups, parentIds);
    expect(ann.locked).toBe(true);
    expect(ann.z).toBe(5);
  });

  it('keeps the flag through the legacy saved-view metadata leg', () => {
    const metadata = annotationDocumentToLegacyMetadata([
      { id: 'g1', type: 'group', label: 'Team', position: { x: 0, y: 0 }, locked: true, z: 4 },
    ]);
    expect(metadata.groups[0]).toEqual(expect.objectContaining({ locked: true, z: 4 }));
    const document = legacyMetadataToAnnotationDocument({
      groups: metadata.groups,
      parentIds: {},
      annotations: [],
    });
    expect(document.annotations[0]).toEqual(expect.objectContaining({ locked: true, z: 4 }));
  });
});
