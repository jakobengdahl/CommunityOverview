import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import AnnotationToolbox from '../src/components/AnnotationToolbox';

describe('AnnotationToolbox', () => {
  it('renders collapsed by default, showing only the toggle', () => {
    render(<AnnotationToolbox onCreate={vi.fn()} />);
    expect(screen.getByTestId('annotation-toolbox')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /add annotation/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^note$/i })).not.toBeInTheDocument();
  });

  it('expands to show every wired annotation kind on toggle click', () => {
    render(<AnnotationToolbox onCreate={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

    expect(screen.getByRole('button', { name: /^note$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^text$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^label$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^frame$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^rectangle$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^circle$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^triangle$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^rhombus$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^hexagon$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^process arrow$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^icon$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^vote dot$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^image$/i })).toBeInTheDocument();
  });

  it('collapses again on a second toggle click', () => {
    render(<AnnotationToolbox onCreate={vi.fn()} />);
    const toggle = screen.getByRole('button', { name: /add annotation/i });
    fireEvent.click(toggle);
    expect(screen.getByRole('button', { name: /^note$/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /collapse annotation toolbox/i }));
    expect(screen.queryByRole('button', { name: /^note$/i })).not.toBeInTheDocument();
  });

  it('calls onCreate with the kind for note/text/label/frame', () => {
    const onCreate = vi.fn();
    render(<AnnotationToolbox onCreate={onCreate} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

    fireEvent.click(screen.getByRole('button', { name: /^note$/i }));
    expect(onCreate).toHaveBeenLastCalledWith('note', undefined);

    fireEvent.click(screen.getByRole('button', { name: /^text$/i }));
    expect(onCreate).toHaveBeenLastCalledWith('text', undefined);

    fireEvent.click(screen.getByRole('button', { name: /^label$/i }));
    expect(onCreate).toHaveBeenLastCalledWith('label', undefined);

    fireEvent.click(screen.getByRole('button', { name: /^frame$/i }));
    expect(onCreate).toHaveBeenLastCalledWith('frame', undefined);
  });

  // Every accepted content.shape variant is offered, not only the two that
  // used to render distinctly (docs/ANNOTATION_CONTRACT.md, `shape` row).
  it.each([
    [/^rectangle$/i, 'rectangle'],
    [/^circle$/i, 'circle'],
    [/^triangle$/i, 'triangle'],
    [/^rhombus$/i, 'rhombus'],
    [/^hexagon$/i, 'hexagon'],
    [/^process arrow$/i, 'process_arrow'],
  ])('calls onCreate with the shape kind and the %s variant', (name, shape) => {
    const onCreate = vi.fn();
    render(<AnnotationToolbox onCreate={onCreate} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

    fireEvent.click(screen.getByRole('button', { name }));
    expect(onCreate).toHaveBeenLastCalledWith('shape', { shape });
  });

  it('calls onCreate with the image kind and no options, like the other simple kinds', () => {
    const onCreate = vi.fn();
    render(<AnnotationToolbox onCreate={onCreate} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

    fireEvent.click(screen.getByRole('button', { name: /^image$/i }));
    expect(onCreate).toHaveBeenLastCalledWith('image', undefined);
  });

  it('calls onCreate with the icon kind and no options', () => {
    const onCreate = vi.fn();
    render(<AnnotationToolbox onCreate={onCreate} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

    fireEvent.click(screen.getByRole('button', { name: /^icon$/i }));
    expect(onCreate).toHaveBeenLastCalledWith('icon', undefined);
  });

  it('calls onCreate with the vote_dot kind and no options', () => {
    const onCreate = vi.fn();
    render(<AnnotationToolbox onCreate={onCreate} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

    fireEvent.click(screen.getByRole('button', { name: /^vote dot$/i }));
    expect(onCreate).toHaveBeenLastCalledWith('vote_dot', undefined);
  });

  it('accepts a labels override so the host app can localize it', () => {
    render(
      <AnnotationToolbox
        onCreate={vi.fn()}
        labels={{ toggleExpand: 'Lägg till kommentar', note: 'Anteckning' }}
      />
    );
    expect(screen.getByRole('button', { name: /lägg till kommentar/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /lägg till kommentar/i }));
    expect(screen.getByRole('button', { name: /^anteckning$/i })).toBeInTheDocument();
  });

  it('applies the compact modifier class for narrow/touch viewports', () => {
    render(<AnnotationToolbox onCreate={vi.fn()} compact />);
    expect(screen.getByTestId('annotation-toolbox').className).toContain(
      'annotation-toolbox--compact'
    );
  });
});
