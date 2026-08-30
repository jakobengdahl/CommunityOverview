import { describe, it, expect } from 'vitest';
import { hasCanvasContent, shouldPersistSnapshot } from './sessionSnapshotGuard.js';

describe('hasCanvasContent', () => {
  it('counts annotations as content, not just graph nodes', () => {
    // The regression this exists for. Annotations live only in ReactFlow's own
    // node state and never enter the graph store, so a guard that asked only
    // about graph nodes declared an annotation-only canvas empty and dropped
    // every save — the work then disappeared on the next session switch.
    expect(hasCanvasContent({ graphNodeCount: 0, annotationCount: 3 })).toBe(true);
  });

  it('counts group boxes as content for the same reason', () => {
    expect(hasCanvasContent({ graphNodeCount: 0, groupCount: 1 })).toBe(true);
  });

  it('still reports a genuinely empty canvas as empty', () => {
    // The guard is deliberate and must stay: without it every visit to a fresh
    // session id would leave an empty session file behind (D13).
    expect(hasCanvasContent({ graphNodeCount: 0, annotationCount: 0, groupCount: 0 })).toBe(false);
    expect(hasCanvasContent({})).toBe(false);
  });

  it('reports graph nodes as content, as it always did', () => {
    expect(hasCanvasContent({ graphNodeCount: 1 })).toBe(true);
  });
});

describe('shouldPersistSnapshot', () => {
  it('writes an annotation-only canvas even before the session exists server-side', () => {
    expect(
      shouldPersistSnapshot({ isMaterialized: false, graphNodeCount: 0, annotationCount: 2 })
    ).toBe(true);
  });

  it('suppresses a genuinely empty, never-materialised session', () => {
    expect(shouldPersistSnapshot({ isMaterialized: false })).toBe(false);
  });

  it('writes an empty canvas once the session exists — the deletion has to persist', () => {
    // After the user clears everything, "empty" is the content to save, not a
    // reason to skip (D14).
    expect(shouldPersistSnapshot({ isMaterialized: true })).toBe(true);
  });
});
