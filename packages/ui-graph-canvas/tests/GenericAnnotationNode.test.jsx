import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import GenericAnnotationNode, {
  regularShapeAspect,
  newShapeSize,
  SHAPE_BASE_WIDTH,
} from '../src/components/GenericAnnotationNode';
import { AnnotationContext } from '../src/components/AnnotationContext';

const hoisted = vi.hoisted(() => ({ resizerProps: [], setNodes: vi.fn(), nodes: [] }));

vi.mock('reactflow', () => ({
  NodeResizer: (props) => {
    hoisted.resizerProps.push(props);
    return <div data-testid="resizer" />;
  },
  useReactFlow: () => ({ setNodes: hoisted.setNodes, getNodes: () => hoisted.nodes }),
}));

// Applies the latest setNodes(updater) call to a single-node array and
// returns the updated node, the same helper style GraphCanvasUndo.test.jsx
// uses for its live node store.
function applyLatestUpdate(node) {
  const call = hoisted.setNodes.mock.calls.at(-1);
  return call[0]([node])[0];
}

// A simple, generic visual representation for the v1 annotation types that
// have no dedicated per-type editor yet (text, frame, shape, icon, vote_dot,
// image) — see docs/ANNOTATION_CONTRACT.md. These render so annotations
// created via MCP/session state are visible on the canvas instead of being
// store/MCP-only. ReactFlow supplies the node's registered type (e.g. "text")
// as the `type` prop, matching how GraphCanvas's nodeTypes map wires this
// component up for each of the six kinds. Selection outline and, for the
// sized kinds (frame/shape/image), model-space resize via NodeResizer are
// this component's direct-manipulation surface; per-type property editors
// stay out of scope for v1.
describe('GenericAnnotationNode', () => {
  beforeEach(() => {
    hoisted.resizerProps.length = 0;
    hoisted.setNodes.mockClear();
    hoisted.nodes = [];
  });

  it('renders text content', () => {
    render(<GenericAnnotationNode type="text" data={{ text: 'Hello canvas' }} />);
    expect(screen.getByText('Hello canvas')).toBeInTheDocument();
  });

  it('renders a frame as a bordered box', () => {
    const { container } = render(
      <GenericAnnotationNode type="frame" data={{ color: '#4ADE80' }} />
    );
    expect(container.querySelector('.kind-frame')).toBeTruthy();
  });

  it('renders a shape, distinguishing circle from rectangle by class', () => {
    const { container } = render(
      <GenericAnnotationNode type="shape" data={{ shape: 'circle', color: '#60A5FA' }} />
    );
    expect(container.querySelector('.shape-circle')).toBeTruthy();
  });

  // The regression this pins: triangle, rhombus, hexagon and process_arrow
  // used to reach the canvas as accepted content.shape values but painted as
  // plain rectangles, because only circle had a rule of its own. A class name
  // alone never caught that, so each variant's drawn geometry is asserted.
  it('draws every accepted shape variant as its own geometry, not as a rectangle', () => {
    const shapes = ['rectangle', 'circle', 'triangle', 'rhombus', 'hexagon', 'process_arrow'];
    const signatures = shapes.map((shape) => {
      const { container } = render(<GenericAnnotationNode type="shape" data={{ shape }} />);
      const el = container.querySelector(`.shape-${shape}`);
      expect(el).toBeTruthy();
      return `${el.style.clipPath}|${el.style.borderRadius}`;
    });
    expect(new Set(signatures).size).toBe(shapes.length);
    for (const signature of signatures.slice(1)) {
      expect(signature).not.toBe(signatures[0]);
    }
  });

  it('falls back to the rectangle geometry for an unknown shape name', () => {
    const { container } = render(<GenericAnnotationNode type="shape" data={{ shape: 'star' }} />);
    const el = container.querySelector('.shape-star');
    expect(el.style.clipPath).toBe('');
    expect(el.style.borderRadius).toBe('');
  });

  // The shape name comes from an annotation's content, so a name that collides
  // with an inherited Object member must not resolve to that member.
  it('falls back to the rectangle geometry for a shape name from Object.prototype', () => {
    const { container } = render(
      <GenericAnnotationNode type="shape" data={{ shape: 'constructor' }} />
    );
    const el = container.querySelector('.shape-constructor');
    expect(el.style.clipPath).toBe('');
    expect(el.style.borderRadius).toBe('');
  });

  // A clip-path clips the selection outline away, so a clipped shape needs
  // selection feedback that survives clipping — otherwise a selected locked
  // triangle (no resize handles either) looks exactly like an unselected one.
  // An element's own filter renders before its clip-path, so the halo has to
  // sit on an ancestor that is not itself clipped; asserting only that some
  // element carries a drop-shadow would pass on a halo that is clipped away.
  it.each(['triangle', 'rhombus', 'hexagon', 'process_arrow', 'rectangle'])(
    'gives a selected %s a selection halo on an unclipped ancestor',
    (shape) => {
      const { container } = render(
        <GenericAnnotationNode type="shape" data={{ shape, locked: true }} selected />
      );
      const clipped = container.querySelector(`.shape-${shape}`);
      const halo = clipped.parentElement;
      expect(halo.style.filter).toContain('drop-shadow');
      expect(halo.style.clipPath).toBe('');
      expect(halo.contains(clipped)).toBe(true);
      expect(clipped).not.toBe(halo);
    }
  );

  it('leaves an unselected shape unfiltered', () => {
    const { container } = render(
      <GenericAnnotationNode type="shape" data={{ shape: 'triangle' }} selected={false} />
    );
    expect(container.querySelector('[data-testid="shape-halo"]').style.filter).toBe('');
  });

  it('renders the configured icon as its glyph, not an abbreviation of its name', () => {
    render(<GenericAnnotationNode type="icon" data={{ icon: 'flag' }} />);
    const badge = screen.getByTitle('flag');
    expect(badge.textContent).toBe('⚑');
    expect(badge.textContent).not.toBe('fl');
  });

  it.each([
    ['FlagFill', '⚑'],
    // A first word that is a single capital would collapse into the next word
    // ("xcircle_fill") without the second camel-boundary rule, and lose its
    // alias — drawing the "XC" abbreviation instead of the cross.
    ['XCircleFill', '✖'],
    ['x-circle-fill', '✖'],
  ])('accepts the Bootstrap-style spelling %s', (icon, glyph) => {
    render(<GenericAnnotationNode type="icon" data={{ icon }} />);
    expect(screen.getByTitle(icon).textContent).toBe(glyph);
  });

  // The set now has a canonical or aliased entry for every one of the host
  // registry's 75 icon names (see docs/ANNOTATION_CONTRACT.md's icon
  // acceptance-matrix cell), so a real host-registry name never falls back to
  // the abbreviation any more - only a name outside that vocabulary does.
  it.each([
    ['no-such-icon', 'no'],
    ['not-a-real-icon-either', 'no'],
  ])(
    'abbreviates the out-of-vocabulary icon name %s instead of drawing a generic dot',
    (icon, text) => {
      render(<GenericAnnotationNode type="icon" data={{ icon }} />);
      const badge = screen.getByTitle(icon);
      expect(badge.textContent).toBe(text);
      expect(badge.className).toContain('kind-icon-abbreviated');
    }
  );

  // Previously unmapped names that used to collapse into the same
  // two-character abbreviation (DatabaseFill/GearFill/PeopleFill all used to
  // draw letters starting "Da"/"Ge"/"Pe" - distinct then, but Diagram2Fill and
  // Diagram3Fill both drew "Di") now each draw their own configured glyph.
  it.each([
    ['DatabaseFill', '\u{1F5C4}'],
    ['GearFill', '⚙'],
    ['PeopleFill', '\u{1F465}'],
    ['Diagram2Fill', '\u{1F578}'],
    ['Diagram3Fill', '\u{1F9EC}'],
  ])(
    'renders the host-registry icon name %s as its configured glyph, not an abbreviation',
    (icon, glyph) => {
      render(<GenericAnnotationNode type="icon" data={{ icon }} />);
      const badge = screen.getByTitle(icon);
      expect(badge.textContent).toBe(glyph);
      expect(badge.className).not.toContain('kind-icon-abbreviated');
    }
  );

  it('keeps out-of-vocabulary names apart that one default glyph would have merged', () => {
    const names = ['AlphaThing', 'BetaThing', 'GammaThing'];
    const drawn = names.map((icon) => {
      render(<GenericAnnotationNode type="icon" data={{ icon }} />);
      return screen.getByTitle(icon).textContent;
    });
    expect(new Set(drawn).size).toBe(names.length);
  });

  // Not a claim that every pair of out-of-vocabulary names stays distinct -
  // the two-character abbreviation still collides on a shared prefix
  // (FileEarmark* all drew "Fi" before the set existed too). The property is
  // narrower: the fallback never regresses distinguishability below what it
  // was before the set existed.
  it('still abbreviates two out-of-vocabulary names sharing their first two characters to the same mark', () => {
    for (const icon of ['FooBarThing', 'FooQuxThing']) {
      render(<GenericAnnotationNode type="icon" data={{ icon }} />);
      expect(screen.getByTitle(icon).textContent).toBe('Fo');
    }
  });

  it('does not mark a real glyph as an abbreviation', () => {
    render(<GenericAnnotationNode type="icon" data={{ icon: 'flag' }} />);
    expect(screen.getByTitle('flag').className).not.toContain('kind-icon-abbreviated');
  });

  // The icon name comes from an annotation's content: an inherited Object
  // member must not resolve to that member (which React would refuse to
  // render) - it falls through to the abbreviation like any unmapped name.
  it.each([
    ['constructor', 'co'],
    ['toString', 'to'],
    ['hasOwnProperty', 'ha'],
    ['__proto__', '__'],
  ])('renders a plain abbreviation for the icon name %s', (icon, text) => {
    render(<GenericAnnotationNode type="icon" data={{ icon }} />);
    expect(screen.getByTitle(icon).textContent).toBe(text);
  });

  it('renders the default glyph when an annotation has no icon name at all', () => {
    const { container } = render(<GenericAnnotationNode type="icon" data={{}} />);
    expect(container.querySelector('.kind-icon').textContent).toBe('●');
  });

  // A shape's rotation goes on the same wrapper as its selection halo, so the
  // halo rotates with the shape instead of staying axis-aligned around it.
  it('draws a rotation on a shape, on the wrapper that also carries its halo', () => {
    const { container } = render(
      <GenericAnnotationNode type="shape" data={{ shape: 'triangle', rotation: 45 }} />
    );
    const halo = container.querySelector('[data-testid="shape-halo"]');
    expect(halo.style.transform).toBe('rotate(45deg)');
    expect(halo.querySelector('.shape-triangle')).toBeTruthy();
  });

  it.each(['text', 'icon', 'vote_dot'])('draws a rotation on a %s', (type) => {
    const { container } = render(
      <GenericAnnotationNode type={type} data={{ rotation: 45, text: 'x' }} />
    );
    expect(container.querySelector('.graph-generic-annotation-node').style.transform).toBe(
      'rotate(45deg)'
    );
  });

  // A frame is one box like a shape, and the MCP tools accept a rotation for
  // it with no per-type validation, so a stored rotation is drawn rather than
  // silently discarded while list_annotations keeps reporting it.
  //
  // smallfix-annotation-rotated-resize-handles: the rotation now lives on the
  // wrapper that also carries the resize handles (rather than on `.kind-frame`
  // itself), so the handles rotate along with the box instead of staying
  // axis-aligned around its unrotated bounds.
  it('draws a rotation on a frame, on the wrapper that also carries its resizer', () => {
    const { container } = render(<GenericAnnotationNode type="frame" data={{ rotation: 45 }} />);
    const wrap = container.querySelector('.graph-generic-annotation-rotate-wrap');
    expect(wrap.style.transform).toBe('rotate(45deg)');
    expect(wrap.querySelector('.kind-frame')).toBeTruthy();
  });

  it('rotates an image annotation, on the wrapper that also carries its resizer', () => {
    const { container } = render(
      <GenericAnnotationNode
        type="image"
        data={{ image: { url: 'https://example.com/a.png' }, alt: 'diagram', rotation: 90 }}
      />
    );
    const wrap = container.querySelector('.graph-generic-annotation-rotate-wrap');
    expect(wrap.style.transform).toBe('rotate(90deg)');
    expect(wrap.contains(screen.getByAltText('diagram'))).toBe(true);
  });

  it.each(['frame', 'shape', 'image'])(
    'rotates the resize handles along with a rotated, selected %s',
    (type) => {
      const data = { rotation: 30, shape: 'circle', image: { url: 'https://example.com/a.png' } };
      const { container } = render(<GenericAnnotationNode type={type} data={data} selected />);
      const resizer = screen.getByTestId('resizer');
      const rotatedAncestor =
        container.querySelector('.graph-generic-annotation-rotate-wrap') ||
        container.querySelector('.graph-generic-annotation-shape-halo');
      expect(rotatedAncestor.style.transform).toBe('rotate(30deg)');
      expect(rotatedAncestor.contains(resizer)).toBe(true);
    }
  );

  it('renders a vote dot with its value', () => {
    render(<GenericAnnotationNode type="vote_dot" data={{ value: 3 }} />);
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  it('renders an image by URL', () => {
    render(
      <GenericAnnotationNode
        type="image"
        data={{ image: { url: 'https://example.com/a.png' }, alt: 'diagram' }}
      />
    );
    const img = screen.getByAltText('diagram');
    expect(img.tagName).toBe('IMG');
    expect(img.src).toBe('https://example.com/a.png');
  });

  it('renders a placeholder (no img element) when an image has no URL', () => {
    render(<GenericAnnotationNode type="image" data={{ alt: 'missing' }} />);
    expect(screen.queryByRole('img')).toBeNull();
    expect(screen.getByText('missing')).toBeInTheDocument();
  });

  it('adds a selected outline class when selected', () => {
    const { container, rerender } = render(
      <GenericAnnotationNode type="icon" data={{ icon: 'flag' }} selected={false} />
    );
    expect(container.querySelector('.selected')).toBeNull();
    rerender(<GenericAnnotationNode type="icon" data={{ icon: 'flag' }} selected />);
    expect(container.querySelector('.selected')).toBeTruthy();
  });

  it.each(['frame', 'shape', 'image'])(
    'shows resize handles for a selected, unlocked %s',
    (type) => {
      render(<GenericAnnotationNode type={type} data={{}} selected />);
      const props = hoisted.resizerProps.at(-1);
      expect(props.isVisible).toBe(true);
    }
  );

  it.each(['text', 'icon', 'vote_dot'])(
    'renders no resizer for %s (fixed intrinsic size)',
    (type) => {
      render(<GenericAnnotationNode type={type} data={{}} selected />);
      expect(screen.queryByTestId('resizer')).toBeNull();
    }
  );

  it('hides resize handles for a locked annotation even when selected', () => {
    render(<GenericAnnotationNode type="frame" data={{ locked: true }} selected />);
    const props = hoisted.resizerProps.at(-1);
    expect(props.isVisible).toBe(false);
  });

  // task-annotation-shared-session-realtime: another client's live selection
  // claim makes an annotation's lease exclusive, the same way a persisted
  // `locked` flag already hides the resize handles.
  it('hides resize handles while another client holds the selection claim', () => {
    render(
      <GenericAnnotationNode
        type="frame"
        data={{ remoteSelection: { clientId: 'c2', color: '#e6194b', displayName: 'Ada' } }}
        selected
      />
    );
    const props = hoisted.resizerProps.at(-1);
    expect(props.isVisible).toBe(false);
  });

  it('hides resize handles when not selected', () => {
    render(<GenericAnnotationNode type="shape" data={{}} selected={false} />);
    const props = hoisted.resizerProps.at(-1);
    expect(props.isVisible).toBe(false);
  });

  it('notifies the annotation context after a resize', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider value={{ notifyChange, labels: {} }}>
        <GenericAnnotationNode type="frame" data={{}} selected />
      </AnnotationContext.Provider>
    );
    const props = hoisted.resizerProps.at(-1);
    props.onResizeEnd();
    expect(notifyChange).toHaveBeenCalledTimes(1);
  });

  // smallfix-annotation-rotated-resize-handles: a rotated frame/shape/image
  // must grow along its own axes, not the canvas's - see
  // resolveRotatedResizeGeometry.test.js for the underlying math.
  it('remaps a resize gesture on a rotated frame through its rotation', () => {
    render(<GenericAnnotationNode id="f1" type="frame" data={{ rotation: 90 }} selected />);
    const props = hoisted.resizerProps.at(-1);
    props.onResizeStart(null, { x: 0, y: 0, width: 100, height: 50 });
    props.onResizeEnd(null, { x: 0, y: 0, width: 150, height: 80 });
    const updated = applyLatestUpdate({
      id: 'f1',
      position: { x: 0, y: 0 },
      style: { width: 100, height: 50 },
    });
    expect(updated.position.x).toBeCloseTo(-40, 9);
    expect(updated.position.y).toBeCloseTo(10, 9);
    expect(updated.style.width).toBe(150);
    expect(updated.style.height).toBe(80);
  });

  // At rotation 0 the correction is the identity, so an unrotated resize
  // keeps behaving exactly as it did before this fix.
  it('leaves an unrotated resize gesture unchanged', () => {
    render(<GenericAnnotationNode id="f1" type="frame" data={{}} selected />);
    const props = hoisted.resizerProps.at(-1);
    props.onResizeStart(null, { x: 5, y: 5, width: 100, height: 50 });
    props.onResizeEnd(null, { x: 5, y: 5, width: 160, height: 90 });
    const updated = applyLatestUpdate({
      id: 'f1',
      position: { x: 5, y: 5 },
      style: { width: 100, height: 50 },
    });
    expect(updated.position).toEqual({ x: 5, y: 5 });
    expect(updated.style).toEqual({ width: 160, height: 90 });
  });
});

