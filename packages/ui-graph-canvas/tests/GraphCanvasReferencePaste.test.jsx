// The human GUI creation path for a `url` reference: pasting a web address
// onto the canvas (docs/ANNOTATION_CONTRACT.md's "Reference tiles"). Unlike
// image paste (GraphCanvasImageIngest.test.jsx), this needs no host round
// trip — the tile is created in local node state like every other annotation
// kind the toolbox makes — so these tests assert on the node that appears.
//
// The gate matters as much as the creation: a paste must never be able to
// produce a reference the canvas would then draw as broken, which is what
// keeps an unsafe address from landing on the canvas as a tile at all.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

const hoisted = vi.hoisted(() => ({ setNodes: null, nodes: [] }));

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
    SelectionMode: { Partial: 'partial' },
  };
});

// Dispatched manually rather than through fireEvent: a plain Event freely
// accepts the extra `clipboardData` property jsdom's ClipboardEvent does not
// expose, the same reason GraphCanvasImageIngest.test.jsx builds its paste
// events by hand. Wrapped in `act` so the node state the handler sets is
// flushed before the assertions read it.
function pasteText(text, target = document) {
  const event = new Event('paste', { bubbles: true, cancelable: true });
  event.clipboardData = {
    items: [{ type: 'text/plain', getAsFile: () => null }],
    getData: (type) => (type === 'text/plain' ? text : ''),
  };
  act(() => {
    target.dispatchEvent(event);
  });
  return event;
}

function referenceNodes() {
  return hoisted.nodes.filter((n) => n.type === 'reference');
}

describe('GraphCanvas reference paste', () => {
  beforeEach(() => {
    hoisted.nodes = [];
    hoisted.setNodes = null;
  });

  it('creates a url reference from a pasted web address', () => {
    const onAnnotationChange = vi.fn();
    render(<GraphCanvas nodes={[]} edges={[]} onAnnotationChange={onAnnotationChange} />);

    pasteText('https://example.org/handbook');

    const created = referenceNodes();
    expect(created).toHaveLength(1);
    expect(created[0].data.target_kind).toBe('url');
    expect(created[0].data.target).toBe('https://example.org/handbook');
    // No invented label: the tile shows the address until the author renames
    // it, which is the honest thing for a paste.
    expect(created[0].data.label).toBe('');
    expect(onAnnotationChange).toHaveBeenCalledWith('create');
  });

  it('gives the pasted tile a box so it draws something', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    pasteText('https://example.org');
    expect(referenceNodes()[0].style).toEqual({ width: 220, height: 72 });
  });

  it('trims surrounding whitespace off the pasted address', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    pasteText('  https://example.org/a\n');
    expect(referenceNodes()[0].data.target).toBe('https://example.org/a');
  });

  it('consumes the paste event only when it created something', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    const created = pasteText('https://example.org');
    expect(created.defaultPrevented).toBe(true);

    const ignored = pasteText('just some notes I copied');
    expect(ignored.defaultPrevented).toBe(false);
  });

  it.each([
    'javascript:alert(1)',
    'JavaScript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'file:///etc/passwd',
    'vbscript:msgbox(1)',
    'java\tscript:alert(1)',
    '//evil.example/x',
    '/admin/delete-everything',
    'about:blank',
    'blob:https://example.org/x',
    'ftp://example.org/x',
  ])('never creates a tile from pasting %j', (text) => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    pasteText(text);
    expect(referenceNodes()).toHaveLength(0);
  });

  it('ignores a paste of ordinary prose', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    pasteText('Remember to ask about the handbook');
    expect(referenceNodes()).toHaveLength(0);
  });

  it('ignores a paste of prose that merely mentions an address', () => {
    // Only a clipboard that is, on its own, an address creates anything —
    // otherwise copying a sentence would litter the canvas.
    render(<GraphCanvas nodes={[]} edges={[]} />);
    pasteText('see https://example.org for details');
    expect(referenceNodes()).toHaveLength(0);
  });

  it('ignores an empty clipboard', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    pasteText('');
    expect(referenceNodes()).toHaveLength(0);
  });

  it('does not intercept a paste aimed at an ordinary text field', () => {
    render(
      <div>
        <input data-testid="some-input" />
        <GraphCanvas nodes={[]} edges={[]} />
      </div>
    );
    pasteText('https://example.org', screen.getByTestId('some-input'));
    expect(referenceNodes()).toHaveLength(0);
  });

  it('works on a host that wired no image ingest', () => {
    // URL paste and image paste are independent: a host with no
    // onImageIngest must still be able to paste a link.
    render(<GraphCanvas nodes={[]} edges={[]} />);
    pasteText('https://example.org');
    expect(referenceNodes()).toHaveLength(1);
  });

  it('renders the pasted tile through the registered reference node type', () => {
    const { container } = render(<GraphCanvas nodes={[]} edges={[]} />);
    pasteText('https://example.org/handbook');
    expect(container.querySelector('.kind-reference')).toBeTruthy();
    expect(container.querySelector('.kind-reference.is-broken')).toBeNull();
  });
});
