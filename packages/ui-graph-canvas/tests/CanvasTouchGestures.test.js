import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Two reported touch/stylus bugs — a resize that lets go after a few pixels,
// and a pinch that does not zoom — had the same root cause: ReactFlow ships no
// `touch-action` declaration at all, so its pane, its nodes and its resize
// handles inherit `auto` and the BROWSER is free to reinterpret a drag as a
// pan or a two-finger gesture as a page zoom, firing `pointercancel` at
// whichever d3 behaviour was mid-gesture.
//
// The fixes are therefore CSS, and CSS is exactly what the rest of this suite
// cannot see: jsdom applies no layout and vitest resolves a CSS import to an
// empty module, so a component test would pass with the rules deleted. Reading
// the source is the only assertion available, and a weak assertion on the real
// cause is worth more here than a strong one on something else — without it,
// the next person to tidy this stylesheet reintroduces both bugs on devices no
// CI runner has.
function readStylesheet(file) {
  return readFileSync(join(process.cwd(), 'src/components', file), 'utf8');
}

// Rules are matched by (selector, declaration) rather than by exact text so
// reformatting the stylesheet does not fail the test.
function hasDeclaration(css, selector, declaration) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const blocks = css.matchAll(new RegExp(`${escaped}[^{}]*\\{([^{}]*)\\}`, 'g'));
  return [...blocks].some((m) => new RegExp(declaration).test(m[1]));
}

describe('canvas touch gestures depend on explicit touch-action rules', () => {
  const css = readStylesheet('GraphCanvas.css');

  it('gives resize handles touch-action: none so a stylus resize is not stolen mid-drag', () => {
    expect(hasDeclaration(css, '.react-flow__resize-control', 'touch-action:\\s*none')).toBe(true);
  });

  it('grows the resize hit area on a coarse pointer without changing the drawn handle', () => {
    // A 10px handle is a fine mouse target and a poor finger one; the enlarged
    // area is a transparent pseudo-element so the handle still looks the same.
    expect(css).toMatch(/@media \(pointer: coarse\)/);
    expect(hasDeclaration(css, '.react-flow__resize-control.handle::after', 'width:\\s*24px')).toBe(
      true
    );
  });

  it('gives the pane and nodes touch-action: none so pinch reaches d3-zoom', () => {
    expect(hasDeclaration(css, '.react-flow__pane', 'touch-action:\\s*none')).toBe(true);
    expect(hasDeclaration(css, '.react-flow__node', 'touch-action:\\s*none')).toBe(true);
  });

  it('exempts text fields, whose caret/selection/scroll must stay the browser’s', () => {
    // Taking these over would fix panning by breaking note editing.
    expect(hasDeclaration(css, '.react-flow__node textarea', 'touch-action:\\s*auto')).toBe(true);
  });
});
