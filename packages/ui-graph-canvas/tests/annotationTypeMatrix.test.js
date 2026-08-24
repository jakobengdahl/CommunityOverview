import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { createAnnotation, normalizeAnnotationDocument } from '../src/utils/annotationModel';

// Shared cross-language fixture (docs/fixtures/annotation_type_matrix.json):
// the complete accepted v1 annotation type matrix (docs/ANNOTATION_CONTRACT.md),
// one case per type/payload variant. backend/core/tests/test_annotation_type_matrix.py
// drives the same file through the backend model, session persistence and the
// MCP tool surface — see that fixture's own `$comment` for the shared-data
// contract between the two.
const fixturePath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../../../docs/fixtures/annotation_type_matrix.json'
);
const { cases } = JSON.parse(readFileSync(fixturePath, 'utf-8'));

function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

// Asserts every key/value the fixture declares is present in `actual`,
// recursing into nested objects (e.g. a line endpoint's `attachment`) —
// `actual` may carry additional normalization-derived keys (e.g.
// `normalizeEndpoint` adding a resolved `point` alongside a fixture-declared
// `attachment`) without failing the assertion, since "complete" here means
// "nothing declared was dropped", not "nothing was added".
function expectContains(actual, expected) {
  for (const [key, value] of Object.entries(expected)) {
    if (isPlainObject(value)) {
      expect(isPlainObject(actual?.[key])).toBe(true);
      expectContains(actual[key], value);
    } else {
      expect(actual?.[key]).toEqual(value);
    }
  }
}

describe('annotation type matrix (cross-language fixture) — JS model', () => {
  it('covers every v1 annotation type exactly once (plus two shape variants)', () => {
    const types = cases.map((c) => c.type);
    expect(new Set(types)).toEqual(
      new Set([
        'note',
        'text',
        'label',
        'line',
        'frame',
        'group',
        'shape',
        'icon',
        'vote_dot',
        'image',
        'freehand',
      ])
    );
    expect(types.filter((t) => t === 'shape')).toHaveLength(2);
  });

  for (const testCase of cases) {
    it(`creates and round-trips a ${testCase.id} annotation unchanged`, () => {
      const input = {
        id: testCase.id,
        type: testCase.type,
        geometry: {
          x: testCase.x,
          y: testCase.y,
          w: testCase.w,
          h: testCase.h,
          rotation: testCase.rotation ?? 0,
        },
        ...testCase.fields,
      };

      const created = createAnnotation(input);
      expect(created.type).toBe(testCase.type);
      expect(created.geometry.x).toBe(testCase.x);
      expect(created.geometry.y).toBe(testCase.y);
      if (testCase.rotation !== undefined) {
        expect(created.geometry.rotation).toBe(testCase.rotation);
      }
      // Every declared field must survive creation — this is the "complete"
      // half of the attachment/shape/icon/endpoint contract: nothing in the
      // accepted payload is silently dropped.
      expectContains(created, testCase.fields);

      // Round-trip through a document (the shape session persistence stores
      // and reloads) must be idempotent: re-normalizing an already-normalized
      // annotation must not change it.
      const doc = normalizeAnnotationDocument({ annotations: [created] });
      expect(doc.annotations[0]).toEqual(created);
    });
  }
});
