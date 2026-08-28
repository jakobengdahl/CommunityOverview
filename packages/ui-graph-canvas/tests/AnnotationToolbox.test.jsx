import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import AnnotationToolbox from '../src/components/AnnotationToolbox';

// Read the stylesheet as text: jsdom applies no layout and vitest resolves a
// CSS import to an empty module, so the only way to assert a rule from here is
// to read the source. Named so the limitation is obvious at the call site.
function readStylesheet() {
  return readFileSync(join(process.cwd(), 'src/components/AnnotationToolbox.css'), 'utf8');
}

// Opens the shape slot's picker and selects `name` (a role name regex) from
// it, scoped to the picker panel (`within`) so an ambiguous match against the
// slot's own current-shape button — e.g. selecting "Rectangle" while the slot
// is already showing "Rectangle" — can never occur.
function selectShapeVariant(name) {
  fireEvent.click(screen.getByRole('button', { name: /choose a shape/i }));
  const picker = screen.getByRole('group', { name: /^shapes$/i });
  fireEvent.click(within(picker).getByRole('button', { name }));
}

describe('AnnotationToolbox', () => {
  beforeEach(() => {
    localStorage.clear();
  });


  it('renders collapsed by default, showing only the toggle', () => {
    render(<AnnotationToolbox onCreate={vi.fn()} />);
    expect(screen.getByTestId('annotation-toolbox')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /add annotation/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^note$/i })).not.toBeInTheDocument();
  });

  it('expands to show every wired annotation kind on toggle click, with the shape slot standing in for every shape variant', () => {
    render(<AnnotationToolbox onCreate={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

    expect(screen.getByRole('button', { name: /^note$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^text$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^label$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^frame$/i })).toBeInTheDocument();
    // The shape slot shows the default shape (rectangle) as its own name;
    // every other variant now lives in the picker, not as a top-level button
    // (task-annotation-shapes-under-one-toolbox-slot).
    expect(screen.getByRole('button', { name: /^rectangle$/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^circle$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^triangle$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^rhombus$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^hexagon$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^process arrow$/i })).not.toBeInTheDocument();
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

  // Every accepted content.shape variant is still offered — now via the
  // shape slot's picker rather than its own top-level button
  // (docs/ANNOTATION_CONTRACT.md, `shape` row).
  it.each([
    [/^rectangle$/i, 'rectangle'],
    [/^circle$/i, 'circle'],
    [/^triangle$/i, 'triangle'],
    [/^rhombus$/i, 'rhombus'],
    [/^hexagon$/i, 'hexagon'],
    [/^process arrow$/i, 'process_arrow'],
  ])(
    'calls onCreate with the shape kind and the %s variant once selected in the picker',
    (name, shape) => {
      const onCreate = vi.fn();
      render(<AnnotationToolbox onCreate={onCreate} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      selectShapeVariant(name);
      expect(screen.queryByRole('group', { name: /^shapes$/i })).not.toBeInTheDocument();

      // Selecting made the slot itself this shape; clicking it creates it.
      fireEvent.click(screen.getByRole('button', { name }));
      expect(onCreate).toHaveBeenLastCalledWith('shape', { shape });
    }
  );

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
      expect(setData).toHaveBeenCalledWith(
        'application/annotation-kind',
        JSON.stringify({ kind: 'note' })
      );
    });

    it("includes the shape option in the dataTransfer payload for the slot's current shape variant", () => {
      render(<AnnotationToolbox onCreate={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
      selectShapeVariant(/^hexagon$/i);

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

    it("passes the shape option through onDragCreate for the slot's current shape variant", () => {
      const onDragCreate = vi.fn();
      render(<AnnotationToolbox onCreate={vi.fn()} onDragCreate={onDragCreate} touch />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
      selectShapeVariant(/^circle$/i);

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

    it('does not let a second pointer’s own gesture on a different item disturb the first pointer’s pending suppression', () => {
      // Regression test: suppression is keyed per-item (suppressClickRef is a
      // Set of item keys), not a single shared flag. A second pointer
      // starting and completing its own gesture on a DIFFERENT item while a
      // first pointer's completed drag is still in its own pending-suppress
      // window (waiting for its synthetic click, or the fallback timeout)
      // must neither erase the first item's suppression nor be swallowed by
      // it — each item's suppression is independent.
      const onCreate = vi.fn();
      const onDragCreate = vi.fn();
      render(<AnnotationToolbox onCreate={onCreate} onDragCreate={onDragCreate} touch />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const note = screen.getByRole('button', { name: /^note$/i });
      // `label` stands in for the old `circle` shape button as "a different
      // item" here — any two distinct toolbox buttons prove the same
      // per-item independence; the shape slot collapsing six of them into
      // one is orthogonal to what this regression test is about.
      const label = screen.getByRole('button', { name: /^label$/i });

      // Pointer 1 completes a drag on `note` — opens note's suppression window.
      dispatch(note, pointerEvent('pointerdown', { pointerId: 1, clientX: 0, clientY: 0 }));
      dispatch(window, pointerEvent('pointermove', { pointerId: 1, clientX: 50, clientY: 50 }));
      dispatch(window, pointerEvent('pointerup', { pointerId: 1, clientX: 50, clientY: 50 }));
      expect(onDragCreate).toHaveBeenCalledTimes(1);

      // Before note's own synthetic click arrives, pointer 2 presses and
      // releases `label` as a plain tap (under the drag threshold) — a
      // genuine, unrelated click on a different button.
      dispatch(label, pointerEvent('pointerdown', { pointerId: 2, clientX: 5, clientY: 5 }));
      dispatch(window, pointerEvent('pointerup', { pointerId: 2, clientX: 6, clientY: 5 }));
      fireEvent.click(label);
      // label's own click must NOT be swallowed by note's pending suppression.
      expect(onCreate).toHaveBeenCalledWith('label', undefined);

      // note's own synthetic click now arrives — it must still be suppressed.
      fireEvent.click(note);
      expect(onCreate).toHaveBeenCalledTimes(1); // only label's, not note's
      expect(onDragCreate).toHaveBeenCalledTimes(1);
    });

    it('keeps two pointers’ suppression windows on the same item independent, so consuming one never erases the other’s', () => {
      // Regression test: suppression is a per-item FIFO queue of per-gesture
      // tokens, not a per-item membership flag. Two different pointers can
      // each complete a drag on the very same button close together (e.g. a
      // fast two-finger touch); each drag's own pending synthetic click must
      // be suppressed, and consuming the first must not erase the second's
      // still-open window.
      const onCreate = vi.fn();
      const onDragCreate = vi.fn();
      render(<AnnotationToolbox onCreate={onCreate} onDragCreate={onDragCreate} touch />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const note = screen.getByRole('button', { name: /^note$/i });

      // Pointer 1 completes a drag on `note`.
      dispatch(note, pointerEvent('pointerdown', { pointerId: 1, clientX: 0, clientY: 0 }));
      dispatch(window, pointerEvent('pointermove', { pointerId: 1, clientX: 50, clientY: 50 }));
      dispatch(window, pointerEvent('pointerup', { pointerId: 1, clientX: 50, clientY: 50 }));

      // Before pointer 1's synthetic click arrives, pointer 2 also completes
      // a drag on the SAME button.
      dispatch(note, pointerEvent('pointerdown', { pointerId: 2, clientX: 1, clientY: 1 }));
      dispatch(window, pointerEvent('pointermove', { pointerId: 2, clientX: 60, clientY: 60 }));
      dispatch(window, pointerEvent('pointerup', { pointerId: 2, clientX: 60, clientY: 60 }));

      expect(onDragCreate).toHaveBeenCalledTimes(2);

      // Both gestures' synthetic clicks arrive — both must be suppressed.
      fireEvent.click(note);
      fireEvent.click(note);
      expect(onCreate).not.toHaveBeenCalled();

      // A genuine third tap, after both suppression windows are consumed,
      // still works normally.
      fireEvent.click(note);
      expect(onCreate).toHaveBeenCalledWith('note', undefined);
    });

    it('leaves nothing behind once every stale fallback timer has actually run, so a later genuine tap still works', async () => {
      // Regression test for a gap the token-queue design (see the
      // `pendingSuppressionsRef` declaration) specifically closes: each
      // completed drag schedules a REAL fallback timer that removes its own
      // gesture's specific token (a no-op if a click already consumed it,
      // which is the ordinary case — a synthetic click fires synchronously
      // with its own pointerup, before any later macrotask). With a bare
      // count instead of per-gesture tokens, a stale timer left over from an
      // earlier, already-consumed gesture could blindly decrement whatever
      // the CURRENT count was for that item — including a different,
      // still-open gesture's on the very same button — once the event loop
      // got a chance to run it, silently letting a later gesture's own
      // synthetic click through as an unwanted extra creation. Real timers
      // (not fake) are used deliberately: firing both gestures' fallback
      // timers together via fake-timer flush cannot be avoided since they
      // share the same nominal delay, so the only way to observe each
      // timer's real no-op behaviour is to let the actual event loop run it
      // after both suppressions are already consumed by their own clicks.
      const onCreate = vi.fn();
      const onDragCreate = vi.fn();
      render(<AnnotationToolbox onCreate={onCreate} onDragCreate={onDragCreate} touch />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const note = screen.getByRole('button', { name: /^note$/i });

      // Pointer 1 completes a drag and its own click consumes the
      // suppression immediately, matching real browsers (a completed
      // gesture's synthetic click is synchronous with its own pointerup;
      // its fallback timer is a later macrotask, still pending here).
      dispatch(note, pointerEvent('pointerdown', { pointerId: 1, clientX: 0, clientY: 0 }));
      dispatch(window, pointerEvent('pointermove', { pointerId: 1, clientX: 50, clientY: 50 }));
      dispatch(window, pointerEvent('pointerup', { pointerId: 1, clientX: 50, clientY: 50 }));
      fireEvent.click(note);
      expect(onCreate).not.toHaveBeenCalled();

      // Pointer 2 completes its own drag on the SAME button before pointer
      // 1's stale timer has run, and its own click likewise consumes its
      // suppression immediately.
      dispatch(note, pointerEvent('pointerdown', { pointerId: 2, clientX: 1, clientY: 1 }));
      dispatch(window, pointerEvent('pointermove', { pointerId: 2, clientX: 60, clientY: 60 }));
      dispatch(window, pointerEvent('pointerup', { pointerId: 2, clientX: 60, clientY: 60 }));
      expect(onDragCreate).toHaveBeenCalledTimes(2);
      fireEvent.click(note);
      expect(onCreate).not.toHaveBeenCalled();

      // Only now let both gestures' now-superfluous fallback timers actually
      // run in the real event loop. Each should find its own token already
      // gone (removed by its own click above) and do nothing.
      await new Promise((resolve) => setTimeout(resolve, 20));

      // A later, genuinely unrelated tap must still work normally — nothing
      // from either stale timer is left behind to swallow it.
      fireEvent.click(note);
      expect(onCreate).toHaveBeenCalledWith('note', undefined);
    });

    it('cleans up in-flight pointer listeners on unmount mid-drag, without throwing or creating anything', () => {
      const onCreate = vi.fn();
      const onDragCreate = vi.fn();
      const { unmount } = render(
        <AnnotationToolbox onCreate={onCreate} onDragCreate={onDragCreate} touch />
      );
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const note = screen.getByRole('button', { name: /^note$/i });
      dispatch(note, pointerEvent('pointerdown', { clientX: 0, clientY: 0 }));
      dispatch(window, pointerEvent('pointermove', { clientX: 50, clientY: 50 }));
      expect(document.querySelector('.annotation-toolbox-drag-ghost')).not.toBeNull();

      act(() => {
        unmount();
      });

      // The component's cleanup effect must have torn down the window
      // listeners this drag attached — a stale pointerup/pointermove
      // dispatched after unmount must neither throw (a listener closing over
      // a torn-down component's setState) nor create anything.
      expect(() => {
        dispatch(window, pointerEvent('pointermove', { clientX: 80, clientY: 80 }));
        dispatch(window, pointerEvent('pointerup', { clientX: 80, clientY: 80 }));
      }).not.toThrow();
      expect(onDragCreate).not.toHaveBeenCalled();
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

  describe('shape slot', () => {
    const STORAGE_KEY = 'communityoverview:annotation-toolbox:shape-slot';

    it('creates the current (default) shape on a plain click, same as any other item', () => {
      const onCreate = vi.fn();
      render(<AnnotationToolbox onCreate={onCreate} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      fireEvent.click(screen.getByRole('button', { name: /^rectangle$/i }));
      expect(onCreate).toHaveBeenCalledWith('shape', { shape: 'rectangle' });
    });

    it('opens the picker on a corner-button click, without creating a shape', () => {
      const onCreate = vi.fn();
      render(<AnnotationToolbox onCreate={onCreate} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
      expect(screen.queryByRole('group', { name: /^shapes$/i })).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole('button', { name: /choose a shape/i }));

      expect(screen.getByRole('group', { name: /^shapes$/i })).toBeInTheDocument();
      expect(onCreate).not.toHaveBeenCalled();
    });

    it('opens the picker on a right-click of the slot, without creating a shape', () => {
      const onCreate = vi.fn();
      render(<AnnotationToolbox onCreate={onCreate} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      fireEvent.contextMenu(screen.getByRole('button', { name: /^rectangle$/i }));

      expect(screen.getByRole('group', { name: /^shapes$/i })).toBeInTheDocument();
      expect(onCreate).not.toHaveBeenCalled();
    });

    it('is keyboard-reachable: the corner button is a real, enabled, native <button> distinct from the slot, so Enter/Space opens the picker', () => {
      // jsdom does not synthesize the browser's native "Enter/Space on a
      // focused <button> fires click" behaviour, so there is nothing to
      // dispatch here beyond what any real user agent already guarantees for
      // a native, enabled <button>. What actually has to be proven is that
      // the corner button IS that — not a div/span standing in for one,
      // which Enter/Space would silently do nothing to.
      render(<AnnotationToolbox onCreate={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const slot = screen.getByRole('button', { name: /^rectangle$/i });
      const corner = screen.getByRole('button', { name: /choose a shape/i });
      expect(corner).not.toBe(slot);
      expect(corner.tagName).toBe('BUTTON');
      expect(corner).toHaveAttribute('type', 'button');
      expect(corner).not.toBeDisabled();

      fireEvent.click(corner);
      expect(screen.getByRole('group', { name: /^shapes$/i })).toBeInTheDocument();
    });

    it('keeps the slot and the corner button as two separate, sequential elements in tab order', () => {
      render(<AnnotationToolbox onCreate={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      const slot = screen.getByRole('button', { name: /^rectangle$/i });
      const corner = screen.getByRole('button', { name: /choose a shape/i });
      expect(slot).not.toBe(corner);
      // Neither opts out of the natural tab order.
      expect(slot).not.toHaveAttribute('tabindex', '-1');
      expect(corner).not.toHaveAttribute('tabindex', '-1');
      // DOM order is tab order here (both at the default tabIndex) — the
      // slot's own button must precede its corner button.
      const position = slot.compareDocumentPosition(corner);
      expect(Boolean(position & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
    });

    it('selecting a shape in the picker updates the slot and persists the choice to localStorage', () => {
      render(<AnnotationToolbox onCreate={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      selectShapeVariant(/^hexagon$/i);

      expect(screen.queryByRole('group', { name: /^shapes$/i })).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: /^hexagon$/i })).toBeInTheDocument();
      expect(localStorage.getItem(STORAGE_KEY)).toBe('hexagon');
    });

    it('reads the remembered shape from localStorage on mount, not the built-in default', () => {
      localStorage.setItem(STORAGE_KEY, 'triangle');
      render(<AnnotationToolbox onCreate={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      expect(screen.getByRole('button', { name: /^triangle$/i })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /^rectangle$/i })).not.toBeInTheDocument();
    });

    it('falls back to the built-in default for a stored value that names no real shape', () => {
      localStorage.setItem(STORAGE_KEY, 'not-a-real-shape');
      render(<AnnotationToolbox onCreate={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      expect(screen.getByRole('button', { name: /^rectangle$/i })).toBeInTheDocument();
    });

    it('closes the picker on Escape and returns focus to the corner button', () => {
      render(<AnnotationToolbox onCreate={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      fireEvent.click(screen.getByRole('button', { name: /choose a shape/i }));
      const picker = screen.getByRole('group', { name: /^shapes$/i });
      fireEvent.keyDown(picker, { key: 'Escape' });

      expect(screen.queryByRole('group', { name: /^shapes$/i })).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: /choose a shape/i })).toHaveFocus();
    });

    it('closes the picker on an outside click, without creating a shape', () => {
      const onCreate = vi.fn();
      render(<AnnotationToolbox onCreate={onCreate} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      fireEvent.click(screen.getByRole('button', { name: /choose a shape/i }));
      expect(screen.getByRole('group', { name: /^shapes$/i })).toBeInTheDocument();

      fireEvent.mouseDown(document.body);

      expect(screen.queryByRole('group', { name: /^shapes$/i })).not.toBeInTheDocument();
      expect(onCreate).not.toHaveBeenCalled();
    });

    it('does not open the picker or create anything from a plain click inside the picker background', () => {
      // Regression guard for the outside-click handler: clicking one of the
      // picker's own option buttons must select it (covered elsewhere), not
      // be treated as "outside" and just close without effect.
      render(<AnnotationToolbox onCreate={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));

      fireEvent.click(screen.getByRole('button', { name: /choose a shape/i }));
      const picker = screen.getByRole('group', { name: /^shapes$/i });
      fireEvent.mouseDown(picker);

      expect(screen.getByRole('group', { name: /^shapes$/i })).toBeInTheDocument();
    });

    it('closes the picker when the toolbox itself collapses', () => {
      render(<AnnotationToolbox onCreate={vi.fn()} />);
      const toggle = screen.getByRole('button', { name: /add annotation/i });
      fireEvent.click(toggle);
      fireEvent.click(screen.getByRole('button', { name: /choose a shape/i }));
      expect(screen.getByRole('group', { name: /^shapes$/i })).toBeInTheDocument();

      fireEvent.click(screen.getByRole('button', { name: /collapse annotation toolbox/i }));

      expect(screen.queryByRole('group', { name: /^shapes$/i })).not.toBeInTheDocument();
    });

    it("positions the panel with `top` (paired with the CSS translateY(-100%)), not `bottom`, so it isn't double-offset away from the slot", () => {
      // Regression test: `position: fixed` + `bottom: Npx` already anchors
      // the panel's own bottom edge N px above the viewport bottom: pairing
      // that with the stylesheet's translateY(-100%) (meant for the `top`
      // convention AnnotationToolbox's own hover tooltip uses — see
      // `showTip`) shifts it up by its own height a second time, landing it
      // detached from the slot instead of flush above it.
      render(<AnnotationToolbox onCreate={vi.fn()} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
      fireEvent.click(screen.getByRole('button', { name: /choose a shape/i }));

      const picker = screen.getByRole('group', { name: /^shapes$/i });
      expect(picker.style.top).not.toBe('');
      expect(picker.style.bottom).toBe('');
    });

    it('does not steal focus back on close once it has already moved elsewhere while the picker was open', () => {
      // Regression test: once focus has genuinely moved to some other
      // focusable element while the picker is open (a `focusin` outside the
      // panel), closing the picker must not yank it back onto the corner
      // button — mirrors ContextMenus.jsx's useMenuOpenFocus, which skips its
      // own restore once a `focusin` fired somewhere outside the menu.
      render(
        <div>
          <button type="button">elsewhere</button>
          <AnnotationToolbox onCreate={vi.fn()} />
        </div>
      );
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
      fireEvent.click(screen.getByRole('button', { name: /choose a shape/i }));
      const picker = screen.getByRole('group', { name: /^shapes$/i });

      // Focus genuinely moves elsewhere while the picker is still open.
      const elsewhere = screen.getByRole('button', { name: /^elsewhere$/i });
      act(() => {
        elsewhere.focus();
      });

      // Close via Escape — must not restore focus to the corner button now
      // that it has already moved away on its own.
      fireEvent.keyDown(picker, { key: 'Escape' });

      expect(screen.queryByRole('group', { name: /^shapes$/i })).not.toBeInTheDocument();
      expect(elsewhere).toHaveFocus();
    });
  });
});
