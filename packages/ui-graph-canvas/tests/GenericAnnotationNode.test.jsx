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

  it('renders an icon badge showing the icon name as a title', () => {
    render(<GenericAnnotationNode type="icon" data={{ icon: 'flag' }} />);
    expect(screen.getByTitle('flag')).toBeInTheDocument();
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
