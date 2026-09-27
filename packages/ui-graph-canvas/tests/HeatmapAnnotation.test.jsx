import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { render, screen, fireEvent } from '@testing-library/react';
import GenericAnnotationNode from '../src/components/GenericAnnotationNode';
import { AnnotationContext } from '../src/components/AnnotationContext';
import {
  createAnnotation,
  normalizeHeatmapIntensity,
  HEATMAP_DEFAULT_DIAMETER,
  HEATMAP_DEFAULT_INTENSITY,
} from '../src/utils/annotationModel';
import {
  computeAnnotationAriaLabel,
  flowNodeToOverlay,
  heatmapFillStyle,
  heatmapPeakAlpha,
  overlayToFlowNode,
  HEATMAP_PEAK_ALPHA,
} from '../src/utils/annotations';

const hoisted = vi.hoisted(() => ({ resizerProps: [], setNodes: vi.fn() }));

vi.mock('reactflow', () => ({
  NodeResizer: (props) => {
    hoisted.resizerProps.push(props);
    return <div data-testid="resizer" />;
  },
  useReactFlow: () => ({ setNodes: hoisted.setNodes, getNodes: () => [] }),
}));

function renderHeatmap(data, { selected = false, notifyChange = vi.fn() } = {}) {
  const utils = render(
    <AnnotationContext.Provider value={{ notifyChange, labels: {} }}>
      <GenericAnnotationNode id="h1" type="heatmap" data={data} selected={selected} />
    </AnnotationContext.Provider>
  );
  return { ...utils, notifyChange, circle: utils.container.querySelector('.kind-heatmap') };
}

describe('heat-map intensity model', () => {
  it('keeps whole levels 0-10 as they are', () => {
    for (let level = 0; level <= 10; level += 1) {
      expect(normalizeHeatmapIntensity(level)).toBe(level);
    }
  });

  it('clamps, rounds and defaults anything else to a whole level', () => {
    expect(normalizeHeatmapIntensity(-3)).toBe(0);
    expect(normalizeHeatmapIntensity(42)).toBe(10);
    expect(normalizeHeatmapIntensity(6.6)).toBe(7);
    expect(normalizeHeatmapIntensity('4')).toBe(4);
    expect(normalizeHeatmapIntensity(undefined)).toBe(HEATMAP_DEFAULT_INTENSITY);
    expect(normalizeHeatmapIntensity(null)).toBe(HEATMAP_DEFAULT_INTENSITY);
    expect(normalizeHeatmapIntensity('hot')).toBe(HEATMAP_DEFAULT_INTENSITY);
    expect(normalizeHeatmapIntensity(Number.NaN)).toBe(HEATMAP_DEFAULT_INTENSITY);
  });

  it('keeps an explicit 0 rather than treating it as unset', () => {
    expect(createAnnotation({ type: 'heatmap', intensity: 0 }).intensity).toBe(0);
  });

  it('gives a heat-map with no stored size a circle of the default diameter', () => {
    const annotation = createAnnotation({ type: 'heatmap', position: { x: 1, y: 2 } });
    expect(annotation.geometry.w).toBe(HEATMAP_DEFAULT_DIAMETER);
    expect(annotation.geometry.h).toBe(HEATMAP_DEFAULT_DIAMETER);
    expect(annotation.intensity).toBe(HEATMAP_DEFAULT_INTENSITY);
  });
});

describe('heat-map rendering semantics', () => {
  it('is fully transparent at level 0 and strongest at level 10', () => {
    expect(heatmapPeakAlpha(0)).toBe(0);
    expect(heatmapPeakAlpha(10)).toBe(HEATMAP_PEAK_ALPHA);
    expect(heatmapFillStyle(0).backgroundImage).not.toMatch(/rgba\([^)]*, 0\.\d+\)|, 1\)/);
  });

  it('never paints an opaque disk, even at level 10', () => {
    expect(HEATMAP_PEAK_ALPHA).toBeLessThan(1);
    // The rim is always fully transparent, which is what makes neighbouring
    // circles merge into one soft field instead of meeting at a hard edge.
    expect(heatmapFillStyle(10).backgroundImage).toMatch(/rgba\(220, 38, 38, 0\) 100%\)$/);
  });

  it('gets strictly denser with every level', () => {
    for (let level = 1; level <= 10; level += 1) {
      expect(heatmapPeakAlpha(level)).toBeGreaterThan(heatmapPeakAlpha(level - 1));
    }
  });

  it('draws a circle even in a box that is not square', () => {
    expect(heatmapFillStyle(5).backgroundImage).toMatch(/^radial-gradient\(circle closest-side,/);
  });

  it('uses one colour for every level, so overlapping circles blend the same in any z order', () => {
    const colours = new Set();
    for (let level = 1; level <= 10; level += 1) {
      for (const match of heatmapFillStyle(level).backgroundImage.matchAll(
        /rgba\((\d+, \d+, \d+),/g
      )) {
        colours.add(match[1]);
      }
    }
    expect([...colours]).toEqual(['220, 38, 38']);
  });
});

