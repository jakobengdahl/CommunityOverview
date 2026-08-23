import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import GenericAnnotationNode from '../src/components/GenericAnnotationNode';
import { AnnotationContext } from '../src/components/AnnotationContext';

const hoisted = vi.hoisted(() => ({ resizerProps: [] }));

vi.mock('reactflow', () => ({
  NodeResizer: (props) => {
    hoisted.resizerProps.push(props);
    return <div data-testid="resizer" />;
  },
}));

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
    // ("xcircle_fill") without the second camel-boundary rule, and silently
    // fall back to the default glyph.
    ['XCircleFill', '✖'],
    ['x-circle-fill', '✖'],
  ])('accepts the host icon registry spelling %s', (icon, glyph) => {
    render(<GenericAnnotationNode type="icon" data={{ icon }} />);
    expect(screen.getByTitle(icon).textContent).toBe(glyph);
  });

  // The set covers 11 of the host registry's 75 icon names. The other 64 must
  // stay as distinguishable as they were before the set existed - collapsing
  // DatabaseFill/GearFill/PeopleFill into three identical dots would make the
  // rendering worse, not better, so an unmapped name keeps its abbreviation.
  it.each([
    ['DatabaseFill', 'Da'],
    ['GearFill', 'Ge'],
    ['PeopleFill', 'Pe'],
    ['no-such-icon', 'no'],
  ])('abbreviates the unmapped icon name %s instead of drawing a generic dot', (icon, text) => {
    render(<GenericAnnotationNode type="icon" data={{ icon }} />);
    const badge = screen.getByTitle(icon);
    expect(badge.textContent).toBe(text);
    expect(badge.className).toContain('kind-icon-abbreviated');
  });

  // Not a claim that unmapped names are all distinct - abbreviating the host
  // registry's 64 unmapped names yields only 36 distinct marks (FileEarmark*
  // all draw "Fi", List* all draw "Li"). The property is narrower: three names
  // a single default glyph would have merged stay apart.
  it('keeps unmapped names apart that one default glyph would have merged', () => {
    const names = ['DatabaseFill', 'GearFill', 'PeopleFill'];
    const drawn = names.map((icon) => {
      render(<GenericAnnotationNode type="icon" data={{ icon }} />);
      return screen.getByTitle(icon).textContent;
    });
    expect(new Set(drawn).size).toBe(names.length);
  });

  it('abbreviates two names sharing their first two characters to the same mark', () => {
    for (const icon of ['Diagram2Fill', 'Diagram3Fill']) {
      render(<GenericAnnotationNode type="icon" data={{ icon }} />);
      expect(screen.getByTitle(icon).textContent).toBe('Di');
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
  it('draws a rotation on a frame', () => {
    const { container } = render(<GenericAnnotationNode type="frame" data={{ rotation: 45 }} />);
    expect(container.querySelector('.kind-frame').style.transform).toBe('rotate(45deg)');
  });

  it('rotates an image annotation', () => {
    render(
      <GenericAnnotationNode
        type="image"
        data={{ image: { url: 'https://example.com/a.png' }, alt: 'diagram', rotation: 90 }}
      />
    );
    expect(screen.getByAltText('diagram').style.transform).toBe('rotate(90deg)');
  });

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
});
