import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

// The heat-map paths through GraphCanvas itself: the live drag outline, the
// minimum drawn size, click placement and the registered node type. Unlike
// GraphCanvasAnnotationToolbox.test.jsx, the viewport zoom here is variable,
// so the screen-to-flow conversion and the zoom-scaled preview floor are
// exercised rather than hidden behind a zoom of 1.
const hoisted = vi.hoisted(() => ({ zoom: 1, setNodes: null, nodes: [] }));

vi.mock('reactflow', async () => {
  const React = await vi.importActual('react');
  const MockReactFlow = ({ children, nodes, nodeTypes }) => (
    <div data-testid="react-flow" className="react-flow">
      <div data-testid="pane" className="react-flow__pane" />
      {(nodes || []).map((n) => {
        const Type = nodeTypes?.[n.type];
        return Type ? (
          <Type key={n.id} id={n.id} type={n.type} data={n.data} selected={!!n.selected} />
        ) : null;
      })}
      {children}
    </div>
  );
  return {
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    useNodesState: (initial) => {
      const [nodes, setNodes] = React.useState(initial || []);
      hoisted.nodes = nodes;
      hoisted.setNodes = setNodes;
      return [nodes, setNodes, vi.fn()];
    },
    useEdgesState: (initial) => [initial || [], vi.fn(), vi.fn()],
    useReactFlow: () => ({
      fitView: vi.fn(),
      zoomIn: vi.fn(),
      zoomOut: vi.fn(),
      getNodes: () => hoisted.nodes,
      getEdges: () => [],
      setNodes: (u) => hoisted.setNodes(u),
      setEdges: vi.fn(),
      screenToFlowPosition: ({ x, y }) => ({ x: x / hoisted.zoom, y: y / hoisted.zoom }),
      setCenter: vi.fn(),
      getViewport: () => ({ x: 0, y: 0, zoom: hoisted.zoom }),
    }),
    useOnSelectionChange: () => {},
    useStore: () => 1,
    Background: () => null,
    Controls: () => null,
    MiniMap: () => null,
    NodeResizer: () => null,
    Handle: () => null,
    Position: { Top: 'top', Bottom: 'bottom', Left: 'left', Right: 'right' },
    SelectionMode: { Partial: 'partial' },
  };
});

function pointerEvent(type, { clientX = 0, clientY = 0 } = {}) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX, clientY });
  Object.defineProperty(event, 'pointerId', { value: 1 });
  Object.defineProperty(event, 'pointerType', { value: 'mouse' });
  return event;
}

function armTool(name) {
  fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
  fireEvent.click(screen.getByRole('button', { name }));
}

function press(x, y) {
  fireEvent(screen.getByTestId('pane'), pointerEvent('pointerdown', { clientX: x, clientY: y }));
}

function move(x, y) {
  fireEvent(screen.getByTestId('pane'), pointerEvent('pointermove', { clientX: x, clientY: y }));
}

function release(x, y) {
  fireEvent(screen.getByTestId('pane'), pointerEvent('pointerup', { clientX: x, clientY: y }));
}

const created = (type) => hoisted.nodes.find((n) => n.type === type);

function previewBox() {
  const el = screen.getByTestId('placement-preview');
  return {
    display: el.style.display,
    left: el.style.left,
    top: el.style.top,
    width: el.style.width,
    height: el.style.height,
    borderRadius: el.style.borderRadius,
  };
}

describe('GraphCanvas heat-map placement', () => {
  beforeEach(() => {
    hoisted.zoom = 1;
    hoisted.nodes = [];
    localStorage.clear();
  });

  it('outlines a drag as a round square from the press point, sized by the longer side', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    armTool(/^heat map$/i);
    press(100, 100);
    move(160, 300);
    expect(previewBox()).toEqual({
      display: 'block',
      left: '100px',
      top: '100px',
      width: '200px',
      height: '200px',
      borderRadius: '50%',
    });
  });

  it('floors the outline at the minimum size scaled by zoom, flipping only past the threshold', () => {
    hoisted.zoom = 2;
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    armTool(/^heat map$/i);
    press(100, 100);
    // Left by 10 (past the 6px threshold), up by 3 (within it): only x flips.
    move(90, 97);
    expect(previewBox()).toEqual({
      display: 'block',
      left: '20px',
      top: '100px',
      width: '80px',
      height: '80px',
      borderRadius: '50%',
    });
  });

  it('does not flip the outline for a few pixels of sideways jitter', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    armTool(/^heat map$/i);
    press(100, 100);
    move(97, 300);
    expect(previewBox()).toMatchObject({ left: '100px', top: '100px', width: '200px' });
  });

  it('outlines a note drag as the plain swept box, without rounding', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    armTool(/^note$/i);
    press(300, 300);
    move(100, 200);
    expect(previewBox()).toEqual({
      display: 'block',
      left: '100px',
      top: '200px',
      width: '200px',
      height: '100px',
      borderRadius: '',
    });
  });

  it('hides the outline again on release', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    armTool(/^heat map$/i);
    press(100, 100);
    move(160, 300);
    release(160, 300);
    expect(previewBox().display).toBe('none');
  });

  it('draws a short drag as a minimum 40x40 circle at the press point', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    armTool(/^heat map$/i);
    press(100, 100);
    move(110, 105);
    release(110, 105);
    const heatmap = created('heatmap');
    expect(heatmap.style).toEqual({ width: 40, height: 40 });
    expect(heatmap.position).toEqual({ x: 100, y: 100 });
  });

  it('puts a short up-left drag minimum circle up-left of the press point', () => {
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    armTool(/^heat map$/i);
    press(100, 100);
    move(90, 92);
    release(90, 92);
    const heatmap = created('heatmap');
    expect(heatmap.style).toEqual({ width: 40, height: 40 });
    expect(heatmap.position).toEqual({ x: 60, y: 60 });
  });

  it('sizes a drawn circle in flow units, not screen pixels, when zoomed', () => {
    hoisted.zoom = 2;
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    armTool(/^heat map$/i);
    press(100, 100);
    move(300, 500);
    release(300, 500);
    const heatmap = created('heatmap');
    expect(heatmap.style).toEqual({ width: 200, height: 200 });
    expect(heatmap.position).toEqual({ x: 50, y: 50 });
  });

  it('places a clicked heat-map circle with its corner at the clicked flow point', () => {
    hoisted.zoom = 2;
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={vi.fn()} />);
    armTool(/^heat map$/i);
    press(120, 90);
    release(120, 90);
    const heatmap = created('heatmap');
    expect(heatmap.position).toEqual({ x: 60, y: 45 });
    expect(heatmap.style).toEqual({ width: 160, height: 160 });
  });

  it('renders a restored heat-map annotation through its registered node type', () => {
    const { container } = render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        annotationsToRestore={[
          {
            id: 'h9',
            kind: 'heatmap',
            position: { x: 0, y: 0 },
            size: { w: 200, h: 200 },
            intensity: 7,
          },
        ]}
      />
    );
    const circle = container.querySelector('.kind-heatmap');
    expect(circle).toBeTruthy();
    expect(circle.getAttribute('data-intensity')).toBe('7');
  });
});
