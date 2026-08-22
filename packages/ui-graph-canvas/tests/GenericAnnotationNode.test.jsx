import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import GenericAnnotationNode from '../src/components/GenericAnnotationNode';

// A simple, generic visual representation for the v1 annotation types that
// have no dedicated interactive canvas UX yet (text, frame, shape, icon,
// vote_dot, image) — see docs/ANNOTATION_CONTRACT.md. These render read-only
// so annotations created via MCP/session state are visible on the canvas
// instead of being store/MCP-only. ReactFlow supplies the node's registered
// type (e.g. "text") as the `type` prop, matching how GraphCanvas's
// nodeTypes map wires this component up for each of the six kinds.
describe('GenericAnnotationNode', () => {
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
});
