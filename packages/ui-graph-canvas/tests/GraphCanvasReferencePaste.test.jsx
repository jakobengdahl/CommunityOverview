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
import { clipboardImageWillBeIngested } from '../src/components/GraphCanvas';

const hoisted = vi.hoisted(() => {
  const h = { setNodes: null, nodes: [], reactFlow: null };
  h.reactFlow = {
    fitView: () => {},
    zoomIn: () => {},
    zoomOut: () => {},
    getNodes: () => h.nodes,
    getEdges: () => [],
    setNodes: (u) => h.setNodes(u),
    setEdges: () => {},
    screenToFlowPosition: ({ x, y }) => ({ x, y }),
    setCenter: () => {},
    getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
  };
  return h;
});

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
    // A STABLE object, memoized across renders, because that is what reactflow
    // v11 does (`useViewportHelper` is a `useMemo`). A mock returning a fresh
    // literal each render gives `screenToFlowPosition` a new identity every
    // time, which re-runs EVERY effect that depends on it — and that hides
    // listener-ordering bugs, because all the listeners re-register together
    // and keep their relative order. The order-dependence round 2 found is
    // only visible when one effect re-runs alone.
    useReactFlow: () => hoisted.reactFlow,
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

  // A clipboardData the handler did not build: `items` but no `getData`. The
  // package's own image-paste tests dispatch exactly this shape, and an
  // uncaught throw in a document-level listener breaks every other paste
  // handler on the page, not just this one.
  it('survives a clipboardData with no getData method', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    const event = new Event('paste', { bubbles: true, cancelable: true });
    event.clipboardData = { items: [{ type: 'image/png', getAsFile: () => null }] };
    expect(() =>
      act(() => {
        document.dispatchEvent(event);
      })
    ).not.toThrow();
    expect(referenceNodes()).toHaveLength(0);
  });

  it('survives a paste with no clipboardData at all', () => {
    render(<GraphCanvas nodes={[]} edges={[]} />);
    const event = new Event('paste', { bubbles: true, cancelable: true });
    expect(() =>
      act(() => {
        document.dispatchEvent(event);
      })
    ).not.toThrow();
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

  // Copying an image out of a web app or chat client puts BOTH an image item
  // and a text/plain URL on the clipboard. Both paste listeners are
  // bubble-phase on `document`, so one paste used to ingest the image AND
  // drop a reference tile on top of it.
  it('does not also create a tile when the image handler claimed the paste', () => {
    const onImageIngest = vi.fn();
    render(<GraphCanvas nodes={[]} edges={[]} onImageIngest={onImageIngest} />);

    const file = new File([new Uint8Array([137, 80, 78, 71])], 'pic.png', {
      type: 'image/png',
    });
    const event = new Event('paste', { bubbles: true, cancelable: true });
    event.clipboardData = {
      items: [
        { type: 'image/png', getAsFile: () => file },
        { type: 'text/plain', getAsFile: () => null },
      ],
      getData: (type) => (type === 'text/plain' ? 'https://example.org/pic.png' : ''),
    };
    act(() => {
      document.dispatchEvent(event);
    });

    expect(referenceNodes()).toHaveLength(0);
  });

  // The guard must not depend on which listener registered first. The two
  // paste effects have different dependency arrays, and the image effect's
  // `onImageIngest` changes identity on a session switch — so it alone
  // re-runs and its listener moves to the end of the bubble order. Round 2 of
  // the review loop found the original `defaultPrevented`-only guard failing
  // exactly here, which the first-mount test above cannot see.
  it('does not create a tile after the image listener has been re-registered', () => {
    const { rerender } = render(<GraphCanvas nodes={[]} edges={[]} onImageIngest={vi.fn()} />);
    // A NEW onImageIngest identity, nothing else changed — what a session
    // switch does to this prop in the host.
    rerender(<GraphCanvas nodes={[]} edges={[]} onImageIngest={vi.fn()} />);

    const file = new File([new Uint8Array([137, 80, 78, 71])], 'pic.png', {
      type: 'image/png',
    });
    const event = new Event('paste', { bubbles: true, cancelable: true });
    event.clipboardData = {
      items: [
        { type: 'image/png', getAsFile: () => file },
        { type: 'text/plain', getAsFile: () => null },
      ],
      getData: (type) => (type === 'text/plain' ? 'https://example.org/pic.png' : ''),
    };
    act(() => {
      document.dispatchEvent(event);
    });

    expect(referenceNodes()).toHaveLength(0);
  });

  it('still creates a tile from a text-only paste when image ingest is wired', () => {
    // The guard must key off the event actually being consumed, not merely
    // off a host that happens to support images.
    const onImageIngest = vi.fn();
    render(<GraphCanvas nodes={[]} edges={[]} onImageIngest={onImageIngest} />);
    pasteText('https://example.org/handbook');
    expect(referenceNodes()).toHaveLength(1);
    expect(onImageIngest).not.toHaveBeenCalled();
  });

  it('works on a host that wired no image ingest', () => {
    // URL paste and image paste are independent: a host with no
    // onImageIngest must still be able to paste a link.
    render(<GraphCanvas nodes={[]} edges={[]} />);
    pasteText('https://example.org');
    expect(referenceNodes()).toHaveLength(1);
  });

  // Round 3 of the review loop: the image-item guard asked "is there an image
  // on the clipboard", which is not the same question as "will the image
  // handler take this paste". In both cases below the answer to the second is
  // no, and the coarse guard made the URL handler stand down anyway, so
  // nothing happened at all and the paste was silently lost.
  //
  // `pasteText` cannot see either one: it builds a clipboard with no image
  // item, so it never reaches the guard. That is why the test above
  // ('works on a host that wired no image ingest') passed throughout.
  function pasteImagePlusUrl(url, { reversed = false, file = null } = {}) {
    const imageItem = { type: 'image/png', getAsFile: () => file };
    const textItem = { type: 'text/plain', getAsFile: () => null };
    const event = new Event('paste', { bubbles: true, cancelable: true });
    event.clipboardData = {
      items: reversed ? [textItem, imageItem] : [imageItem, textItem],
      getData: (type) => (type === 'text/plain' ? url : ''),
    };
    act(() => {
      document.dispatchEvent(event);
    });
    return event;
  }

  it('creates a tile from an image+url clipboard when no image ingest is wired', () => {
    // The host has no image listener registered AT ALL (the image effect
    // returns before `addEventListener`), so there is nothing for this paste
    // to collide with. frontend/widget is such a host in this repo.
    const file = new File([new Uint8Array([137, 80, 78, 71])], 'pic.png', { type: 'image/png' });
    render(<GraphCanvas nodes={[]} edges={[]} />);

    pasteImagePlusUrl('https://example.org/pic.png', { file });

    expect(referenceNodes()).toHaveLength(1);
    expect(referenceNodes()[0].data.target).toBe('https://example.org/pic.png');
  });

  it('creates a tile when the image item yields no file', () => {
    // The image handler bails on a null `getAsFile()` WITHOUT calling
    // preventDefault, so if this handler also stands down the paste is lost.
    const onImageIngest = vi.fn();
    render(<GraphCanvas nodes={[]} edges={[]} onImageIngest={onImageIngest} />);

    pasteImagePlusUrl('https://example.org/pic.png', { file: null });

    expect(referenceNodes()).toHaveLength(1);
    expect(onImageIngest).not.toHaveBeenCalled();
  });

  // Round 3's mutation pass: both double-create tests above put the image item
  // FIRST, so narrowing the scan to `items[0]` left them green — and a test
  // that merely reverses the clipboard order cannot catch it either, because
  // the image listener is registered first and `defaultPrevented` covers for
  // the narrowed scan. Asserting the rule itself is what pins it: these need
  // no listener at all, so no ordering assumption can mask them.
  describe('clipboardImageWillBeIngested', () => {
    const file = new File([new Uint8Array([137, 80, 78, 71])], 'pic.png', { type: 'image/png' });
    const imageItem = { type: 'image/png', getAsFile: () => file };
    const emptyImageItem = { type: 'image/png', getAsFile: () => null };
    const textItem = { type: 'text/plain', getAsFile: () => null };
    const ingest = () => {};

    it('is true for an ingestible image wherever it sits in the list', () => {
      expect(clipboardImageWillBeIngested([imageItem, textItem], ingest)).toBe(true);
      expect(clipboardImageWillBeIngested([textItem, imageItem], ingest)).toBe(true);
      expect(clipboardImageWillBeIngested([textItem, textItem, imageItem], ingest)).toBe(true);
    });

    it('is false when the host wired no image ingest', () => {
      expect(clipboardImageWillBeIngested([imageItem, textItem], undefined)).toBe(false);
    });

    it('is false when the image item yields no file', () => {
      expect(clipboardImageWillBeIngested([emptyImageItem, textItem], ingest)).toBe(false);
    });

    it('is false for a clipboard with no image and for a missing list', () => {
      expect(clipboardImageWillBeIngested([textItem], ingest)).toBe(false);
      expect(clipboardImageWillBeIngested([], ingest)).toBe(false);
      expect(clipboardImageWillBeIngested(undefined, ingest)).toBe(false);
    });

    it('tolerates an item with no type', () => {
      expect(clipboardImageWillBeIngested([{ getAsFile: () => null }], ingest)).toBe(false);
    });
  });

  it('renders the pasted tile through the registered reference node type', () => {
    const { container } = render(<GraphCanvas nodes={[]} edges={[]} />);
    pasteText('https://example.org/handbook');
    expect(container.querySelector('.kind-reference')).toBeTruthy();
    expect(container.querySelector('.kind-reference.is-broken')).toBeNull();
  });
});