// task-annotation-render-direct-manipulation: a right-click property editor
// (shape subtype picker + rotation control) for every kind this component
// renders — previously none of them had any GUI editor at all.
describe('GenericAnnotationNode property editor', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
  });

  it('opens a shape-subtype picker and rotation controls for a shape annotation', () => {
    render(<GenericAnnotationNode id="s1" type="shape" data={{ shape: 'circle' }} />);
    fireEvent.contextMenu(screen.getByTestId('shape-halo'));
    expect(screen.getByLabelText('circle')).toBeInTheDocument();
    expect(screen.getByLabelText('triangle')).toBeInTheDocument();
    expect(screen.getByLabelText('Rotate left 15°')).toBeInTheDocument();
  });

  it('marks the current shape as the active picker option', () => {
    render(<GenericAnnotationNode id="s1" type="shape" data={{ shape: 'hexagon' }} />);
    fireEvent.contextMenu(screen.getByTestId('shape-halo'));
    expect(screen.getByLabelText('hexagon').className).toContain('active');
    expect(screen.getByLabelText('circle').className).not.toContain('active');
  });

  it("changes an existing shape annotation's subtype from the picker", () => {
    render(<GenericAnnotationNode id="s1" type="shape" data={{ shape: 'circle' }} />);
    fireEvent.contextMenu(screen.getByTestId('shape-halo'));
    fireEvent.click(screen.getByLabelText('triangle'));
    expect(applyLatestUpdate({ id: 's1', data: { shape: 'circle' } }).data.shape).toBe('triangle');
  });

  it('does not show a shape picker for a non-shape kind', () => {
    const { container } = render(<GenericAnnotationNode id="f1" type="frame" data={{}} />);
    fireEvent.contextMenu(container.querySelector('.kind-frame'));
    expect(screen.queryByLabelText('circle')).toBeNull();
    expect(screen.getByLabelText('Rotate left 15°')).toBeInTheDocument();
  });

  // task-annotation-render-direct-manipulation remaining_scope: "no icon
  // picker for `icon`" — closed by reusing annotationIcons.js's full
  // vocabulary, the same pattern the shape subtype picker already
  // established.
  it('opens an icon picker over the full vocabulary for an icon annotation', () => {
    render(<GenericAnnotationNode id="i1" type="icon" data={{ icon: 'circle' }} />);
    fireEvent.contextMenu(screen.getByTitle('circle'));
    expect(screen.getByLabelText('flag')).toBeInTheDocument();
    expect(screen.getByLabelText('person_fill')).toBeInTheDocument();
    expect(screen.getByLabelText('Rotate left 15°')).toBeInTheDocument();
  });

  it('marks the current icon as the active picker option', () => {
    render(<GenericAnnotationNode id="i1" type="icon" data={{ icon: 'flag' }} />);
    fireEvent.contextMenu(screen.getByTitle('flag'));
    expect(screen.getByLabelText('flag').className).toContain('active');
    expect(screen.getByLabelText('circle').className).not.toContain('active');
  });

  it("changes an existing icon annotation's name from the picker", () => {
    render(<GenericAnnotationNode id="i1" type="icon" data={{ icon: 'circle' }} />);
    fireEvent.contextMenu(screen.getByTitle('circle'));
    fireEvent.click(screen.getByLabelText('flag'));
    expect(applyLatestUpdate({ id: 'i1', data: { icon: 'circle' } }).data.icon).toBe('flag');
  });

  it('does not show an icon picker for a non-icon kind', () => {
    const { container } = render(<GenericAnnotationNode id="f1" type="frame" data={{}} />);
    fireEvent.contextMenu(container.querySelector('.kind-frame'));
    expect(screen.queryByLabelText('flag')).toBeNull();
  });

  it('refuses the icon picker while another client holds the selection claim', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt, labels: {} }}
      >
        <GenericAnnotationNode
          id="i1"
          type="icon"
          data={{
            icon: 'circle',
            remoteSelection: { clientId: 'c2', color: '#e6194b', displayName: 'Ada' },
          }}
        />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByTitle('circle'));
    expect(screen.queryByLabelText('flag')).toBeNull();
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
  });

  // task-annotation-shared-session-realtime: an exclusive lease refuses even
  // opening the property editor while another client holds the claim, so an
  // in-progress rotation/shape edit can never be started against it.
  it('refuses to open the property editor while another client holds the selection claim, notifying instead', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    const { container } = render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt, labels: {} }}
      >
        <GenericAnnotationNode
          id="s1"
          type="shape"
          data={{
            shape: 'circle',
            remoteSelection: { clientId: 'c2', color: '#e6194b', displayName: 'Ada' },
          }}
        />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByTestId('shape-halo'));
    expect(screen.queryByLabelText('Rotate left 15°')).toBeNull();
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
    expect(container.querySelector('.graph-node-remote-badge').textContent).toBe('Ada');
  });

  it.each(['text', 'frame', 'shape', 'icon', 'vote_dot', 'image'])(
    'rotates right by 15° and normalizes into [0, 360) for a %s',
    (kind) => {
      const data = { rotation: 350, text: 'x', icon: 'flag', image: {} };
      const { container } = render(
        <GenericAnnotationNode id="n1" type={kind} data={data} selected />
      );
      const root = container.querySelector(
        kind === 'shape' ? '[data-testid="shape-halo"]' : `.kind-${kind}`
      );
      fireEvent.contextMenu(root);
      fireEvent.click(screen.getByLabelText('Rotate right 15°'));
      expect(applyLatestUpdate({ id: 'n1', data }).data.rotation).toBe(5);
    }
  );

  it('resets rotation to 0 via the reset button, showing the current angle on it', () => {
    render(<GenericAnnotationNode id="n1" type="text" data={{ rotation: 45, text: 'x' }} />);
    fireEvent.contextMenu(screen.getByText('x'));
    expect(screen.getByLabelText('Reset rotation').textContent).toBe('45°');
    fireEvent.click(screen.getByLabelText('Reset rotation'));
    expect(applyLatestUpdate({ id: 'n1', data: { rotation: 45 } }).data.rotation).toBe(0);
  });

  it('deletes the annotation via the context menu delete button', () => {
    render(<GenericAnnotationNode id="v1" type="vote_dot" data={{ value: 1 }} />);
    fireEvent.contextMenu(screen.getByText('1'));
    fireEvent.click(screen.getByText(/Delete/));
    const call = hoisted.setNodes.mock.calls.at(-1);
    expect(call[0]([{ id: 'v1' }, { id: 'other' }])).toEqual([{ id: 'other' }]);
  });

  it('notifies the annotation context after a rotation change', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{
          notifyChange,
          labels: { rotateReset: 'Reset rotation', delete: 'Delete', rotation: 'Rotation' },
        }}
      >
        <GenericAnnotationNode id="v1" type="vote_dot" data={{ value: 1 }} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByText('1'));
    fireEvent.click(screen.getByLabelText('Reset rotation'));
    expect(notifyChange).toHaveBeenCalledTimes(1);
  });

  // task-annotation-render-direct-manipulation remaining_scope: "No
  // color/recolor editor for any generic kind". The picker is offered only
  // for the kinds that actually paint `color` — `image` carries the field in
  // the model but nothing renders it.
  describe('colour editor', () => {
    it.each([
      ['text', (c) => c.querySelector('.kind-text'), { text: 'x' }],
      ['frame', (c) => c.querySelector('.kind-frame'), {}],
      ['icon', (c) => c.querySelector('.kind-icon'), { icon: 'circle' }],
      ['vote_dot', (c) => c.querySelector('.kind-vote_dot'), { value: 1 }],
    ])('offers colour swatches for a %s annotation', (kind, find, data) => {
      const { container } = render(<GenericAnnotationNode id="a1" type={kind} data={data} />);
      fireEvent.contextMenu(find(container));
      expect(screen.getByLabelText('#ef4444')).toBeInTheDocument();
    });

    it('offers colour swatches for a shape annotation', () => {
      render(<GenericAnnotationNode id="s1" type="shape" data={{ shape: 'circle' }} />);
      fireEvent.contextMenu(screen.getByTestId('shape-halo'));
      expect(screen.getByLabelText('#ef4444')).toBeInTheDocument();
    });

    it('offers no colour swatches for an image annotation, which paints no colour', () => {
      const { container } = render(
        <GenericAnnotationNode id="im1" type="image" data={{ image: { url: 'x.png' } }} />
      );
      fireEvent.contextMenu(container.querySelector('.kind-image'));
      expect(screen.queryByLabelText('#ef4444')).toBeNull();
      expect(screen.getByLabelText('Rotate left 15°')).toBeInTheDocument();
    });

    it("changes a shape's colour from the picker", () => {
      render(<GenericAnnotationNode id="s1" type="shape" data={{ shape: 'circle' }} />);
      fireEvent.contextMenu(screen.getByTestId('shape-halo'));
      fireEvent.click(screen.getByLabelText('#22c55e'));
      expect(applyLatestUpdate({ id: 's1', data: { shape: 'circle' } }).data.color).toBe('#22c55e');
    });

    it('marks the current colour as the active swatch', () => {
      render(<GenericAnnotationNode id="s1" type="shape" data={{ color: '#3b82f6' }} />);
      fireEvent.contextMenu(screen.getByTestId('shape-halo'));
      expect(screen.getByLabelText('#3b82f6').className).toContain('active');
      expect(screen.getByLabelText('#ef4444').className).not.toContain('active');
    });

    it('refuses a recolour while another client holds the claim', () => {
      const notifyRemoteLockedAttempt = vi.fn();
      render(
        <AnnotationContext.Provider
          value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt, labels: {} }}
        >
          <GenericAnnotationNode
            id="s1"
            type="shape"
            data={{ shape: 'circle', remoteSelection: { color: '#f00', displayName: 'Ada' } }}
          />
        </AnnotationContext.Provider>
      );
      fireEvent.contextMenu(screen.getByTestId('shape-halo'));
      expect(notifyRemoteLockedAttempt).toHaveBeenCalled();
      expect(hoisted.setNodes).not.toHaveBeenCalled();
    });
  });

  // task-annotation-render-direct-manipulation remaining_scope: "No way to
  // change a `vote_dot`'s value after creation" — it was MCP-only.
  describe('vote_dot value stepper', () => {
    it('raises and lowers the value', () => {
      render(<GenericAnnotationNode id="v1" type="vote_dot" data={{ value: 2 }} />);
      fireEvent.contextMenu(screen.getByText('2'));
      fireEvent.click(screen.getByLabelText('Increase value'));
      expect(applyLatestUpdate({ id: 'v1', data: { value: 2 } }).data.value).toBe(3);
      fireEvent.click(screen.getByLabelText('Decrease value'));
      expect(applyLatestUpdate({ id: 'v1', data: { value: 2 } }).data.value).toBe(1);
    });

    it('never counts below zero', () => {
      render(<GenericAnnotationNode id="v1" type="vote_dot" data={{ value: 0 }} />);
      fireEvent.contextMenu(screen.getByText('0'));
      fireEvent.click(screen.getByLabelText('Decrease value'));
      expect(applyLatestUpdate({ id: 'v1', data: { value: 0 } }).data.value).toBe(0);
    });

    it('treats a vote dot created without a value as zero', () => {
      const { container } = render(<GenericAnnotationNode id="v1" type="vote_dot" data={{}} />);
      fireEvent.contextMenu(container.querySelector('.kind-vote_dot'));
      fireEvent.click(screen.getByLabelText('Increase value'));
      expect(applyLatestUpdate({ id: 'v1', data: {} }).data.value).toBe(1);
    });

    it('offers no value stepper on a kind that has no value', () => {
      render(<GenericAnnotationNode id="s1" type="shape" data={{ shape: 'circle' }} />);
      fireEvent.contextMenu(screen.getByTestId('shape-halo'));
      expect(screen.queryByLabelText('Increase value')).toBeNull();
    });
  });

  // See docs/ANNOTATION_CONTRACT.md's "Layer order" for why the control is
  // bring-to-front/send-to-back rather than a one-step forward/back. The
  // arithmetic itself is covered by annotationLayers.test.js; these pin the
  // wiring into the menu.
  describe('layer controls', () => {
    it('writes an integer layer onto the annotation as zIndex', () => {
      hoisted.nodes = [
        { id: 'v1', type: 'vote_dot', zIndex: 0 },
        { id: 'other', type: 'note', zIndex: 1 },
      ];
      render(<GenericAnnotationNode id="v1" type="vote_dot" data={{ value: 1 }} />);
      fireEvent.contextMenu(screen.getByText('1'));
      fireEvent.click(screen.getByLabelText('Bring to front'));
      const updated = hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes);
      expect(updated[0].zIndex).toBe(2);
      expect(Number.isInteger(updated[0].zIndex)).toBe(true);
      expect(updated[1].zIndex).toBe(1);
    });

    it('refuses a layer change while another client holds the claim', () => {
      hoisted.nodes = [
        { id: 'v1', type: 'vote_dot', zIndex: 0 },
        { id: 'other', type: 'note', zIndex: 1 },
      ];
      const notifyRemoteLockedAttempt = vi.fn();
      render(
        <AnnotationContext.Provider
          value={{
            notifyChange: vi.fn(),
            notifyRemoteLockedAttempt,
            labels: { layer: 'Layer', layerFront: 'Bring to front' },
          }}
        >
          <GenericAnnotationNode
            id="v1"
            type="vote_dot"
            data={{ value: 1, remoteSelection: { color: '#f00', displayName: 'Ada' } }}
          />
        </AnnotationContext.Provider>
      );
      // A remote claim refuses the context menu outright, so the row is never
      // reachable — the attempt is surfaced rather than silently ignored.
      fireEvent.contextMenu(screen.getByText('1'));
      expect(notifyRemoteLockedAttempt).toHaveBeenCalled();
      expect(hoisted.setNodes).not.toHaveBeenCalled();
    });

    it('leaves the canvas untouched when the annotation is already at the front', () => {
      hoisted.nodes = [
        { id: 'v1', type: 'vote_dot', zIndex: 5 },
        { id: 'other', type: 'note', zIndex: 1 },
      ];
      const notifyChange = vi.fn();
      render(
        <AnnotationContext.Provider
          value={{ notifyChange, labels: { layer: 'Layer', layerFront: 'Bring to front' } }}
        >
          <GenericAnnotationNode id="v1" type="vote_dot" data={{ value: 1 }} />
        </AnnotationContext.Provider>
      );
      fireEvent.contextMenu(screen.getByText('1'));
      fireEvent.click(screen.getByLabelText('Bring to front'));
      expect(hoisted.setNodes).not.toHaveBeenCalled();
      expect(notifyChange).not.toHaveBeenCalled();
    });

    it('offers no layer controls on a locked annotation', () => {
      render(<GenericAnnotationNode id="v1" type="vote_dot" data={{ value: 1, locked: true }} />);
      fireEvent.contextMenu(screen.getByText('1'));
      expect(screen.queryByLabelText('Bring to front')).toBeNull();
      expect(screen.getByText(/Unlock/)).toBeInTheDocument();
    });
  });

  it('closes the context menu on Escape', async () => {
    render(<GenericAnnotationNode id="v1" type="vote_dot" data={{ value: 1 }} />);
    fireEvent.contextMenu(screen.getByText('1'));
    expect(screen.getByLabelText('Reset rotation')).toBeInTheDocument();
    // The dismiss listeners are wired up on a setTimeout(0) (so the very
    // contextmenu event that opened the menu doesn't immediately close it);
    // let it flush before the Escape keydown.
    await new Promise((resolve) => setTimeout(resolve, 0));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByLabelText('Reset rotation')).toBeNull();
  });
});

