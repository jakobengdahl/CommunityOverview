import { describe, it, expect } from 'vitest';
import { decideClearAction } from './clearBoard';

describe('decideClearAction', () => {
  // Unnamed, unlocked scratch board: clears immediately from either source.
  it('clears immediately on an unnamed, unlocked board (keyboard)', () => {
    expect(decideClearAction({ locked: false, named: false, source: 'keyboard' })).toBe('clear');
  });

  it('clears immediately on an unnamed, unlocked board (button)', () => {
    expect(decideClearAction({ locked: false, named: false, source: 'button' })).toBe('clear');
  });

  // Named board: confirm from either source.
  it('confirms before clearing a named board via esc-esc', () => {
    expect(decideClearAction({ locked: false, named: true, source: 'keyboard' })).toBe('confirm');
  });

  it('confirms before clearing a named board via the clear button', () => {
    expect(decideClearAction({ locked: false, named: true, source: 'button' })).toBe('confirm');
  });

  // Locked board: esc-esc does nothing at all; the button shows the strong warning.
  it('does nothing when esc-esc is pressed on a locked board', () => {
    expect(decideClearAction({ locked: true, named: false, source: 'keyboard' })).toBe('noop');
  });

  it('shows the strong locked confirmation when the clear button is used on a locked board', () => {
    expect(decideClearAction({ locked: true, named: false, source: 'button' })).toBe(
      'confirm-locked'
    );
  });

  // Lock takes precedence over name: a locked *and* named board still ignores
  // esc-esc and shows the emphatic locked warning from the button.
  it('lets the lock override the name for esc-esc', () => {
    expect(decideClearAction({ locked: true, named: true, source: 'keyboard' })).toBe('noop');
  });

  it('lets the lock override the name for the clear button', () => {
    expect(decideClearAction({ locked: true, named: true, source: 'button' })).toBe(
      'confirm-locked'
    );
  });
});
