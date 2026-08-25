import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { GraphCanvas } from '../src/index';
import { overlayToFlowNode, flowNodeToOverlay } from '../src/utils/annotations';
import AnnotationErrorBoundary from '../src/components/AnnotationErrorBoundary';
import { AnnotationContext } from '../src/components/AnnotationContext';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn(), nodes: [] }));

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
  ])('flowNodeToOverlay returns null for %s', (_name, input) => {
    expect(flowNodeToOverlay(input)).toBeNull();
  });

  it('still translates an unknown kind rather than discarding it', () => {
    // Refusing input is for what cannot be represented at all. A kind this
    // version does not know is still a real annotation with a real id, and
    // dropping it here would silently delete it on the next save.
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

  it('still draws the placeholder when there is no context to report to', () => {
    // The boundary must never be the thing that throws. A node rendered
    // outside a provider (a test, a future host) still degrades.
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
