import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

// Fullscreen canvas mode hides every direct child of `.app` but the canvas and
// its exit button; a summoned fullscreen viewer is still moved, so the summon
// notice (a direct child of `.app`) must be exempt or Return is unreachable.
describe('fullscreen canvas mode', () => {
  it('keeps the summon notice visible', () => {
    const here = dirname(fileURLToPath(import.meta.url));
    const css = readFileSync(join(here, '../src/App.css'), 'utf8');
    const rule = css.match(/\.app\.fullscreen-canvas-mode\s*>\s*([^{]+)\{/);
    expect(rule).not.toBeNull();
    expect(rule[1]).toContain(':not(.app-summon-notice)');
  });
});
