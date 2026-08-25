import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { GraphCanvas } from '../src/index';
import { overlayToFlowNode, flowNodeToOverlay } from '../src/utils/annotations';
import AnnotationErrorBoundary from '../src/components/AnnotationErrorBoundary';
import { AnnotationContext } from '../src/components/AnnotationContext';
import NoteNode from '../src/components/NoteNode';
import GenericAnnotationNode from '../src/components/GenericAnnotationNode';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children, nodes, nodeTypes }) => {
    // Captured so the tests can render each registered type directly: node
    // state is mocked, so annotations restored through the canvas never reach
    // a render, and only the registered component itself proves the wiring.
    hoisted.nodeTypes = nodeTypes;
    return (
      <div data-testid="react-flow">
        {/* Render each node through its registered type, the way ReactFlow does,
          so a node component that throws throws inside this tree — which is
          the whole failure mode under test. */}
        {(nodes || []).map((n) => {
          const Type = hoisted.nodeTypes?.[n.type];
          return Type ? <Type key={n.id} id={n.id} type={n.type} data={n.data} /> : null;
        })}
        {children}
      </div>
    );
  };
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
    useStore: () => 1,
    Background: () => null,
    Controls: () => null,
    MiniMap: () => null,
    NodeResizer: () => null,
    Handle: () => null,
    Position: { Top: 'top', Bottom: 'bottom', Left: 'left', Right: 'right' },
    MarkerType: { ArrowClosed: 'arrowclosed' },
    SelectionMode: { Partial: 'partial', Full: 'full' },
    ConnectionMode: { Loose: 'loose', Strict: 'strict' },
    PanOnScrollMode: { Free: 'free', Vertical: 'vertical', Horizontal: 'horizontal' },
    applyNodeChanges: (_c, n) => n,
    applyEdgeChanges: (_c, e) => e,
    addEdge: (_p, e) => e,
    getBezierPath: () => ['M0,0', 0, 0],
    BaseEdge: () => null,
    EdgeLabelRenderer: ({ children }) => <div>{children}</div>,
    useInternalNode: () => null,
  };
});

// Every shape of annotation data this codebase might meet from a graph written
// by an older version of itself. Annotations may be redesigned without
// migrating what is stored, so these are expected inputs — the guarantee that
// replaces backward compatibility is that none of them costs the user the
// canvas.
const MALFORMED = [
  { id: 'x1', kind: 'wormhole', position: { x: 0, y: 0 } },
  { id: 'x2', kind: 'note' },
  { id: 'x3', kind: 'shape', position: { x: 0, y: 0 }, shape: 'dodecahedron' },
  { id: 'x4', kind: 'shape', position: { x: 0, y: 0 }, size: { w: '160px', h: 'tall' } },
  { id: 'x5', kind: 'freehand', position: { x: 0, y: 0 } },
  { id: 'x6', kind: 'freehand', position: { x: 0, y: 0 }, points: 'not-an-array' },
  { id: 'x7', kind: 'vote_dot', position: { x: 0, y: 0 }, value: { legacy: true } },
  { id: 'x8', kind: 'icon', position: { x: 0, y: 0 }, icon: 42 },
  { id: 'x9', kind: 'image', position: { x: 0, y: 0 } },
  { id: 'x10', kind: 'arrow', position: { x: 0, y: 0 } },
  null,
  undefined,
  { kind: 'note', position: { x: 0, y: 0 } },
  'a string where an annotation should be',
];