// smallfix-annotation-context-menus-ignore-lock: the accepted capability
// baseline is "a locked object remains selectable but offers only unlock or
// copy" - previously every kind's context menu offered full editing
// regardless of `data.locked`.
describe('GenericAnnotationNode locked context menu', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
  });

  it.each(['text', 'frame', 'shape', 'icon', 'vote_dot', 'image'])(
    'shows unlock and duplicate for a locked %s, hiding rotation/shape/delete',
    (kind) => {
      const data = { locked: true, text: 'x', icon: 'flag', image: {}, value: 1 };
      const { container } = render(
        <AnnotationContext.Provider
          value={{
            notifyChange: vi.fn(),
            labels: { unlock: 'Unlock', duplicate: 'Duplicate' },
          }}
        >
          <GenericAnnotationNode id="n1" type={kind} data={data} selected />
        </AnnotationContext.Provider>
      );
      const root =
        kind === 'shape'
          ? screen.getByTestId('shape-halo')
          : container.querySelector(`.kind-${kind}`);
      fireEvent.contextMenu(root);
      expect(screen.getByText(/Unlock/)).toBeInTheDocument();
      expect(screen.getByText(/Duplicate/)).toBeInTheDocument();
      expect(screen.queryByLabelText('Rotate left 15°')).toBeNull();
      expect(screen.queryByLabelText('circle')).toBeNull();
      expect(screen.queryByText(/Delete/)).toBeNull();
    }
  );

  it('unlocks a locked annotation, notifies the annotation context, and makes it draggable again', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider value={{ notifyChange, labels: { unlock: 'Unlock' } }}>
        <GenericAnnotationNode id="f1" type="frame" data={{ locked: true }} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.kind-frame'));
    fireEvent.click(screen.getByText(/Unlock/));
    const updated = applyLatestUpdate({ id: 'f1', data: { locked: true }, draggable: false });
    expect(updated.data.locked).toBe(false);
    expect(updated.draggable).toBe(true);
    expect(notifyChange).toHaveBeenCalledWith('style');
  });

  it('still shows the full property editor when unlocked', () => {
    render(<GenericAnnotationNode id="f1" type="frame" data={{ locked: false }} selected />);
    fireEvent.contextMenu(document.querySelector('.kind-frame'));
    expect(screen.getByLabelText('Rotate left 15°')).toBeInTheDocument();
    expect(screen.getByText(/Duplicate/)).toBeInTheDocument();
    expect(screen.getByText(/Delete/)).toBeInTheDocument();
  });

  // Resize/drag already refuse a locked annotation; the resizer must stay
  // hidden even if the (restricted) context menu is open at the same time.
  it('keeps resize handles hidden for a locked, selected frame', () => {
    render(<GenericAnnotationNode type="frame" data={{ locked: true }} selected />);
    expect(hoisted.resizerProps.at(-1).isVisible).toBe(false);
  });
});