describe('heat-map accessible name', () => {
  it('names the kind and its level', () => {
    expect(computeAnnotationAriaLabel('heatmap', { intensity: 7 })).toBe(
      'Heat-map circle, intensity 7 of 10'
    );
    expect(computeAnnotationAriaLabel('heatmap', { intensity: 0 })).toBe(
      'Heat-map circle, intensity 0 of 10'
    );
  });

  it.each([
    [7.6, 8],
    [15, 10],
    [-2, 0],
    ['x', 5],
    [undefined, 5],
  ])('names a stored %s as the level it draws, %i', (stored, level) => {
    expect(computeAnnotationAriaLabel('heatmap', { intensity: stored })).toBe(
      `Heat-map circle, intensity ${level} of 10`
    );
  });

  it('fills a host-translated template', () => {
    expect(
      computeAnnotationAriaLabel(
        'heatmap',
        { intensity: 3 },
        { ariaKindHeatmap: 'Värmekartcirkel, intensitet {level} av {max}' }
      )
    ).toBe('Värmekartcirkel, intensitet 3 av 10');
  });
});

describe('heat-map overlay round trip', () => {
  it('carries intensity and size through overlay -> flow node -> overlay', () => {
    const overlay = {
      id: 'h1',
      kind: 'heatmap',
      position: { x: 10, y: 20 },
      intensity: 0,
      size: { w: 220, h: 220 },
      z: 0,
      locked: false,
      rotation: 0,
    };
    const node = overlayToFlowNode(overlay);
    expect(node.type).toBe('heatmap');
    expect(node.style).toEqual({ width: 220, height: 220 });
    expect(node.data.intensity).toBe(0);
    const back = flowNodeToOverlay(node);
    expect(back.intensity).toBe(0);
    expect(back.size).toEqual({ w: 220, h: 220 });
  });

  it('leaves the other sized kinds on their own 160x96 default', () => {
    expect(overlayToFlowNode({ id: 's1', kind: 'shape', position: { x: 0, y: 0 } }).style).toEqual({
      width: 160,
      height: 96,
    });
    for (const type of ['shape', 'image', 'note']) {
      const { geometry } = createAnnotation({ type, position: { x: 0, y: 0 } });
      expect([geometry.w, geometry.h]).toEqual([160, 96]);
    }
  });

  it('keeps an unsized image on the generic 160x96 box, not the heat-map square', () => {
    const node = overlayToFlowNode({ id: 'i1', kind: 'image', position: { x: 0, y: 0 } });
    expect(node.style).toEqual({ width: 160, height: 96 });
  });

  it('gives an unsized heat-map overlay a square default box', () => {
    const node = overlayToFlowNode({ id: 'h2', kind: 'heatmap', position: { x: 0, y: 0 } });
    expect(node.style).toEqual({
      width: HEATMAP_DEFAULT_DIAMETER,
      height: HEATMAP_DEFAULT_DIAMETER,
    });
  });
});

