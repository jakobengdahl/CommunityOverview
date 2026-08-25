import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import GenericAnnotationNode from '../src/components/GenericAnnotationNode';
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

  // docs/ANNOTATION_CONTRACT.md: "semantic default layers plus manual
  // forward/back". The arithmetic itself is covered by
  // annotationLayers.test.js; these pin the wiring into the menu.
  describe('layer controls', () => {
    it('writes the stepped layer onto the annotation as zIndex', () => {
      hoisted.nodes = [
        { id: 'v1', type: 'vote_dot', zIndex: 0 },
        { id: 'other', type: 'note', zIndex: 1 },
      ];
      render(<GenericAnnotationNode id="v1" type="vote_dot" data={{ value: 1 }} />);
      fireEvent.contextMenu(screen.getByText('1'));
      fireEvent.click(screen.getByLabelText('Bring forward'));
      const updated = hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes);
      expect(updated[0].zIndex).toBe(2);
      expect(updated[1].zIndex).toBe(1);
    });

    it('leaves the canvas untouched when the annotation is already at the front', () => {
      hoisted.nodes = [
        { id: 'v1', type: 'vote_dot', zIndex: 5 },
        { id: 'other', type: 'note', zIndex: 1 },
      ];
      const notifyChange = vi.fn();
      render(
        <AnnotationContext.Provider
          value={{ notifyChange, labels: { layer: 'Layer', layerForward: 'Bring forward' } }}
        >
          <GenericAnnotationNode id="v1" type="vote_dot" data={{ value: 1 }} />
        </AnnotationContext.Provider>
      );
      fireEvent.contextMenu(screen.getByText('1'));
      fireEvent.click(screen.getByLabelText('Bring forward'));
      expect(hoisted.setNodes).not.toHaveBeenCalled();
      expect(notifyChange).not.toHaveBeenCalled();
    });

    it('offers no layer controls on a locked annotation', () => {
      render(<GenericAnnotationNode id="v1" type="vote_dot" data={{ value: 1, locked: true }} />);
      fireEvent.contextMenu(screen.getByText('1'));
      expect(screen.queryByLabelText('Bring forward')).toBeNull();
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
    'shows only an unlock action for a locked %s, hiding rotation/shape/delete',
    (kind) => {
      const data = { locked: true, text: 'x', icon: 'flag', image: {}, value: 1 };
      const { container } = render(
        <AnnotationContext.Provider value={{ notifyChange: vi.fn(), labels: { unlock: 'Unlock' } }}>
          <GenericAnnotationNode id="n1" type={kind} data={data} selected />
        </AnnotationContext.Provider>
      );
      const root =
        kind === 'shape'
          ? screen.getByTestId('shape-halo')
          : container.querySelector(`.kind-${kind}`);
      fireEvent.contextMenu(root);
      expect(screen.getByText(/Unlock/)).toBeInTheDocument();
      expect(screen.queryByLabelText('Rotate left 15°')).toBeNull();
      expect(screen.queryByLabelText('circle')).toBeNull();
      expect(screen.queryByText(/Delete/)).toBeNull();
    }
  );

  it('unlocks a locked annotation and notifies the annotation context', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider value={{ notifyChange, labels: { unlock: 'Unlock' } }}>
        <GenericAnnotationNode id="f1" type="frame" data={{ locked: true }} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(document.querySelector('.kind-frame'));
    fireEvent.click(screen.getByText(/Unlock/));
    const updated = applyLatestUpdate({ id: 'f1', data: { locked: true } });
    expect(updated.data.locked).toBe(false);
    expect(notifyChange).toHaveBeenCalledWith('style');
  });

  it('still shows the full property editor when unlocked', () => {
    render(<GenericAnnotationNode id="f1" type="frame" data={{ locked: false }} selected />);
    fireEvent.contextMenu(document.querySelector('.kind-frame'));
    expect(screen.getByLabelText('Rotate left 15°')).toBeInTheDocument();
    expect(screen.getByText(/Delete/)).toBeInTheDocument();
  });

  // Resize/drag already refuse a locked annotation; the resizer must stay
  // hidden even if the (restricted) context menu is open at the same time.
  it('keeps resize handles hidden for a locked, selected frame', () => {
    render(<GenericAnnotationNode type="frame" data={{ locked: true }} selected />);
    expect(hoisted.resizerProps.at(-1).isVisible).toBe(false);
  });
});