describe('annotations the current code does not expect', () => {
  beforeEach(() => hoisted.setNodes.mockClear());

  it('renders the canvas rather than throwing, whatever is stored', () => {
    // The invariant, stated plainly: the graph survives. An annotation is a
    // decoration; the graph is the user's work, and no stored decoration
    // should be able to take it away.
    expect(() =>
      render(
        <GraphCanvas
          nodes={[{ id: 'n1', type: 'custom', position: { x: 0, y: 0 }, data: { label: 'Real' } }]}
          edges={[]}
          annotationsToRestore={MALFORMED}
          onAnnotationsRestored={vi.fn()}
        />
      )
    ).not.toThrow();
    expect(screen.getByTestId('react-flow')).toBeInTheDocument();
  });

  it('drops what cannot become a node instead of putting a null in the node list', () => {
    // A null in the node list is its own crash, one step later and harder to
    // trace back to the annotation that caused it.
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        annotationsToRestore={MALFORMED}
        onAnnotationsRestored={vi.fn()}
      />
    );
    for (const call of hoisted.setNodes.mock.calls) {
      const produced = typeof call[0] === 'function' ? call[0]([]) : call[0];
      if (!Array.isArray(produced)) continue;
      expect(produced.every((n) => n && typeof n.id === 'string')).toBe(true);
    }
  });
});

describe('the three crashes this closes, each at its own component', () => {
  // The per-type test below cannot cover these: its Proxy throws on every read,
  // so it exercises the boundary whether or not the component is fixed. These
  // assert the opposite — that the component draws NORMALLY, without falling
  // through to the placeholder. That distinction is the fix; the boundary is
  // only the backstop behind it.
  //
  // Rendered INSIDE the boundary on purpose. Rendered bare, a broken component
  // would throw out of `render()` and the "no placeholder" assertion would be
  // unreachable — true but inert, since nothing could ever produce a
  // placeholder. Inside it, a regression shows up as the placeholder appearing,
  // which is what the assertion claims to detect.
  const draw = (el) =>
    render(
      <AnnotationContext.Provider value={{ notifyChange: vi.fn(), labels: {} }}>
        <AnnotationErrorBoundary nodeId="under-test">{el}</AnnotationErrorBoundary>
      </AnnotationContext.Provider>
    );
  const noPlaceholder = () => expect(screen.queryByTestId('annotation-broken')).toBeNull();

  it('a note with no payload at all draws as an empty note', () => {
    draw(<NoteNode id="n1" selected={false} />);
    noPlaceholder();
  });

  // `frame` is deliberately absent: its render branch has no bare `data.`
  // dereference, so it drew fine before the fix too and the row would assert
  // nothing. The five below each threw.
  it.each(['text', 'shape', 'icon', 'vote_dot', 'image'])(
    'a %s with no payload at all draws rather than falling through to the placeholder',
    (kind) => {
      draw(<GenericAnnotationNode id={`g-${kind}`} type={kind} selected={false} />);
      noPlaceholder();
    }
  );

  it('a vote dot carrying a non-primitive value draws empty rather than throwing', () => {
    // React refuses an object as a child. An array would have rendered as its
    // joined members, which is why this coerces on type rather than testing
    // for object-ness.
    draw(<GenericAnnotationNode id="v1" type="vote_dot" data={{ value: { legacy: true } }} />);
    noPlaceholder();
    expect(document.querySelector('.kind-vote_dot').textContent).toBe('');
  });
});

describe('the registered node types are the ones that are guarded', () => {
  // The canvas-level test above cannot reach this: node state is mocked, so
  // the restored annotations never actually render. Without these, removing
  // the boundary from every annotation type leaves the suite green — verified.
  const registered = () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    return hoisted.nodeTypes;
  };

  const ANNOTATION_TYPES = [
    'group',
    'note',
    'label',
    'arrow',
    'text',
    'frame',
    'shape',
    'icon',
    'vote_dot',
    'image',
    'freehand',
  ];

  it.each(ANNOTATION_TYPES)('%s is wrapped, so one bad one cannot take the canvas', (kind) => {
    const Type = registered()[kind];
    expect(Type).toBeTruthy();
    // Data that throws on any read: stands in for whatever a future stored
    // shape does that this version's renderer cannot survive.
    const hostile = new Proxy(
      {},
      {
        get() {
          throw new Error('unreadable annotation payload');
        },
      }
    );
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => render(<Type id={`bad-${kind}`} type={kind} data={hostile} />)).not.toThrow();
    spy.mockRestore();
    expect(screen.getAllByTestId('annotation-broken').length).toBeGreaterThan(0);
  });

  it('leaves graph nodes unguarded, because their failure should be loud', () => {
    // `custom` is the user's actual work. Swallowing a render failure there
    // would hide a real defect behind a placeholder, and its data carries no
    // licence to change shape without migrating.
    const Custom = registered().custom;
    const hostile = new Proxy(
      {},
      {
        get() {
          throw new Error('unreadable node payload');
        },
      }
    );
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => render(<Custom id="n" type="custom" data={hostile} />)).toThrow();
    spy.mockRestore();
    expect(screen.queryByTestId('annotation-broken')).toBeNull();
  });
});

