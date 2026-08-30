// End-to-end regression coverage for round 5 (smallfix-applyremoteop-canvas-
// no-version-guard): a stale/reordered remote annotation broadcast must never
// reach the live canvas, not just the sessionSyncClient.js internal sync
// baseline (round 3's own scope).
//
// This drives the REAL `SessionSyncClient` through a real simulated SSE
// stream, the REAL `annotationsToOverlays` translator (`sessionAnnotations.js`
// — what App.jsx's `applyAnnotationUpsertToCanvas` actually calls), and the
// REAL `GraphCanvas` component's remote-annotation-ops effect (the same
// effect `packages/ui-graph-canvas/tests/GraphCanvasRemote.test.jsx` exercises)
// — the same three layers a real browser tab chains together between an SSE
// 'op' event and a rendered ReactFlow node. `reactflow` itself is mocked the
// same minimal way that file's harness does (real ReactFlow does not run
// under jsdom here); everything else in this chain is the actual production
// code, not a stand-in for it. App.jsx's own `applyRemoteOp`/
// `applyAnnotationUpsertToCanvas` are ~2500 lines deep in a component this
// repo's test infra does not mount wholesale (see sessionFlow.test.jsx, which
// stubs GraphCanvas out entirely for that reason) — the harness component
// below reproduces exactly the few lines of glue those two functions add for
// an annotation_created/annotation_updated op (translate via
// annotationsToOverlays, then queue an upsert-overlay op), so what is
// "stand-in" here is only that narrow seam, not any of the three real layers
// the bug actually spans.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { useState, useEffect } from 'react';
import { render, act } from '@testing-library/react';
import { GraphCanvas } from '@community-graph/ui-graph-canvas';
import { SessionSyncClient } from '../src/services/sessionSyncClient';
import { annotationsToOverlays } from '../src/utils/sessionAnnotations';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children }) => <div data-testid="react-flow">{children}</div>;
  return {
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    useNodesState: (initial) => [initial || [], hoisted.setNodes, vi.fn()],
    useEdgesState: (initial) => [initial || [], vi.fn(), vi.fn()],
    useReactFlow: () => ({
      fitView: vi.fn(),
      getNodes: () => [],
      getEdges: () => [],
      setNodes: hoisted.setNodes,
      setEdges: vi.fn(),
      screenToFlowPosition: ({ x, y }) => ({ x, y }),
      setCenter: vi.fn(),
      getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
    }),
    useOnSelectionChange: () => {},
    Background: () => null,
    Controls: () => null,
    MiniMap: () => null,
    NodeResizer: () => null,
    SelectionMode: { Partial: 'partial' },
  };
});

// Mirrors GraphCanvasRemote.test.jsx's own helper: several effects call
// setNodes on a given render (including the input-sync effect that resets to
// the empty `nodes` prop) — apply each captured updater to a fresh copy of
// `seed` independently and return the first that satisfies `predicate`.
function findResult(seed, predicate) {
  for (const call of hoisted.setNodes.mock.calls) {
    if (typeof call[0] !== 'function') continue;
    let result;
    try {
      result = call[0](seed.map((n) => ({ ...n })));
    } catch {
      continue;
    }
    if (Array.isArray(result) && predicate(result)) return result;
  }
  return null;
}

class FakeEventSource {
  constructor() {
    this.onmessage = null;
    this.onerror = null;
    FakeEventSource.instances.push(this);
  }
  emit(obj) {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
  close() {}
}
FakeEventSource.instances = [];

// The narrow slice of App.jsx's applyRemoteOp/applyAnnotationUpsertToCanvas
// this harness stands in for, for a non-group annotation_created/
// annotation_updated op — see this file's header comment.
function applyAnnotationOpToRemoteQueue(op, setRemoteAnnotationOps) {
  if (op?.op !== 'annotation_created' && op?.op !== 'annotation_updated') return;
  const ann = op.annotation;
  if (!ann || !ann.id) return;
  const [overlay] = annotationsToOverlays([ann]);
  if (overlay) {
    setRemoteAnnotationOps((prev) => [...(prev || []), { action: 'upsert-overlay', overlay }]);
  }
}

function Harness({ clientRef, onOpsPushed }) {
  const [remoteAnnotationOps, setRemoteAnnotationOps] = useState(null);
  useEffect(() => {
    clientRef.current.handlers.onRemoteOps = (ops) => {
      for (const op of ops) {
        applyAnnotationOpToRemoteQueue(op, setRemoteAnnotationOps);
      }
      onOpsPushed?.(ops);
    };
  }, [clientRef, onOpsPushed]);
  return (
    <GraphCanvas
      nodes={[]}
      edges={[]}
      remoteAnnotationOps={remoteAnnotationOps}
      onRemoteAnnotationsApplied={() => setRemoteAnnotationOps(null)}
    />
  );
}

function makeClient() {
  return new SessionSyncClient({
    sessionId: '1234-5678',
    clientId: 'client-A',
    streamUrl: '/api/sessions/1234-5678/stream',
    opsUrl: '/api/sessions/1234-5678/ops',
    handlers: {},
    flushIntervalMs: 1,
    fetchImpl: vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) })),
    EventSourceImpl: FakeEventSource,
  });
}

