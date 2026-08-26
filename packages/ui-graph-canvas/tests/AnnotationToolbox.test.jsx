import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
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

    // And the element the rule reveals actually exists, once per item. Every
    // other assertion about the caption reads the stylesheet, so without this
    // the touch and keyboard fallback that justifies dropping the visible
    // names could be deleted from the markup with the suite still green.
    render(<AnnotationToolbox onCreate={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
    const items = document.querySelectorAll('.annotation-toolbox-item');
    const captions = document.querySelectorAll('.annotation-toolbox-item-label');
    expect(items.length).toBeGreaterThan(1);
    expect(captions.length).toBe(items.length);
    expect([...captions].every((c) => c.textContent.trim())).toBe(true);
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

  it('gives every emoji glyph that needs one its variation selector', () => {
    // A pictographic code point with Emoji_Presentation=No renders text-style
    // or as tofu unless U+FE0F follows it. U+1F5D2 (sticky note) and U+270F
    // (pencil) are both in that class; U+1F518 and U+26AB are
    // Emoji_Presentation=Yes and correctly carry none.
    //
    // This is a checked LIST, not a derived rule — the property is a Unicode
    // table this package does not carry, so a glyph added later is not
    // covered until it is added here. Naming that plainly rather than dressing
    // the list up as a rule, because the first version of this test claimed
    // the rule and then skipped U+270F, which is in the toolbox already.
    const NEEDS_SELECTOR = new Set([0x1f5d2, 0x1f3f7, 0x1f5bc, 0x270f]);

    render(<AnnotationToolbox onCreate={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

    const glyphs = [...document.querySelectorAll('.annotation-toolbox-item-glyph')].map(
      (el) => el.textContent
    );
    expect(glyphs.length).toBeGreaterThan(1);

    const checked = new Set();
    for (const glyph of glyphs) {
      const [first, second] = [...glyph].map((c) => c.codePointAt(0));
      if (!NEEDS_SELECTOR.has(first)) continue;
      checked.add(first);
      expect(second).toBe(0xfe0f);
    }
    // Guard the other direction: if one of these glyphs leaves the toolbox the
    // test would otherwise pass for no reason. Distinct code points, so a
    // second item legitimately reusing one does not read as drift.
    expect(checked.size).toBe(NEEDS_SELECTOR.size);
  });

  it('applies the compact modifier class for narrow/touch viewports', () => {
    render(<AnnotationToolbox onCreate={vi.fn()} compact />);
    expect(screen.getByTestId('annotation-toolbox').className).toContain(
      'annotation-toolbox--compact'
    );
  });

  describe('drag-to-create', () => {
    // jsdom has no PointerEvent constructor — build one from MouseEvent, the
    // same pattern GraphCanvasFreehandDrawing.test.jsx and
    // GraphCanvasTouch.test.jsx use for the identical limitation.
    function pointerEvent(type, { pointerId = 1, clientX = 0, clientY = 0 } = {}) {
      const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX, clientY });
      Object.defineProperty(event, 'pointerId', { value: pointerId });
      return event;
    }

    // The pointer handlers are attached via plain addEventListener (see
    // AnnotationToolbox's handlePointerDown), not React's synthetic event
    // system, so a dispatch outside act() leaves the resulting state update
    // unflushed when the very next line asserts on it.
    function dispatch(target, event) {
      act(() => {
        target.dispatchEvent(event);
      });
    }

    it('is HTML5-draggable by default (fine pointer) for every kind that creates an object', () => {
      render(<AnnotationToolbox onCreate={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      expect(screen.getByRole('button', { name: /^note$/i })).toHaveAttribute('draggable', 'true');
      expect(screen.getByRole('button', { name: /^rectangle$/i })).toHaveAttribute(
        'draggable',
        'true'
      );
    });

    it('never makes image or freehand draggable, on a fine pointer or a coarse one', () => {
      for (const touch of [false, true]) {
        const { unmount } = render(<AnnotationToolbox onCreate={vi.fn()} touch={touch} />);
        fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

        expect(screen.getByRole('button', { name: /^image$/i })).toHaveAttribute(
          'draggable',
          'false'
        );
        expect(screen.getByRole('button', { name: /^freehand$/i })).toHaveAttribute(
          'draggable',
          'false'
        );
        unmount();
      }
    });

    it('sets the annotation-kind dataTransfer payload on dragstart for a plain kind', () => {
      render(<AnnotationToolbox onCreate={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const setData = vi.fn();
      fireEvent.dragStart(screen.getByRole('button', { name: /^note$/i }), {
        dataTransfer: { setData, effectAllowed: '' },
      });
      expect(setData).toHaveBeenCalledWith('application/annotation-kind', JSON.stringify({ kind: 'note' }));
    });

    it('includes the shape option in the dataTransfer payload for a shape variant', () => {
      render(<AnnotationToolbox onCreate={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const setData = vi.fn();
      fireEvent.dragStart(screen.getByRole('button', { name: /^hexagon$/i }), {
        dataTransfer: { setData, effectAllowed: '' },
      });
      expect(setData).toHaveBeenCalledWith(
        'application/annotation-kind',
        JSON.stringify({ kind: 'shape', shape: 'hexagon' })
      );
    });

    it('never fires a dataTransfer payload for image or freehand (no dragstart handler at all)', () => {
      render(<AnnotationToolbox onCreate={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const setData = vi.fn();
      fireEvent.dragStart(screen.getByRole('button', { name: /^image$/i }), {
        dataTransfer: { setData, effectAllowed: '' },
      });
      fireEvent.dragStart(screen.getByRole('button', { name: /^freehand$/i }), {
        dataTransfer: { setData, effectAllowed: '' },
      });
      expect(setData).not.toHaveBeenCalled();
    });

    it('turns off native draggable on a coarse pointer, for every kind', () => {
      render(<AnnotationToolbox onCreate={vi.fn()} touch />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
      expect(screen.getByRole('button', { name: /^note$/i })).toHaveAttribute('draggable', 'false');
    });

    it('creates via onDragCreate at the release point once a coarse-pointer drag clears the threshold', () => {
      const onCreate = vi.fn();
      const onDragCreate = vi.fn();
      render(<AnnotationToolbox onCreate={onCreate} onDragCreate={onDragCreate} touch />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const note = screen.getByRole('button', { name: /^note$/i });
      dispatch(note, pointerEvent('pointerdown', { clientX: 100, clientY: 100 }));
      // Under the threshold: no ghost, no creation yet.
      dispatch(window, pointerEvent('pointermove', { clientX: 102, clientY: 100 }));
      expect(document.querySelector('.annotation-toolbox-drag-ghost')).toBeNull();

      // Clears the threshold: the ghost appears and follows the pointer.
      dispatch(window, pointerEvent('pointermove', { clientX: 140, clientY: 220 }));
      const ghost = document.querySelector('.annotation-toolbox-drag-ghost');
      expect(ghost).not.toBeNull();
      expect(ghost.style.left).toBe('140px');
      expect(ghost.style.top).toBe('220px');

      dispatch(window, pointerEvent('pointerup', { clientX: 150, clientY: 230 }));
      expect(onDragCreate).toHaveBeenCalledWith('note', undefined, { x: 150, y: 230 });
      expect(onCreate).not.toHaveBeenCalled();
      expect(document.querySelector('.annotation-toolbox-drag-ghost')).toBeNull();
    });

    it('passes the shape option through onDragCreate for a shape variant', () => {
      const onDragCreate = vi.fn();
      render(<AnnotationToolbox onCreate={vi.fn()} onDragCreate={onDragCreate} touch />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const circle = screen.getByRole('button', { name: /^circle$/i });
      dispatch(circle, pointerEvent('pointerdown', { clientX: 0, clientY: 0 }));
      dispatch(window, pointerEvent('pointermove', { clientX: 50, clientY: 50 }));
      dispatch(window, pointerEvent('pointerup', { clientX: 60, clientY: 60 }));

      expect(onDragCreate).toHaveBeenCalledWith('shape', { shape: 'circle' }, { x: 60, y: 60 });
    });

    it('treats a press-and-release under the threshold as a plain click, not a drag', () => {
      const onCreate = vi.fn();
      const onDragCreate = vi.fn();
      render(<AnnotationToolbox onCreate={onCreate} onDragCreate={onDragCreate} touch />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const note = screen.getByRole('button', { name: /^note$/i });
      dispatch(note, pointerEvent('pointerdown', { clientX: 10, clientY: 10 }));
      dispatch(window, pointerEvent('pointerup', { clientX: 11, clientY: 10 }));
      expect(onDragCreate).not.toHaveBeenCalled();

      // The tap's own click still creates normally — drag is additive.
      fireEvent.click(note);
      expect(onCreate).toHaveBeenCalledWith('note', undefined);
    });

    it('suppresses the click that follows a completed drag, without blocking the next real tap', () => {
      const onCreate = vi.fn();
      const onDragCreate = vi.fn();
      render(<AnnotationToolbox onCreate={onCreate} onDragCreate={onDragCreate} touch />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const note = screen.getByRole('button', { name: /^note$/i });
      dispatch(note, pointerEvent('pointerdown', { clientX: 0, clientY: 0 }));
      dispatch(window, pointerEvent('pointermove', { clientX: 50, clientY: 50 }));
      dispatch(window, pointerEvent('pointerup', { clientX: 50, clientY: 50 }));
      expect(onDragCreate).toHaveBeenCalledTimes(1);

      // The synthetic click a touch release can produce right after pointerup.
      fireEvent.click(note);
      expect(onCreate).not.toHaveBeenCalled();

      // A later, unrelated tap on the same item still works.
      fireEvent.click(note);
      expect(onCreate).toHaveBeenCalledWith('note', undefined);
    });

    it('never starts a pointer drag for image or freehand even on a coarse pointer', () => {
      const onCreate = vi.fn();
      const onDragCreate = vi.fn();
      render(<AnnotationToolbox onCreate={onCreate} onDragCreate={onDragCreate} touch />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const image = screen.getByRole('button', { name: /^image$/i });
      dispatch(image, pointerEvent('pointerdown', { clientX: 0, clientY: 0 }));
      dispatch(window, pointerEvent('pointermove', { clientX: 100, clientY: 100 }));
      dispatch(window, pointerEvent('pointerup', { clientX: 100, clientY: 100 }));

      expect(onDragCreate).not.toHaveBeenCalled();
      expect(document.querySelector('.annotation-toolbox-drag-ghost')).toBeNull();

      // Click-only behaviour is unaffected.
      fireEvent.click(image);
      expect(onCreate).toHaveBeenCalledWith('image', undefined);
    });

    it('drops the in-progress drag with no creation on pointercancel', () => {
      const onDragCreate = vi.fn();
      render(<AnnotationToolbox onCreate={vi.fn()} onDragCreate={onDragCreate} touch />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const note = screen.getByRole('button', { name: /^note$/i });
      dispatch(note, pointerEvent('pointerdown', { clientX: 0, clientY: 0 }));
      dispatch(window, pointerEvent('pointermove', { clientX: 100, clientY: 100 }));
      dispatch(window, pointerEvent('pointercancel', { clientX: 100, clientY: 100 }));

      expect(onDragCreate).not.toHaveBeenCalled();
      expect(document.querySelector('.annotation-toolbox-drag-ghost')).toBeNull();
    });
  });
});