describe('every call site drops what the translators refuse', () => {
  // Three call sites, and the doc section claims all three are guarded. Only
  // the restore path was asserted; a reviewer showed the other two could have
  // their guard removed with the suite still green.
  const brokenNode = { id: '', type: 'note', position: { x: 0, y: 0 }, data: {} };

  // NOT COVERED, and said plainly rather than covered badly: the saved-view
  // export's own `.filter(Boolean)`. It reads node state, which this harness
  // mocks, so nothing a test puts on the canvas reaches it — a first attempt
  // passed with the filter removed, because an empty exported list satisfies
  // "no nulls" perfectly well. Reaching it needs a harness that runs real node
  // state, which is a bigger change than this assertion is worth. The filter
  // is there, and `flowNodeToOverlay` returning null for an id-less node is
  // covered below; what is untested is the export dropping it.

  it('a malformed remote op does not put a null in the node list', () => {
    // A peer or an agent sending something this client cannot translate must
    // not take this client's canvas down.
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteAnnotationOps={[
          { action: 'upsert-overlay', overlay: brokenNode },
          { action: 'upsert-overlay', overlay: null },
          {
            action: 'upsert-overlay',
            overlay: { id: 'ok', kind: 'note', position: { x: 0, y: 0 } },
          },
        ]}
      />
    );
    for (const call of hoisted.setNodes.mock.calls) {
      const produced = typeof call[0] === 'function' ? call[0]([]) : call[0];
      if (!Array.isArray(produced)) continue;
      expect(produced.every((n) => n && typeof n.id === 'string' && n.id)).toBe(true);
    }
  });
});

describe('a malformed group in a restored session', () => {
  // The sibling of the overlay restore, and the one this branch first missed:
  // it throws on `g.id` INSIDE an effect, so no error boundary can catch it and
  // the whole canvas unmounts. The failure the branch exists to prevent, for
  // the input it claimed to have closed.
  const groups = [
    { id: 'g-ok', position: { x: 0, y: 0 }, label: 'Team' },
    null,
    undefined,
    'a string',
    { position: { x: 1, y: 1 } },
    { id: '', position: { x: 2, y: 2 } },
  ];

  it('does not take the canvas down', () => {
    expect(() =>
      render(
        <GraphCanvas
          nodes={[{ id: 'n1', type: 'custom', position: { x: 0, y: 0 }, data: { label: 'Real' } }]}
          edges={[]}
          groupsToRestore={{ groups, parentIds: {} }}
        />
      )
    ).not.toThrow();
    expect(screen.getByTestId('react-flow')).toBeInTheDocument();
  });

  it('restores the group it can read and drops the rest', () => {
    render(<GraphCanvas nodes={[]} edges={[]} groupsToRestore={{ groups, parentIds: {} }} />);
    const produced = hoisted.setNodes.mock.calls
      .map((c) => (typeof c[0] === 'function' ? c[0]([]) : c[0]))
      .filter(Array.isArray)
      .find((list) => list.some((n) => n?.type === 'group'));
    expect(produced).toBeTruthy();
    expect(produced.filter((n) => n.type === 'group').map((n) => n.id)).toEqual(['g-ok']);
  });

  it('never re-parents a node onto a group that is not on the canvas', () => {
    // Re-parenting onto a group that does not exist is its own crash, thrown
    // from inside ReactFlow's store where no boundary reaches. Asserted as the
    // invariant rather than against one contrived input, since the ways to
    // produce a dangling parent will change as the group shape does.
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        groupsToRestore={{ groups, parentIds: { 'n-orphan': 'g-gone', n1: 'g-ok' } }}
      />
    );
    for (const call of hoisted.setNodes.mock.calls) {
      const produced = typeof call[0] === 'function' ? call[0]([]) : call[0];
      if (!Array.isArray(produced)) continue;
      const ids = new Set(produced.map((n) => n?.id));
      for (const n of produced) {
        if (n?.parentId) expect(ids.has(n.parentId)).toBe(true);
      }
    }
  });
});