describe('a stale remote annotation broadcast never reaches the live canvas (round 5, smallfix-applyremoteop-canvas-no-version-guard)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    FakeEventSource.instances = [];
  });

  const initial = {
    id: 'note-1',
    type: 'note',
    kind: 'note',
    text: 'A wrote this',
    position: { x: 0, y: 0 },
    version: 1,
    field_versions: {},
  };

  it("keeps client A's confirmed edit on the canvas when client B's stale/reordered broadcast arrives late", () => {
    const clientRef = { current: makeClient() };
    const opsPushed = vi.fn();
    render(<Harness clientRef={clientRef} onOpsPushed={opsPushed} />);
    clientRef.current.connect();
    const es = FakeEventSource.instances[0];

    // Baseline: this canvas already has the annotation at version 2, with
    // A's own confirmed edit (mirrors the sync baseline already being at
    // version 3 in the equivalent sessionSyncClient.test.js scenario — the
    // exact number differs, only the ordering matters here).
    clientRef.current.setBaseline({
      annotations: [{ ...initial, text: "A's edit", version: 2, field_versions: { text: 2 } }],
    });

    // 1. A genuinely different, and genuinely newer, collaborator broadcast
    // (client-C, version 3) applies normally — the canvas must show it.
    act(() => {
      es.emit({
        type: 'op',
        seq: 1,
        client_id: 'client-C',
        op: {
          op: 'annotation_updated',
          annotation: {
            ...initial,
            text: "A's edit, then C's",
            version: 3,
            field_versions: { text: 3 },
          },
        },
      });
    });
    expect(opsPushed).toHaveBeenCalledTimes(1);
    const afterC = findResult(
      [],
      (r) => r.find((n) => n.id === 'note-1')?.data?.text === "A's edit, then C's"
    );
    expect(afterC).not.toBeNull();
    expect(afterC.find((n) => n.id === 'note-1').data.text).toBe("A's edit, then C's");

    // 2. Client B's SSE broadcast for its own EARLIER (lower-version) op
    // arrives late — after the canvas already shows C's newer content above.
    // Before this fix, sessionSyncClient.js's onRemoteOps fired for this op
    // regardless of the (correct) baseline guard, and App.jsx's
    // applyRemoteOp/applyAnnotationUpsertToCanvas wrote it straight onto the
    // canvas with no version check of its own — flashing the canvas back to
    // B's stale content.
    hoisted.setNodes.mockClear();
    act(() => {
      es.emit({
        type: 'op',
        seq: 2,
        client_id: 'client-B',
        op: {
          op: 'annotation_updated',
          annotation: {
            ...initial,
            text: "B's stale edit",
            version: 2,
            field_versions: { text: 2 },
          },
        },
      });
    });

    // The fix: onRemoteOps is never invoked a second time for this stale
    // broadcast, so applyAnnotationOpToRemoteQueue (App.jsx's real
    // applyAnnotationUpsertToCanvas logic) never runs and no new
    // upsert-overlay op is ever queued — GraphCanvas's remote-op effect never
    // fires again for it at all.
    expect(opsPushed).toHaveBeenCalledTimes(1); // still just the one call, from step 1
    const afterB = findResult(afterC, (r) => r.find((n) => n.id === 'note-1')?.data?.text != null);
    // No new relevant setNodes call exists to find; the canvas keeps C's content.
    expect(afterB).toBeNull();
  });

  it('still applies a genuinely newer broadcast that arrives after a stale one (collaboration keeps working)', () => {
    const clientRef = { current: makeClient() };
    const opsPushed = vi.fn();
    render(<Harness clientRef={clientRef} onOpsPushed={opsPushed} />);
    clientRef.current.connect();
    const es = FakeEventSource.instances[0];

    clientRef.current.setBaseline({
      annotations: [{ ...initial, text: "A's edit", version: 3, field_versions: { text: 3 } }],
    });

    // A stale broadcast (version 2) arrives first — dropped, never reaches
    // the canvas (proven by the previous test); a genuinely newer one
    // (version 4) arrives right after and must still apply normally.
    act(() => {
      es.emit({
        type: 'op',
        seq: 2,
        client_id: 'client-B',
        op: {
          op: 'annotation_updated',
          annotation: {
            ...initial,
            text: "B's stale edit",
            version: 2,
            field_versions: { text: 2 },
          },
        },
      });
      es.emit({
        type: 'op',
        seq: 4,
        client_id: 'client-C',
        op: {
          op: 'annotation_updated',
          annotation: {
            ...initial,
            text: "C's newer edit",
            version: 4,
            field_versions: { text: 4 },
          },
        },
      });
    });

    expect(opsPushed).toHaveBeenCalledTimes(1); // only C's genuinely newer op
    const result = findResult(
      [],
      (r) => r.find((n) => n.id === 'note-1')?.data?.text === "C's newer edit"
    );
    expect(result).not.toBeNull();
    expect(result.find((n) => n.id === 'note-1').data.text).toBe("C's newer edit");
  });

  it('applies a brand-new annotation (no existing canvas node) unconditionally', () => {
    const clientRef = { current: makeClient() };
    const opsPushed = vi.fn();
    render(<Harness clientRef={clientRef} onOpsPushed={opsPushed} />);
    clientRef.current.connect();
    const es = FakeEventSource.instances[0];
    // No setBaseline call: this id is entirely new to the baseline, mirroring
    // round 3's own "no baseline entry -> apply normally" rule at the canvas
    // layer too.
    act(() => {
      es.emit({
        type: 'op',
        seq: 1,
        client_id: 'client-B',
        op: {
          op: 'annotation_created',
          annotation: {
            ...initial,
            id: 'note-new',
            text: 'brand new',
            version: 1,
            field_versions: {},
          },
        },
      });
    });
    expect(opsPushed).toHaveBeenCalledTimes(1);
    const result = findResult([], (r) => r.some((n) => n.id === 'note-new'));
    expect(result?.find((n) => n.id === 'note-new').data.text).toBe('brand new');
  });
});