describe('GenericAnnotationNode — heatmap', () => {
  beforeEach(() => {
    hoisted.resizerProps.length = 0;
    hoisted.setNodes.mockClear();
  });

  it('paints the level as a radial gradient', () => {
    const { circle } = renderHeatmap({ intensity: 8 });
    expect(circle).toBeTruthy();
    expect(circle.querySelector('.graph-heatmap-circle').style.backgroundImage).toContain(
      'radial-gradient'
    );
    expect(circle.getAttribute('data-intensity')).toBe('8');
    expect(circle.classList.contains('is-empty')).toBe(false);
  });

  it('marks a level-0 circle so it stays findable although it paints nothing', () => {
    const { circle } = renderHeatmap({ intensity: 0 });
    expect(circle.classList.contains('is-empty')).toBe(true);
    expect(heatmapPeakAlpha(0)).toBe(0);
  });

  it.each([0, 1, 7, 10])('paints exactly the fill for level %i on the circle', (level) => {
    const { circle } = renderHeatmap({ intensity: level });
    expect(circle.querySelector('.graph-heatmap-circle').style.backgroundImage).toBe(
      heatmapFillStyle(level).backgroundImage
    );
  });

  it('offers a locked circle only unlock and duplicate, no intensity buttons', () => {
    const { circle, notifyChange } = renderHeatmap({ intensity: 5, locked: true });
    fireEvent.contextMenu(circle);
    expect(screen.queryByRole('button', { name: 'Intensity' })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Intensity \d+$/ })).toBeNull();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });

  // A stored intensity is read through normalizeHeatmapIntensity everywhere it
  // is used, so a raw value that is out of range, fractional or absent must
  // behave exactly like the whole level it normalises to.
  it.each([-2, 0.3])('treats a stored %s as level 0 and marks it empty', (stored) => {
    const { circle } = renderHeatmap({ intensity: stored });
    expect(circle.getAttribute('data-intensity')).toBe('0');
    expect(circle.classList.contains('is-empty')).toBe(true);
  });

  it.each([
    [undefined, 5],
    [7.6, 8],
  ])('marks level %s as %i and publishes nothing when that level is chosen', (stored, level) => {
    const data = stored === undefined ? {} : { intensity: stored };
    const { circle, notifyChange } = renderHeatmap(data);
    fireEvent.contextMenu(circle);
    fireEvent.click(screen.getByRole('button', { name: 'Intensity' }));
    const pressed = screen
      .getAllByRole('button', { name: /^Intensity \d+$/ })
      .filter((b) => b.getAttribute('aria-pressed') === 'true');
    expect(pressed.map((b) => b.textContent)).toEqual([String(level)]);
    fireEvent.click(screen.getByRole('button', { name: `Intensity ${level}` }));
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('leaves an image free to change its aspect ratio while resizing', () => {
    render(
      <AnnotationContext.Provider value={{ notifyChange: vi.fn(), labels: {} }}>
        <GenericAnnotationNode id="i1" type="image" data={{ image: { url: 'x.png' } }} selected />
      </AnnotationContext.Provider>
    );
    const props = hoisted.resizerProps.at(-1);
    expect(props.isVisible).toBe(true);
    expect(props.keepAspectRatio).toBe(false);
  });

  it('draws the circle as its own element inside the box', () => {
    const { circle } = renderHeatmap({ intensity: 3 });
    expect(circle.style.backgroundImage).toBe('');
    expect(circle.querySelectorAll('.graph-heatmap-circle')).toHaveLength(1);
  });

  it('shows the level as a number only while selected', () => {
    renderHeatmap({ intensity: 6 });
    expect(screen.queryByText('6')).toBeNull();
    renderHeatmap({ intensity: 6 }, { selected: true });
    expect(screen.getByText('6')).toBeInTheDocument();
  });

  it('is resizable with a locked, square aspect', () => {
    renderHeatmap({ intensity: 5 }, { selected: true });
    const props = hoisted.resizerProps.at(-1);
    expect(props.keepAspectRatio).toBe(true);
    expect(props.isVisible).toBe(true);
  });

  it('hides the resize handles while locked', () => {
    renderHeatmap({ intensity: 5, locked: true }, { selected: true });
    expect(hoisted.resizerProps.at(-1).isVisible).toBe(false);
  });

  it('offers one button per level 0-10, marking the current one', () => {
    const { circle } = renderHeatmap({ intensity: 5 });
    fireEvent.contextMenu(circle);
    fireEvent.click(screen.getByRole('button', { name: 'Intensity' }));
    const levels = screen.getAllByRole('button', { name: /^Intensity \d+$/ });
    expect(levels.map((b) => b.textContent)).toEqual([
      '0',
      '1',
      '2',
      '3',
      '4',
      '5',
      '6',
      '7',
      '8',
      '9',
      '10',
    ]);
    expect(screen.getByRole('button', { name: 'Intensity 5' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(screen.getByRole('button', { name: 'Intensity 4' })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
  });

  it('sets the intensity with one change per choice, reported as a style change', () => {
    const { circle, notifyChange } = renderHeatmap({ intensity: 5 });
    fireEvent.contextMenu(circle);
    fireEvent.click(screen.getByRole('button', { name: 'Intensity' }));
    fireEvent.click(screen.getByRole('button', { name: 'Intensity 0' }));
    expect(hoisted.setNodes).toHaveBeenCalledTimes(1);
    const updater = hoisted.setNodes.mock.calls.at(-1)[0];
    const [updated] = updater([{ id: 'h1', type: 'heatmap', data: { intensity: 5 } }]);
    expect(updated.data.intensity).toBe(0);
    expect(notifyChange).toHaveBeenCalledTimes(1);
    expect(notifyChange).toHaveBeenCalledWith('style');
  });

  it('publishes nothing when the current level is chosen again', () => {
    const { circle, notifyChange } = renderHeatmap({ intensity: 5 });
    fireEvent.contextMenu(circle);
    fireEvent.click(screen.getByRole('button', { name: 'Intensity' }));
    fireEvent.click(screen.getByRole('button', { name: 'Intensity 5' }));
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('refuses an intensity change when another client takes the lease while the menu is open', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    const notifyChange = vi.fn();
    const ui = (data) => (
      <AnnotationContext.Provider value={{ notifyChange, notifyRemoteLockedAttempt, labels: {} }}>
        <GenericAnnotationNode id="h1" type="heatmap" data={data} />
      </AnnotationContext.Provider>
    );
    const { container, rerender } = render(ui({ intensity: 5 }));
    fireEvent.contextMenu(container.querySelector('.kind-heatmap'));
    fireEvent.click(screen.getByRole('button', { name: 'Intensity' }));
    rerender(ui({ intensity: 5, remoteLease: { clientId: 'other', displayName: 'Other' } }));
    fireEvent.click(screen.getByRole('button', { name: 'Intensity 9' }));
    expect(notifyRemoteLockedAttempt).toHaveBeenCalled();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('offers no rotation or opacity control — intensity is its only transparency', () => {
    const { circle } = renderHeatmap({ intensity: 5 });
    fireEvent.contextMenu(circle);
    expect(screen.queryByRole('button', { name: 'Rotation' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Opacity' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Size' })).toBeInTheDocument();
  });

  it('refuses an intensity change while another client holds the edit lease', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    const { container } = render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt, labels: {} }}
      >
        <GenericAnnotationNode
          id="h1"
          type="heatmap"
          data={{ intensity: 5, remoteLease: { clientId: 'other', displayName: 'Other' } }}
        />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(container.querySelector('.kind-heatmap'));
    expect(notifyRemoteLockedAttempt).toHaveBeenCalled();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
  });
});

// jsdom never loads stylesheets or evaluates media queries, so the one CSS
// rule a guarantee rests on is checked as text: under forced colours a level-0
// circle must not get the permanent outline visible circles get.
describe('heat-map stylesheet', () => {
  const css = readFileSync(
    path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      '../src/components/GenericAnnotationNode.css'
    ),
    'utf-8'
  );

  // Every rule's selector list and body, with the @media block (if any) it
  // sits in. Enough of a parser for this stylesheet: comments stripped, one
  // level of nesting, no strings containing braces.
  const parseRules = (source) => {
    const out = [];
    const text = source.replace(/\/\*[\s\S]*?\*\//g, '');
    let media = null;
    let i = 0;
    while (i < text.length) {
      const open = text.indexOf('{', i);
      const close = text.indexOf('}', i);
      if (close !== -1 && (open === -1 || close < open)) {
        media = null;
        i = close + 1;
        continue;
      }
      if (open === -1) break;
      const prelude = text.slice(i, open).trim();
      if (prelude.startsWith('@media')) {
        media = prelude;
        i = open + 1;
        continue;
      }
      const end = text.indexOf('}', open);
      out.push({
        media,
        selectors: prelude.split(',').map((sel) => sel.trim()),
        body: text.slice(open + 1, end),
      });
      i = end + 1;
    }
    return out;
  };
  const rules = parseRules(css);
  const inForcedColours = (r) => Boolean(r.media?.includes('forced-colors'));
  // Declarations that make an element visible. Every border property draws —
  // longhands and logical border-inline*/border-block* included — except the
  // radii, which only shape the circle. backdrop-filter paints the box's area
  // even when the box itself is transparent; `filter` counts conservatively,
  // since a url(...) filter can flood the box and a drop-shadow copies
  // whatever the box or its children draw. outline-offset and background-size
  // and the like only adjust what something else draws, and border-collapse
  // and border-spacing are table layout. A -webkit- prefix draws the same.
  //
  // A value draws nothing when every token is a no-op (`border: 0 none`,
  // `outline: 0em`). A border or outline shorthand carries one width and one
  // style, so either being zero, none or hidden draws nothing either — unless
  // the style is `auto`, whose width a browser may ignore, or any part is a
  // function such as var(...) that could resolve to it. That shorthand rule
  // does not extend to longhands (`border-width: 0 2px` still draws a side) or
  // to box-shadow (`0 0 0 1px red` is a ring). `transparent` hides only a
  // background: forced-colours mode repaints border and outline colours.
  // `initial` counts as painting: on border-color or border-width it means
  // currentcolor or a medium width, though on some properties, such as
  // border-style or box-shadow, it is a no-op. A quoted string, or a
  // parenthesised group such as rgba(...) or calc((...)) with any nesting,
  // counts as one opaque token; a value whose parentheses do not balance
  // counts as painting, and so does anything else not listed here.
  const PAINTING_PROPERTY =
    /^(?:-webkit-)?(border(-[a-z-]+)?|outline(-(color|style|width))?|box-shadow|(backdrop-)?filter|background(-(color|image))?)$/;
  const BORDER_SHORTHAND =
    /^(?:-webkit-)?(border(-(top|right|bottom|left|inline|block)(-(start|end))?)?|outline)$/;
  const ZERO_LENGTH = /^0+(\.0+)?([a-z]+|%)?$/;
  const isNoOpToken = (token) => ['none', 'hidden'].includes(token) || ZERO_LENGTH.test(token);
  const paints = (body) =>
    body.split(';').some((decl) => {
      const colon = decl.indexOf(':');
      if (colon === -1) return false;
      const property = decl.slice(0, colon).trim().toLowerCase();
      const value = decl
        .slice(colon + 1)
        .replace(/!important/i, '')
        .trim()
        .toLowerCase();
      if (
        !PAINTING_PROPERTY.test(property) ||
        property.endsWith('-radius') ||
        /^(?:-webkit-)?border-(collapse|spacing)$/.test(property)
      ) {
        return false;
      }
      let flat = value.replace(/"[^"]*"|'[^']*'/g, 'str');
      for (let prev; prev !== flat;) {
        prev = flat;
        flat = flat.replace(/[\w-]*\([^()]*\)/g, 'fn');
      }
      if (/[()]/.test(flat)) return true;
      const tokens = flat.split(/\s+/);
      const noOp = (t) =>
        isNoOpToken(t) || (t === 'transparent' && property.startsWith('background'));
      if (tokens.every(noOp)) return false;
      const mayBeAuto = tokens.some((t) => t === 'auto' || t === 'fn');
      if (BORDER_SHORTHAND.test(property) && !mayBeAuto && tokens.some(isNoOpToken)) return false;
      return true;
    });
  const onHeatmapCircle = (sel) => /\.(kind-heatmap|graph-heatmap-circle)(?![\w-])/.test(sel);
  // Each :not(...) is cut out whole, nested parentheses included; one never
  // closed takes the rest of the selector with it. `top` marks a :not that
  // sits directly on its compound rather than inside another pseudo-class.
  const scanNots = (sel) => {
    let bare = '';
    const nots = [];
    let depth = 0;
    let i = 0;
    while (i < sel.length) {
      if (/^:not\(/i.test(sel.slice(i, i + 5))) {
        let inner = 1;
        let j = i + 5;
        for (; j < sel.length && inner > 0; j++) {
          if (sel[j] === '(') inner++;
          else if (sel[j] === ')') inner--;
        }
        nots.push({ arg: sel.slice(i + 5, inner === 0 ? j - 1 : j), top: depth === 0 });
        i = j;
        continue;
      }
      if (sel[i] === '(') depth++;
      else if (sel[i] === ')' && depth > 0) depth--;
      bare += sel[i++];
    }
    return { bare, nots };
  };
  const withoutNot = (sel) => scanNots(sel).bare;
  const compoundsOf = (sel) => {
    const parts = [''];
    let depth = 0;
    for (const c of sel.trim()) {
      if (c === '(' || c === '[') depth++;
      else if ((c === ')' || c === ']') && depth > 0) depth--;
      if (depth === 0 && /[\s>+~]/.test(c)) {
        if (parts.at(-1)) parts.push('');
      } else {
        parts[parts.length - 1] += c;
      }
    }
    return parts.filter(Boolean);
  };
  // `is-empty` is set on the heat-map node itself, so only a :not(.is-empty)
  // on the node's own compound keeps a level-0 circle out of a rule.
  const excludesEmptyHeatmap = (sel) =>
    compoundsOf(sel).some((compound) => {
      const { bare, nots } = scanNots(compound);
      return (
        /\.(graph-generic-annotation-node|kind-heatmap)(?![\w-])/.test(bare) &&
        nots.some((n) => n.top && n.arg.trim() === '.is-empty')
      );
    });
  // The element a selector styles is its last compound; a class further left
  // only scopes it. A selector is set aside only when its last compound is
  // plain — at least one class, optionally a type, and argument-free
  // pseudo-classes or pseudo-elements — and carries none of the classes the
  // heat-map node or its circle can have, as
  // `.kind-heatmap .graph-heatmap-level` does. Anything else, such as
  // :has(...), :not(...), :is(...), [attr] or `*`, could still narrow to the
  // node or the circle, so it is judged as before, by the whole selector. So
  // is every selector of a rule whose selector list has a parenthesis,
  // bracket, quote or escape anywhere in it: the rule parser splits lists on
  // every comma, even one inside :is(a, b, c), so a fragment's plain-looking
  // last piece may not be its selector's last compound.
  //
  // And nothing in a sheet is set aside unless the sheet is flat enough for
  // the parser to read it as CSS does: no stripped comment holds a brace (the
  // comments are stripped as text, so url(/*) ... url(*/) would swallow the
  // rules between them); the rest holds no `&`, quote or backslash anywhere,
  // since a `}` in a string or escape ends a body or skips a prelude early
  // and `&` refers to an outer rule; and no parsed body holds `{` or an
  // unbalanced parenthesis, the marks a nested block or a `}` inside
  // url(...) leaves behind.
  const NODE_OR_CIRCLE_CLASS =
    /\.(graph-generic-annotation-node|kind-heatmap|is-empty|selected|graph-heatmap-circle)(?![\w-])/;
  const isBalanced = (body) => {
    let depth = 0;
    for (const c of body) {
      if (c === '(') depth++;
      else if (c === ')' && --depth < 0) return false;
    }
    return depth === 0;
  };
  const isFlatSheet = (source) => {
    const comments = source.match(/\/\*[\s\S]*?\*\//g) ?? [];
    if (comments.some((c) => /[{}]/.test(c))) return false;
    if (/[&"'\\]/.test(source.replace(/\/\*[\s\S]*?\*\//g, ''))) return false;
    return parseRules(source).every((r) => !r.body.includes('{') && isBalanced(r.body));
  };
  const sheetIsFlat = isFlatSheet(css);
  const stylesHeatmapCircle = (sel, list = sel, flat = true) => {
    const last = sel
      .trim()
      .split(/\s*[\s>+~]\s*/)
      .pop();
    const plain =
      flat &&
      !/[()[\]"'\\]/.test(list) &&
      /^[\w-]*(?:(?:\.|::?)[\w-]+)+$/.test(last) &&
      last.includes('.');
    return onHeatmapCircle(sel) && !(plain && !NODE_OR_CIRCLE_CLASS.test(last));
  };

  it.each([
    ['border-top-color: red', true],
    ['border-inline-start: 1px solid red', true],
    ['border-block-width: 2px', true],
    ['border: 1px dashed rgba(220, 38, 38, 0.6)', true],
    ['outline: 2px solid CanvasText', true],
    ['box-shadow: 0 0 0 1px red', true],
    ['background: rgba(220, 38, 38, 0.2)', true],
    ['border-radius: 50%', false],
    ['border-top-left-radius: 4px', false],
    ['border-start-end-radius: 4px', false],
    ['box-sizing: border-box', false],
    ['outline-offset: 2px', false],
    ['background-color: transparent', false],
    ['background-size: cover', false],
    ['border: none', false],
    ['box-shadow: none !important', false],
    ['--border: 1px solid red', false],
    ['-webkit-box-shadow: 0 0 0 1px red', true],
    ['-webkit-border-radius: 50%', false],
    ['border-collapse: separate', false],
    ['border-spacing: 2px', false],
    ['border: 0 none', false],
    ['border: 0em solid red', false],
    ['border: 1px solid transparent', true],
    ['outline: 2px solid transparent', true],
    ['border-color: transparent', true],
    ['background: transparent none', false],
    ['border-top: 2px hidden red', false],
    ['outline: 0em', false],
    ['outline: 1px none red', false],
    ['outline: 0 auto', true],
    ['outline: 0 var(--ring-style, auto)', true],
    ['border-color: initial', true],
    ['border-width: initial', true],
    ['border-style: hidden', false],
    ['border-width: 0 2px', true],
    ['border-color: transparent red', true],
    ['border: 1px solid rgba(0 0 0 / 0.5)', true],
    ['border: 2px solid hsl(var(--h) 0% 40%)', true],
    ['border: 2px solid rgb(calc((0)) 0 0)', true],
    ['border: 2px solid rgb(0 0 0', true],
    ['box-shadow: 0 0 0 1px transparent, 0 0 0 2px red', true],
    ['filter: drop-shadow(0 0 2px red)', true],
    ['-webkit-filter: drop-shadow(0 0 2px red)', true],
    ['backdrop-filter: blur(2px)', true],
    ['-webkit-backdrop-filter: blur(2px)', true],
    ['filter: none', false],
    ['backdrop-filter: none', false],
  ])('treats `%s` as painting: %s', (decl, expected) => {
    expect(paints(`\n  width: 10px;\n  ${decl};\n`)).toBe(expected);
  });

  it('checks forced-colours rules on the circle, not on the intensity menu', () => {
    expect(
      onHeatmapCircle('.graph-generic-annotation-node.kind-heatmap .graph-heatmap-circle')
    ).toBe(true);
    expect(onHeatmapCircle('.graph-heatmap-circle')).toBe(true);
    expect(onHeatmapCircle('.heatmap-level-button.active')).toBe(false);
    expect(onHeatmapCircle('.context-menu-heatmap-intensity')).toBe(false);
    expect(onHeatmapCircle('.kind-heatmap-legend')).toBe(false);
    expect(onHeatmapCircle('.graph-heatmap-circle-label')).toBe(false);
    expect(onHeatmapCircle('.kind-heatmapx .graph-heatmap-circle_inner')).toBe(false);
  });

  it.each([
    ['.graph-generic-annotation-node.kind-heatmap.is-empty:hover .graph-heatmap-circle', true],
    ['.graph-generic-annotation-node.kind-heatmap', true],
    ['.kind-heatmap > .graph-heatmap-circle', true],
    ['.kind-heatmap:not(.is-empty) .graph-heatmap-circle', true],
    ['.kind-heatmap .selected', true],
    ['.kind-heatmap.is-empty > *', true],
    ['.kind-heatmap.is-empty div', true],
    ['.kind-heatmap.is-empty:has(> .graph-heatmap-level)', true],
    ['.graph-generic-annotation-node:has(> .graph-heatmap-circle)', true],
    ['.graph-generic-annotation-node:not(.kind-heatmap:hover)', true],
    ['.graph-generic-annotation-node:is(.kind-heatmap).is-empty', true],
    ['.graph-generic-annotation-node.is-empty:nth-child(n of .kind-heatmap)', true],
    ['.graph-generic-annotation-node.is-empty:nth-child(n OF .kind-heatmap)', true],
    ['.graph-generic-annotation-node.is-empty:not(:not(.kind-heatmap))', true],
    ['.kind-heatmap .graph-heatmap-circle:nth-child(2n + 1)', true],
    ['.kind-heatmap .x:where(.graph-heatmap-circle)', true],
    ['.graph-heatmap-circle[title="a] .b"]', true],
    ['.graph-heatmap-circle[title=") .b"]', true],
    ['.kind-heatmap.is-empty > :is(.a .b', true],
    ['.kind-heatmap.is-empty .x:where(.a .b', true],
    ['.kind-heatmap .graph-heatmap-level', false],
    ['.kind-heatmap.is-empty > .graph-heatmap-level', false],
    ['.kind-heatmap.is-empty:hover .graph-heatmap-level::before', false],
    ['.kind-heatmap ~ .other', false],
    ['.graph-heatmap-circle-label', false],
  ])('judges `%s` as styling the heat-map node or circle: %s', (sel, expected) => {
    expect(stylesHeatmapCircle(sel)).toBe(expected);
  });

  it.each([
    ['.a { color: red; }\n@media (x) { .b { background: rgba(1, 2, 3, 0.5); } }', true],
    ['/* a plain note */ .a { color: red; }', true],
    ['.a { color: red; :is(&, .x) { border: 1px solid; } }', false],
    ['.a { color: red; & .x { border: 1px solid; } }', false],
    ['.a { .b { color: red } border: 1px solid; .c { outline: 1px solid } }', false],
    ['@scope (.a) { .b { color: red } :scope, .c { border: 1px solid } }', false],
    ['.a { background-image: url(}); }', false],
    ['.a { grid-area: \\}; }', false],
    ['.a { content: "}"; }', false],
    ['.a, .z\\}, .b { border: 1px solid; }', false],
    ['.a { --p: url(/*); }\n.b { --q: url(*/); border: 1px solid; }', false],
    ['/* { */ .a { color: red; }', false],
  ])('reads `%s` as a flat sheet: %s', (source, expected) => {
    expect(isFlatSheet(source)).toBe(expected);
  });

  it('judges a list fragment by the whole selector list it came from', () => {
    const list = ':is(.a, .kind-heatmap.is-empty .wrap, .b:hover) .graph-heatmap-circle';
    expect(stylesHeatmapCircle('.kind-heatmap.is-empty .wrap', list)).toBe(true);
    expect(stylesHeatmapCircle('.kind-heatmap .graph-heatmap-level', list)).toBe(true);
    expect(
      stylesHeatmapCircle(
        '.kind-heatmap .graph-heatmap-level',
        '.x, .kind-heatmap .graph-heatmap-level'
      )
    ).toBe(false);
  });

  it.each([
    ['.kind-heatmap:not(.is-empty) .graph-heatmap-circle', '.kind-heatmap .graph-heatmap-circle'],
    ['.a:not(:is(.b, .c)).is-empty:hover', '.a.is-empty:hover'],
    ['.a:not(:not(.b)):hover', '.a:hover'],
    ['.a:NOT(.b).c', '.a.c'],
    [':is(.a:not(.b)) .c', ':is(.a) .c'],
    ['.a:not(:is(.b) .c', '.a'],
    [
      '.kind-heatmap.is-empty.selected .graph-heatmap-circle',
      '.kind-heatmap.is-empty.selected .graph-heatmap-circle',
    ],
  ])('strips every :not from `%s`', (sel, expected) => {
    expect(withoutNot(sel)).toBe(expected);
  });

  it.each([
    ['.graph-generic-annotation-node.kind-heatmap:not(.is-empty) .graph-heatmap-circle', true],
    ['.kind-heatmap:not( .is-empty ):hover > .graph-heatmap-circle', true],
    ['.graph-generic-annotation-node:not(.is-empty).kind-heatmap .graph-heatmap-circle', true],
    ['.canvas:not(.is-empty) .kind-heatmap.is-empty .graph-heatmap-circle', false],
    ['.kind-heatmap.is-empty .graph-heatmap-circle:not(.is-empty)', false],
    ['.kind-heatmap .x:not(.is-empty) .graph-heatmap-circle', false],
    ['.kind-heatmap:not(:not(.is-empty)) .graph-heatmap-circle', false],
    ['.kind-heatmap:is(:not(.is-empty)) .graph-heatmap-circle', false],
    ['.kind-heatmap:not(.is-empty-x) .graph-heatmap-circle', false],
    ['.kind-heatmap-legend:not(.is-empty) .graph-heatmap-circle', false],
    ['.kind-heatmap .graph-heatmap-circle', false],
  ])('reads `%s` as keeping level-0 circles out: %s', (sel, expected) => {
    expect(excludesEmptyHeatmap(sel)).toBe(expected);
  });

  it('outlines only non-empty circles in every forced-colours heat-map rule', () => {
    const selectors = rules
      .filter(inForcedColours)
      .flatMap((r) => r.selectors)
      .filter(onHeatmapCircle);
    expect(selectors.length).toBeGreaterThan(0);
    for (const sel of selectors) {
      expect(sel).toContain(':not(.is-empty)');
      expect(sel.replace(':not(.is-empty)', '')).not.toContain('.is-empty');
    }
  });

  it('draws an empty circle only while it is selected or hovered', () => {
    const painting = rules
      .filter((r) => !inForcedColours(r) && paints(r.body))
      .flatMap((r) =>
        r.selectors.filter((sel) => stylesHeatmapCircle(sel, r.selectors.join(','), sheetIsFlat))
      );
    // The rim itself must exist, so the loop below checks something.
    expect(painting.some((sel) => withoutNot(sel).includes('.is-empty'))).toBe(true);
    for (const sel of painting) {
      if (excludesEmptyHeatmap(sel)) continue;
      const bare = withoutNot(sel);
      expect(bare.includes('.selected') || bare.includes(':hover')).toBe(true);
    }
  });

  it('keeps level-0 circles out of the forced-colours outline', () => {
    const block = css.slice(css.indexOf('@media (forced-colors: active)'));
    const rule = block.slice(0, block.indexOf('{', block.indexOf('{') + 1));
    expect(rule).toContain('.kind-heatmap:not(.is-empty) .graph-heatmap-circle');
  });

  it('shows the level-0 rim only while selected or hovered', () => {
    expect(css).toMatch(/\.kind-heatmap\.is-empty\.selected \.graph-heatmap-circle,/);
    expect(css).toMatch(/\.kind-heatmap\.is-empty:hover \.graph-heatmap-circle \{/);
    expect(css).not.toMatch(/\.kind-heatmap\.is-empty \{/);
  });
});