// task-annotation-render-direct-manipulation / task-annotation-responsive-
// bottom-toolbox: duplication was MCP-only (`duplicate_annotation`) with no
// GUI action anywhere. See annotationDuplicateWiring.test.jsx for the
// shared-hook wiring pinned across every kind; this pins the six generic
// kinds' own (GenericAnnotationNode.test.jsx's own convention, same as
// "layer controls" above).
describe('GenericAnnotationNode duplicate control', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.nodes = [];
  });

  it.each(['text', 'frame', 'shape', 'icon', 'vote_dot', 'image'])(
    'duplicates a %s into a new, offset, unlocked copy and leaves the original untouched',
    (kind) => {
      const data = { text: 'x', icon: 'flag', color: '#94a3b8', value: 1 };
      const source = { id: 'n1', type: kind, position: { x: 10, y: 10 }, data };
      hoisted.nodes = [source];
      const { container } = render(<GenericAnnotationNode id="n1" type={kind} data={data} />);
      const root =
        kind === 'shape'
          ? screen.getByTestId('shape-halo')
          : container.querySelector(`.kind-${kind}`);
      fireEvent.contextMenu(root);
      fireEvent.click(screen.getByText(/Duplicate/));
      const updated = hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes);
      expect(updated).toHaveLength(2);
      const [original, copy] = updated;
      expect(original).toBe(source);
      expect(copy.id).not.toBe('n1');
      expect(copy.type).toBe(kind);
      expect(copy.position).not.toEqual(source.position);
      expect(copy.data.locked).toBe(false);
    }
  );

  it('duplicates a locked annotation into an unlocked copy, and leaves the source locked', () => {
    const data = { locked: true, text: 'x' };
    const source = { id: 'f1', type: 'frame', position: { x: 0, y: 0 }, data };
    hoisted.nodes = [source];
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), labels: { unlock: 'Unlock', duplicate: 'Duplicate' } }}
      >
        <GenericAnnotationNode id="f1" type="frame" data={data} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.kind-frame'));
    fireEvent.click(screen.getByText(/Duplicate/));
    const updated = hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes);
    const copy = updated.find((n) => n.id !== 'f1');
    expect(copy.data.locked).toBe(false);
    expect(updated.find((n) => n.id === 'f1').data.locked).toBe(true);
  });

  it('publishes the duplicate as a create', () => {
    const notifyChange = vi.fn();
    const data = { text: 'x' };
    hoisted.nodes = [{ id: 'n1', type: 'text', position: { x: 0, y: 0 }, data }];
    render(
      <AnnotationContext.Provider value={{ notifyChange, labels: { duplicate: 'Duplicate' } }}>
        <GenericAnnotationNode id="n1" type="text" data={data} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.kind-text'));
    fireEvent.click(screen.getByText(/Duplicate/));
    expect(notifyChange).toHaveBeenCalledWith('create');
  });
});