describe('overlayToFlowNode / flowNodeToOverlay refuse what they cannot translate', () => {
  it.each([
    ['null', null],
    ['undefined', undefined],
    ['a string', 'nope'],
    ['no id', { kind: 'note', position: { x: 0, y: 0 } }],
    ['a non-string id', { id: 7, kind: 'note' }],
  ])('overlayToFlowNode returns null for %s', (_name, input) => {
    expect(overlayToFlowNode(input)).toBeNull();
  });

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['no id', { type: 'note', position: { x: 0, y: 0 } }],
    ['an empty id', { id: '', type: 'note' }],
  ])('flowNodeToOverlay returns null for %s', (_name, input) => {
    expect(flowNodeToOverlay(input)).toBeNull();
  });

  it('still translates an unknown kind rather than discarding it', () => {
    // Refusing input is for what cannot be represented at all. These
    // translators also run on already-built canvas nodes, where an unfamiliar
    // type is not a reason to throw the node away — a stored unknown kind is
    // dropped earlier, while normalising, and never reaches here.
    const node = overlayToFlowNode({ id: 'k', kind: 'wormhole', position: { x: 1, y: 2 } });
    expect(node).toMatchObject({ id: 'k', type: 'wormhole' });
  });
});

describe('AnnotationErrorBoundary', () => {
  const Boom = () => {
    throw new Error('cannot draw this');
  };

  it('replaces a failing annotation with a placeholder and reports it', () => {
    const notifyRenderFailure = vi.fn();
    // React logs the caught error; silence it so a passing test is not noisy.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(
      <AnnotationContext.Provider
        value={{ notifyRenderFailure, labels: { brokenAnnotation: 'could not draw' } }}
      >
        <AnnotationErrorBoundary nodeId="bad-1">
          <Boom />
        </AnnotationErrorBoundary>
      </AnnotationContext.Provider>
    );
    spy.mockRestore();

    expect(screen.getByTestId('annotation-broken')).toBeInTheDocument();
    expect(screen.getByTestId('annotation-broken')).toHaveAttribute('title', 'could not draw');
    expect(notifyRenderFailure).toHaveBeenCalledWith('bad-1', expect.any(Error));
  });

  it('still draws the placeholder with no provider above it', () => {
    // The boundary must never be the thing that throws. AnnotationContext has
    // a default value, so what is missing without a provider is the reporter
    // key, not the context — the placeholder must appear either way.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() =>
      render(
        <AnnotationErrorBoundary nodeId="bad-2">
          <Boom />
        </AnnotationErrorBoundary>
      )
    ).not.toThrow();
    spy.mockRestore();
    expect(screen.getByTestId('annotation-broken')).toBeInTheDocument();
  });

  it('leaves a working annotation completely alone', () => {
    render(
      <AnnotationErrorBoundary nodeId="ok-1">
        <div data-testid="fine">fine</div>
      </AnnotationErrorBoundary>
    );
    expect(screen.getByTestId('fine')).toBeInTheDocument();
    expect(screen.queryByTestId('annotation-broken')).toBeNull();
  });
});
