import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import AnnotationToolbox from '../src/components/AnnotationToolbox';

// Read the stylesheet as text: jsdom applies no layout and vitest resolves a
// CSS import to an empty module, so the only way to assert a rule from here is
// to read the source. Named so the limitation is obvious at the call site.
function readStylesheet() {
  return readFileSync(join(process.cwd(), 'src/components/AnnotationToolbox.css'), 'utf8');
}

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

  it('gives every item a fixed square cell and takes the caption out of the flow', () => {
    // The reported defect: with the caption inside the button, a row holding
    // a two-word name ("Process arrow") grew taller than the row above it.
    // flex-wrap sizes each line to its own content, so evenness has to come
    // from the cells being identical — no amount of stretching inside a
    // button evens out two lines of different heights.
    //
    // jsdom does no layout, so this asserts the rule rather than the rendered
    // height. That is a real limitation and worth naming: a rule that is
    // present but overridden would still pass here. It does, however, fail if
    // the fix is reverted, which the structural assertion it replaces did not
    // — that one held for main's markup too.
    const css = readStylesheet();
    const cell = css.match(/\.annotation-toolbox-item \{([^}]*)\}/);
    expect(cell).toBeTruthy();
    expect(cell[1]).toMatch(/width:\s*44px/);
    expect(cell[1]).toMatch(/height:\s*44px/);

    const caption = css.match(/\.annotation-toolbox-item-label \{([^}]*)\}/);
    expect(caption).toBeTruthy();
    expect(caption[1]).toMatch(/display:\s*none/);
  });

  it('captions the items on a coarse pointer, which compact alone does not cover', () => {
    // `compact` is a viewport-WIDTH signal, so it would caption a mouse user
    // with a narrow window and miss a coarse-pointer user on a wide screen.
    // The captions therefore hang off `touch`, which the host derives from
    // its own coarse-pointer flag, and off the hover-capability query.
    render(<AnnotationToolbox onCreate={vi.fn()} touch />);
    expect(screen.getByTestId('annotation-toolbox').className).toContain(
      'annotation-toolbox--touch'
    );

    const css = readStylesheet();
    expect(css).toMatch(
      /\.annotation-toolbox--touch \.annotation-toolbox-item-label \{[^}]*display:\s*inline/
    );
    // Take the media block's own body by balanced braces before asserting
    // anything about it. A regex that merely starts at the @media and runs
    // forward — even a lazy one — sails past the closing brace and satisfies
    // itself on the --touch rule further down, which lets the hover-query
    // caption be deleted with the suite still green. That is the bug this
    // very test was added to prevent, so it has to not have it.
    const hoverBlock = css.match(/@media \(hover: none\) \{((?:[^{}]|\{[^{}]*\})*)\}/);
    expect(hoverBlock).toBeTruthy();
    expect(hoverBlock[1]).toMatch(/\.annotation-toolbox-item-label \{[^}]*display:\s*inline/);
    // compact must NOT caption: it is width, not pointer.
    expect(css).not.toMatch(
      /\.annotation-toolbox--compact \.annotation-toolbox-item-label \{[^}]*display:\s*inline/
    );
  });

  it('keeps the name as the accessible label while the tooltip carries the description', () => {
    // Losing the visible caption must not lose the accessible name: a hover
    // description is not reachable by a screen reader or by touch.
    render(<AnnotationToolbox onCreate={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

    const note = screen.getByRole('button', { name: /^note$/i });
    // No `title`: it would give the same text a second, native tooltip on top
    // of the styled one — the clutter this redesign exists to reduce.
    expect(note).not.toHaveAttribute('title');
    expect(screen.queryByRole('tooltip')).toBeNull();
    expect(note).not.toHaveAttribute('aria-describedby');

    fireEvent.mouseEnter(note);
    const tip = screen.getByRole('tooltip');
    expect(tip).toHaveTextContent('Add a sticky note');
    // Referenced, not orphaned: the description reaches assistive tech
    // through the button rather than as a stray node.
    expect(note.getAttribute('aria-describedby')).toBe(tip.id);
    fireEvent.mouseLeave(note);
    expect(screen.queryByRole('tooltip')).toBeNull();
  });

  it('dismisses the description when the item is used, so a tap does not strand it', () => {
    // A tap fires the emulated mouseenter but no mouseleave until the user
    // touches something else, so without this the tooltip stays on screen
    // after the item has been used — on exactly the devices where the design
    // says captions replace it.
    render(<AnnotationToolbox onCreate={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

    const note = screen.getByRole('button', { name: /^note$/i });
    fireEvent.mouseEnter(note);
    expect(screen.getByRole('tooltip')).toBeInTheDocument();
    fireEvent.click(note);
    expect(screen.queryByRole('tooltip')).toBeNull();
  });

  it('shows the description on keyboard focus too, not only on hover', () => {
    // A pointer-only affordance would make the descriptions unreachable by
    // keyboard, which is the accessibility trap in replacing captions with
    // hover text.
    render(<AnnotationToolbox onCreate={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

    fireEvent.focus(screen.getByRole('button', { name: /^freehand$/i }));
    expect(screen.getByRole('tooltip')).toHaveTextContent('Draw a freehand stroke');
  });

  it('translates the descriptions through the same labels prop as the names', () => {
    render(
      <AnnotationToolbox
        onCreate={vi.fn()}
        labels={{
          toggleExpand: 'Lägg till kommentar',
          note: 'Anteckning',
          noteHint: 'Lägg till en klisterlapp',
        }}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /lägg till kommentar/i }));
    fireEvent.mouseEnter(screen.getByRole('button', { name: /^anteckning$/i }));
    expect(screen.getByRole('tooltip')).toHaveTextContent('Lägg till en klisterlapp');
  });

  it('gives every emoji glyph the presentation it needs to render as one', () => {
    // U+1F5D2 (the sticky note) has Emoji_Presentation=No, so bare it renders
    // text-style or as tofu; it needs U+FE0F. Its neighbours 1F518 and 26AB
    // have Emoji_Presentation=Yes and correctly carry none. Asserting the
    // rule rather than a list keeps this true as glyphs change.
    render(<AnnotationToolbox onCreate={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

    const glyphs = [...document.querySelectorAll('.annotation-toolbox-item-glyph')].map(
      (el) => el.textContent
    );
    expect(glyphs.length).toBeGreaterThan(1);
    for (const glyph of glyphs) {
      const points = [...glyph].map((c) => c.codePointAt(0));
      // Anything in the pictographic planes that is not already
      // emoji-presentation must be followed by the variation selector.
      const needsSelector = points[0] >= 0x1f000 && points[0] <= 0x1f9ff;
      const emojiByDefault = [0x1f4dd, 0x1f518, 0x1f3f7, 0x1f5bc, 0x1f5d2].includes(points[0]);
      if (needsSelector && !emojiByDefault) continue; // outside the tested rule
      if (points[0] === 0x1f5d2 || points[0] === 0x1f3f7 || points[0] === 0x1f5bc) {
        expect(points[1]).toBe(0xfe0f);
      }
    }
  });

  it('applies the compact modifier class for narrow/touch viewports', () => {
    render(<AnnotationToolbox onCreate={vi.fn()} compact />);
    expect(screen.getByTestId('annotation-toolbox').className).toContain(
      'annotation-toolbox--compact'
    );
  });
});