// task-annotation-doubleclick-to-edit-text: only note/label/group had
// double-click-to-edit; `text` (whose whole purpose is text) and every
// `shape` variant had no content-editing UI at all. This follows
// NoteNode/LabelNode's exact pattern: double-click to enter, blur/Escape to
// commit, live per-keystroke sync (the host's scheduler debounces 'text'
// changes to 300ms — see AnnotationContext's notifyChange doc comment).
describe('GenericAnnotationNode inline text editing', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
  });

  it('enters edit mode on double-click, showing a textarea seeded with the current text', () => {
    render(<GenericAnnotationNode id="t1" type="text" data={{ text: 'Hello' }} />);
    fireEvent.doubleClick(screen.getByText('Hello'));
    expect(screen.getByRole('textbox')).toHaveValue('Hello');
  });

  it('syncs every keystroke live and commits the trimmed value on blur', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider value={{ notifyChange, labels: {} }}>
        <GenericAnnotationNode id="t1" type="text" data={{ text: 'Hello' }} />
      </AnnotationContext.Provider>
    );
    fireEvent.doubleClick(screen.getByText('Hello'));
    const textarea = screen.getByRole('textbox');
    fireEvent.change(textarea, { target: { value: 'typing' } });
    expect(applyLatestUpdate({ id: 't1', data: { text: 'Hello' } }).data.text).toBe('typing');
    expect(notifyChange).toHaveBeenCalledWith('text');

    fireEvent.change(textarea, { target: { value: '  world  ' } });
    fireEvent.blur(textarea);
    expect(applyLatestUpdate({ id: 't1', data: { text: 'Hello' } }).data.text).toBe('world');
  });

  it('cancels the edit on Escape, reverting to the stored text without writing it', () => {
    render(<GenericAnnotationNode id="t1" type="text" data={{ text: 'Hello' }} />);
    fireEvent.doubleClick(screen.getByText('Hello'));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'discard me' } });
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' });
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.getByText('Hello')).toBeInTheDocument();
  });

  it('refuses to enter edit mode while another client holds the selection claim', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt, labels: {} }}
      >
        <GenericAnnotationNode
          id="t1"
          type="text"
          data={{ text: 'Hello', remoteSelection: { color: '#f00', displayName: 'Ada' } }}
        />
      </AnnotationContext.Provider>
    );
    fireEvent.doubleClick(screen.getByText('Hello'));
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
  });

  // smallfix-locked-annotation-text-still-editable-by-doubleclick: the persisted lock gates every
  // context menu already (see "GenericAnnotationNode locked context menu"
  // above); the double-click editor was the one path around it, for both the
  // `text` kind and a `shape`'s caption below.
  it('refuses to enter edit mode while locked', () => {
    render(<GenericAnnotationNode id="t1" type="text" data={{ text: 'Hello', locked: true }} />);
    fireEvent.doubleClick(screen.getByText('Hello'));
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('closes the editor and discards the pending edit when a lock arrives mid-edit', () => {
    const notifyChange = vi.fn();
    const { rerender } = render(
      <AnnotationContext.Provider value={{ notifyChange, labels: {} }}>
        <GenericAnnotationNode id="t1" type="text" data={{ text: 'Hello' }} />
      </AnnotationContext.Provider>
    );
    fireEvent.doubleClick(screen.getByText('Hello'));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'typed before lock' } });
    hoisted.setNodes.mockClear();
    notifyChange.mockClear();
    rerender(
      <AnnotationContext.Provider value={{ notifyChange, labels: {} }}>
        <GenericAnnotationNode id="t1" type="text" data={{ text: 'Hello', locked: true }} />
      </AnnotationContext.Provider>
    );
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.getByText('Hello')).toBeInTheDocument();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('enters edit mode on double-click for a shape annotation', () => {
    render(<GenericAnnotationNode id="s1" type="shape" data={{ shape: 'triangle' }} />);
    fireEvent.doubleClick(screen.getByTestId('shape-halo'));
    expect(screen.getByRole('textbox')).toBeInTheDocument();
  });

  it("writes a shape's caption and notifies the text-kind change", () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider value={{ notifyChange, labels: {} }}>
        <GenericAnnotationNode id="s1" type="shape" data={{ shape: 'rectangle' }} />
      </AnnotationContext.Provider>
    );
    fireEvent.doubleClick(screen.getByTestId('shape-halo'));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Step 1' } });
    expect(applyLatestUpdate({ id: 's1', data: { shape: 'rectangle' } }).data.text).toBe('Step 1');
    expect(notifyChange).toHaveBeenCalledWith('text');
  });

  // smallfix-locked-annotation-text-still-editable-by-doubleclick: the second of the two entry
  // points that shared this gap (see the `text`-kind version above).
  it("refuses to enter edit mode on a locked shape's caption", () => {
    render(
      <GenericAnnotationNode
        id="s1"
        type="shape"
        data={{ shape: 'rectangle', text: 'Step 1', locked: true }}
      />
    );
    fireEvent.doubleClick(screen.getByTestId('shape-halo'));
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('shows a previously stored caption without entering edit mode', () => {
    render(<GenericAnnotationNode type="shape" data={{ shape: 'hexagon', text: 'caption' }} />);
    expect(screen.getByText('caption')).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('renders no caption layer at all for an empty, unedited shape', () => {
    const { container } = render(<GenericAnnotationNode type="shape" data={{ shape: 'circle' }} />);
    expect(container.querySelector('.graph-generic-annotation-shape-text')).toBeNull();
  });

  it.each(['icon', 'vote_dot', 'image', 'frame'])(
    'does not open a text editor on double-click for %s (no free-text field to edit)',
    (kind) => {
      const { container } = render(
        <GenericAnnotationNode id="n1" type={kind} data={{ icon: 'flag', value: 1, image: {} }} />
      );
      const root = container.querySelector(`.kind-${kind}`);
      fireEvent.doubleClick(root);
      expect(screen.queryByRole('textbox')).toBeNull();
    }
  );

  // The whole point of this task's shape-specific problem: a clip-path clips
  // the shape's own outline, so a caption centred in the bounding box would
  // spill past the visible figure at the corners for every non-rectangular
  // variant. Each SHAPE_TEXT_INSET entry is the axis-aligned rectangle its
  // clip-path is proven to contain (see that constant's derivation comment
  // in GenericAnnotationNode.jsx) — pin that the rendered overlay actually
  // carries it, not merely that some inset exists.
  it.each([
    ['rectangle', { top: '0%', right: '0%', bottom: '0%', left: '0%' }],
    ['triangle', { top: '50%', right: '25%', bottom: '0%', left: '25%' }],
    ['rhombus', { top: '25%', right: '25%', bottom: '25%', left: '25%' }],
    ['hexagon', { top: '0%', right: '25%', bottom: '0%', left: '25%' }],
    ['process_arrow', { top: '0%', right: '30%', bottom: '0%', left: '0%' }],
  ])('insets a %s caption to the rectangle its clip-path is proven to contain', (shape, inset) => {
    const { container } = render(
      <GenericAnnotationNode type="shape" data={{ shape, text: 'x' }} />
    );
    const overlay = container.querySelector('.graph-generic-annotation-shape-text');
    expect(overlay.style.top).toBe(inset.top);
    expect(overlay.style.right).toBe(inset.right);
    expect(overlay.style.bottom).toBe(inset.bottom);
    expect(overlay.style.left).toBe(inset.left);
  });

  it("insets a circle caption to its inscribed square's margin", () => {
    const { container } = render(
      <GenericAnnotationNode type="shape" data={{ shape: 'circle', text: 'x' }} />
    );
    const overlay = container.querySelector('.graph-generic-annotation-shape-text');
    const margin = ((1 - 1 / Math.sqrt(2)) / 2) * 100;
    expect(parseFloat(overlay.style.top)).toBeCloseTo(margin, 6);
    expect(parseFloat(overlay.style.left)).toBeCloseTo(margin, 6);
  });

  it('falls back to the rectangle inset for an unrecognised shape name', () => {
    const { container } = render(
      <GenericAnnotationNode type="shape" data={{ shape: 'star', text: 'x' }} />
    );
    const overlay = container.querySelector('.graph-generic-annotation-shape-text');
    expect(overlay.style.top).toBe('0%');
    expect(overlay.style.left).toBe('0%');
  });
});

// Parse a `polygon(x% y%, ...)` clip-path into side lengths at a given box
// size, so the assertions below are about the figure a user sees rather than
// about the percentages that happen to produce it. This is the whole point of
// the aspect ratios: the same clip-path draws a regular hexagon or a squashed
// one depending only on the box it resolves against, and no class-name or
// style-string assertion can tell those apart.
function sideLengths(clipPath, w, h) {
  const pts = clipPath
    .replace(/^polygon\(/, '')
    .replace(/\)$/, '')
    .split(',')
    .map((pair) => {
      const [x, y] = pair
        .trim()
        .split(/\s+/)
        .map((v) => parseFloat(v) / 100);
      return [x * w, y * h];
    });
  return pts.map((pt, i) => {
    const next = pts[(i + 1) % pts.length];
    return Math.hypot(next[0] - pt[0], next[1] - pt[1]);
  });
}

function clipPathFor(shape) {
  const { container } = render(<GenericAnnotationNode type="shape" data={{ shape }} />);
  return container.querySelector(`.shape-${shape}`).style.clipPath;
}

describe('regular shape geometry', () => {
  // Drive the size from the shipped helper rather than restating it, so a
  // change to the created box is a change these assertions see. Restating it
  // is what let a full revert of the creation branch pass.
  const sidesAt = (shape) => {
    const { width, height } = newShapeSize(shape);
    return sideLengths(clipPathFor(shape), width, height);
  };
  const spread = (sides) => (Math.max(...sides) - Math.min(...sides)) / Math.max(...sides);

  it.each(['triangle', 'hexagon'])(
    'draws %s with equal-length sides at the size it is created with',
    (shape) => {
      // Tolerance is 0.5%, not 1%: rounding the height to a whole pixel costs
      // 0.24%, so 1% left a band of about +/-1.5% of wrong ratio passing.
      expect(spread(sidesAt(shape))).toBeLessThan(0.005);
    }
  );

  it('draws a rhombus as a square on its corner, which is the property a ratio can fix', () => {
    // A rhombus clip-path has four equal sides at EVERY ratio, so the
    // equal-sides assertion above would pass for any value here and says
    // nothing. What 1:1 buys is equal diagonals — a square standing on its
    // corner rather than a wide flat lozenge.
    const { width, height } = newShapeSize('rhombus');
    expect(spread(sidesAt('rhombus'))).toBeLessThan(0.005); // true regardless; documents why
    expect(width).toBe(height);
  });

  it('would draw them squashed in the generic box the other subtypes use', () => {
    // The witness for the whole change: the same clip-paths in the box a
    // shape used to be created in. Read from the helper, so reverting the
    // creation size to 160x96 makes the equal-sides tests fail and this one
    // explain why.
    const generic = newShapeSize('rectangle');
    expect(
      spread(sideLengths(clipPathFor('hexagon'), generic.width, generic.height))
    ).toBeGreaterThan(0.1);
  });

  it('sizes each subtype from its own ratio, and leaves the rest in the generic box', () => {
    // The helper only. That GraphCanvas actually calls it is a separate fact
    // and needs its own assertion — a helper test cannot see the call site
    // reverting to a hardcoded box, which is exactly the mutant that
    // reproduces the reported bug. Covered in
    // GraphCanvasAnnotationToolbox.test.jsx, at the toolbox.
    expect(newShapeSize('triangle')).toEqual({ width: SHAPE_BASE_WIDTH, height: 139 });
    expect(newShapeSize('hexagon')).toEqual({ width: SHAPE_BASE_WIDTH, height: 139 });
    expect(newShapeSize('rhombus')).toEqual({ width: SHAPE_BASE_WIDTH, height: 160 });
    expect(newShapeSize('rectangle')).toEqual({ width: 160, height: 96 });
    expect(newShapeSize('circle')).toEqual({ width: 160, height: 96 });
    expect(newShapeSize('process_arrow')).toEqual({ width: 160, height: 96 });
  });

  it('resizes the box when the subtype switch needs a different ratio', () => {
    // The second way to get a squashed shape, and the one keepAspectRatio
    // makes permanent: right-click a 160x96 rectangle and choose Triangle.
    // Without moving the box the triangle is drawn in a rectangle's ratio,
    // and the ratio lock then preserves exactly that.
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider value={{ notifyChange, labels: {} }}>
        <GenericAnnotationNode id="s1" type="shape" data={{ shape: 'rectangle' }} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('[data-testid="shape-halo"]'));
    fireEvent.click(screen.getByLabelText('triangle'));

    const updated = applyLatestUpdate({
      id: 's1',
      data: { shape: 'rectangle' },
      style: { width: 160, height: 96 },
    });
    expect(updated.data.shape).toBe('triangle');
    // Height re-proportioned, width kept: 160 is what the node already had.
    expect(updated.style).toEqual({ width: 160, height: 139 });
    expect(notifyChange).toHaveBeenCalledWith('geometry');
  });

  it('keeps a deliberately resized width when the subtype changes', () => {
    // Re-proportioning must not throw away a resize. A 480-wide triangle
    // becoming a hexagon stays 480 wide and only gets the height its ratio
    // needs — the two share a ratio, so nothing was squashed to begin with.
    render(
      <AnnotationContext.Provider value={{ notifyChange: vi.fn(), labels: {} }}>
        <GenericAnnotationNode id="s2" type="shape" data={{ shape: 'triangle' }} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('[data-testid="shape-halo"]'));
    fireEvent.click(screen.getByLabelText('hexagon'));

    const updated = applyLatestUpdate({
      id: 's2',
      data: { shape: 'triangle' },
      style: { width: 480, height: 417 },
    });
    expect(updated.style).toEqual({
      width: 480,
      height: Math.round(480 / regularShapeAspect('hexagon')),
    });
  });

  it('leaves the box untouched when switching to a subtype that fills its box', () => {
    // A rectangle fills whatever box it is given, so there is nothing to
    // correct — and resetting it would discard the user's resize.
    render(
      <AnnotationContext.Provider value={{ notifyChange: vi.fn(), labels: {} }}>
        <GenericAnnotationNode id="s3" type="shape" data={{ shape: 'hexagon' }} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('[data-testid="shape-halo"]'));
    fireEvent.click(screen.getByLabelText('rectangle'));

    const updated = applyLatestUpdate({
      id: 's3',
      data: { shape: 'hexagon' },
      style: { width: 480, height: 417 },
    });
    expect(updated.style).toEqual({ width: 480, height: 417 });
    expect(updated.data.shape).toBe('rectangle');
  });

  it('locks the resize ratio only for the subtypes that need one', () => {
    // A boolean, not the ratio: reactflow's NodeResizer takes no target ratio
    // and preserves whatever the node measures at drag start. Asserting the
    // number here would assert a value the library discards.
    for (const shape of ['triangle', 'rhombus', 'hexagon']) {
      render(<GenericAnnotationNode type="shape" data={{ shape }} selected />);
      expect(hoisted.resizerProps.at(-1).keepAspectRatio).toBe(true);
    }
    for (const shape of ['rectangle', 'circle', 'process_arrow']) {
      render(<GenericAnnotationNode type="shape" data={{ shape }} selected />);
      expect(hoisted.resizerProps.at(-1).keepAspectRatio).toBe(false);
    }
  });

  it('has no ratio for the subtypes meant to fill their box', () => {
    expect(regularShapeAspect('rectangle')).toBeNull();
    expect(regularShapeAspect('circle')).toBeNull();
    expect(regularShapeAspect('process_arrow')).toBeNull();
    expect(regularShapeAspect('not-a-shape')).toBeNull();
  });
});

// task-annotation-text-alignment-and-font: nine-position alignment, font
// size and a curated font-family picker for the two EDITABLE_TEXT_KINDS
// (`text`, `shape`). An existing annotation with none of these fields stored
// must keep rendering exactly as it did before this task, so the default
// cases below are pinned as carefully as the overridden ones.
describe('text/shape typography', () => {
  beforeEach(() => {
    hoisted.resizerProps.length = 0;
    hoisted.setNodes.mockClear();
    hoisted.nodes = [];
  });

  it('defaults `text` to its previous 16px size and top-left layout', () => {
    const { container } = render(<GenericAnnotationNode type="text" data={{ text: 'Hi' }} />);
    const node = container.querySelector('.kind-text');
    expect(node.style.fontSize).toBe('16px');
    expect(node.style.justifyContent).toBe('flex-start');
    expect(node.style.alignItems).toBe('flex-start');
    // No font-family override: inherits the ambient app font, unchanged.
    expect(node.style.fontFamily).toBe('');
  });

  it('defaults a `shape` caption to its previous 14px, centred layout', () => {
    const { container } = render(
      <GenericAnnotationNode type="shape" data={{ shape: 'rectangle', text: 'Caption' }} />
    );
    const wrapper = container.querySelector('.graph-generic-annotation-shape-text');
    expect(wrapper.style.justifyContent).toBe('center');
    expect(wrapper.style.alignItems).toBe('center');
    const content = container.querySelector('.graph-generic-annotation-shape-text-content');
    expect(content.style.fontSize).toBe('14px');
  });

  it('renders an overridden fontSize/font/textAlign on `text`', () => {
    const { container } = render(
      <GenericAnnotationNode
        type="text"
        data={{ text: 'Hi', fontSize: 28, font: 'serif', textAlign: 'bottom-right' }}
      />
    );
    const node = container.querySelector('.kind-text');
    expect(node.style.fontSize).toBe('28px');
    expect(node.style.fontFamily).toBe('serif');
    expect(node.style.justifyContent).toBe('flex-end');
    expect(node.style.alignItems).toBe('flex-end');
  });

  // A stored fontSize of 0 is an explicit, if degenerate, value and must be
  // honored rather than treated as "unset" and replaced by the kind default
  // (an earlier `data?.fontSize || default` did exactly that, since `||`
  // treats 0 as falsy the same way it does undefined/null).
  it('honors an explicit fontSize of 0 instead of falling back to the default', () => {
    const { container } = render(
      <GenericAnnotationNode type="text" data={{ text: 'Hi', fontSize: 0 }} />
    );
    expect(container.querySelector('.kind-text').style.fontSize).toBe('0px');
  });

  it('renders an overridden fontSize/font/textAlign on a `shape` caption', () => {
    const { container } = render(
      <GenericAnnotationNode
        type="shape"
        data={{
          shape: 'rectangle',
          text: 'Caption',
          fontSize: 20,
          font: 'monospace',
          textAlign: 'top-left',
        }}
      />
    );
    const wrapper = container.querySelector('.graph-generic-annotation-shape-text');
    expect(wrapper.style.justifyContent).toBe('flex-start');
    expect(wrapper.style.alignItems).toBe('flex-start');
    const content = container.querySelector('.graph-generic-annotation-shape-text-content');
    expect(content.style.fontSize).toBe('20px');
    expect(content.style.fontFamily).toBe('monospace');
  });

  it.each(['text', 'shape'])(
    'shows the alignment grid, font-size picker and font picker for %s',
    (kind) => {
      const { container } = render(
        <GenericAnnotationNode type={kind} data={kind === 'shape' ? { shape: 'rectangle' } : {}} />
      );
      const target =
        kind === 'shape' ? screen.getByTestId('shape-halo') : container.querySelector('.kind-text');
      fireEvent.contextMenu(target);
      expect(document.querySelectorAll('.align-picker-button')).toHaveLength(9);
      expect(document.querySelector('.context-menu-sizes')).toBeTruthy();
      // Button text is the translated family label (labels.fontFamily*), not
      // the bare stored keyword — packages/ui-graph-canvas's i18n rule.
      expect(screen.getByText('Default')).toBeInTheDocument();
      expect(screen.getByText('Serif')).toBeInTheDocument();
      expect(screen.getByText('Monospace')).toBeInTheDocument();
      expect(screen.getByText('Cursive')).toBeInTheDocument();
    }
  );

  it.each(['frame', 'icon', 'vote_dot', 'image'])('shows no typography controls for %s', (kind) => {
    const data = kind === 'icon' ? { icon: 'flag' } : kind === 'vote_dot' ? { value: 1 } : {};
    const { container } = render(<GenericAnnotationNode type={kind} data={data} />);
    const target =
      kind === 'icon'
        ? screen.getByTitle('flag')
        : kind === 'vote_dot'
          ? screen.getByText('1')
          : container.querySelector(`.kind-${kind}`);
    fireEvent.contextMenu(target);
    expect(document.querySelector('.context-menu-align')).toBeNull();
    expect(document.querySelector('.context-menu-fonts')).toBeNull();
  });

  it('sets textAlign on click and notifies the annotation context', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{
          notifyChange,
          notifyRemoteLockedAttempt: () => {},
          labels: {
            textAlign: 'Alignment',
            alignTop: 'Top',
            alignMiddle: 'Middle',
            alignBottom: 'Bottom',
            alignLeft: 'Left',
            alignCenter: 'Center',
            alignRight: 'Right',
            textSize: 'Text size',
            fontFamily: 'Font',
            fontDefault: 'Default',
          },
        }}
      >
        <GenericAnnotationNode id="t1" type="text" data={{ text: 'Hi' }} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByText('Hi'));
    fireEvent.click(screen.getByLabelText('Bottom Right'));
    expect(applyLatestUpdate({ id: 't1', data: {} }).data.textAlign).toBe('bottom-right');
    expect(notifyChange).toHaveBeenCalledWith('style');
  });

  it('sets fontSize on click', () => {
    render(<GenericAnnotationNode id="t1" type="text" data={{ text: 'Hi' }} />);
    fireEvent.contextMenu(screen.getByText('Hi'));
    // Every GENERIC_TEXT_FONT_SIZES button renders the same "A" glyph at a
    // different size — pick the first (smallest, 12) rather than an
    // ambiguous text match.
    fireEvent.click(screen.getAllByText('A')[0]);
    expect(applyLatestUpdate({ id: 't1', data: {} }).data.fontSize).toBe(12);
  });

  it('sets and clears a font-family override on click', () => {
    render(<GenericAnnotationNode id="t1" type="text" data={{ text: 'Hi', font: 'serif' }} />);
    fireEvent.contextMenu(screen.getByText('Hi'));
    // Clicked by its translated label ("Monospace"), matching what the DOM
    // actually shows; the stored value it writes is still the bare keyword.
    fireEvent.click(screen.getByText('Monospace'));
    expect(applyLatestUpdate({ id: 't1', data: { font: 'serif' } }).data.font).toBe('monospace');
    fireEvent.contextMenu(screen.getByText('Hi'));
    fireEvent.click(screen.getByText('Default'));
    expect(applyLatestUpdate({ id: 't1', data: { font: 'serif' } }).data.font).toBeNull();
  });

  it('refuses a typography change on a remote-locked annotation', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{
          notifyChange: () => {},
          notifyRemoteLockedAttempt,
          labels: {
            textAlign: 'Alignment',
            alignTop: 'Top',
            alignMiddle: 'Middle',
            alignBottom: 'Bottom',
            alignLeft: 'Left',
            alignCenter: 'Center',
            alignRight: 'Right',
            textSize: 'Text size',
            fontFamily: 'Font',
            fontDefault: 'Default',
          },
        }}
      >
        <GenericAnnotationNode
          id="t1"
          type="text"
          data={{ text: 'Hi', remoteSelection: { color: '#fff', displayName: 'Ada' } }}
        />
      </AnnotationContext.Provider>
    );
    // A remote claim refuses even opening the menu (openContextMenu itself
    // notifies and returns) — matching every other mutation on a remote-locked
    // annotation, so there is nothing to click here at all.
    fireEvent.contextMenu(screen.getByText('Hi'));
    expect(document.querySelector('.context-menu-align')).toBeNull();
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
  });
});

describe('process arrow', () => {
  it('is a full-height block with a point, not a thin arrow', () => {
    const clip = clipPathFor('process_arrow');
    // The body spans the full height: the leading edge runs 0% to 100%,
    // where the old arrow glyph ran 25% to 75% and left the rest empty.
    expect(clip).toBe('polygon(0% 0%, 70% 0%, 100% 50%, 70% 100%, 0% 100%)');
    const sides = sideLengths(clip, 200, 100);
    // Five sides: back edge, top, two point edges, bottom. An arrow glyph has
    // seven. The count alone distinguishes the two shapes.
    expect(sides).toHaveLength(5);
  });
});
