// covers: frontend/web/src/utils/theme.js
import { describe, it, expect } from 'vitest';
import { applyTheme } from './theme';

describe('applyTheme', () => {
  it('derives a hover only for an accent without its own, and ignores unknown keys', () => {
    const root = document.createElement('div');
    applyTheme({ accent: '#0a7f5c', surface: '#000' }, root);

    expect(root.style.getPropertyValue('--co-accent')).toBe('#0a7f5c');
    expect(root.style.getPropertyValue('--co-accent-hover')).toBe(
      'color-mix(in srgb, #0a7f5c 85%, black)'
    );
    expect(root.style.getPropertyValue('--co-surface')).toBe('');

    applyTheme({ accent: '#0a7f5c', accent_hover: '#086446', accent_contrast: '#000' }, root);
    expect(root.style.getPropertyValue('--co-accent-hover')).toBe('#086446');
    expect(root.style.getPropertyValue('--co-accent-contrast')).toBe('#000');
  });

  it('clears an earlier override when the next theme no longer sets it', () => {
    const root = document.createElement('div');
    applyTheme({ accent: '#0a7f5c', accent_hover: '#086446', accent_contrast: '#000' }, root);
    applyTheme({}, root);

    expect(root.style.getPropertyValue('--co-accent')).toBe('');
    expect(root.style.getPropertyValue('--co-accent-hover')).toBe('');
    expect(root.style.getPropertyValue('--co-accent-contrast')).toBe('');
  });
});
