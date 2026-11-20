import { describe, it, expect } from 'vitest';
import { applyTheme } from './theme';

describe('applyTheme', () => {
  it('sets the known tokens, derives a hover for a lone accent and ignores other keys', () => {
    const root = document.createElement('div');
    applyTheme({ accent: '#0a7f5c', surface: '#000' }, root);

    expect(root.style.getPropertyValue('--co-accent')).toBe('#0a7f5c');
    expect(root.style.getPropertyValue('--co-accent-hover')).not.toBe('');
    expect(root.style.getPropertyValue('--co-surface')).toBe('');
  });

  it('clears an earlier override when the next theme no longer sets it', () => {
    const root = document.createElement('div');
    applyTheme({ accent: '#0a7f5c', accent_hover: '#086446' }, root);
    applyTheme({}, root);

    expect(root.style.getPropertyValue('--co-accent')).toBe('');
    expect(root.style.getPropertyValue('--co-accent-hover')).toBe('');
  });
});
