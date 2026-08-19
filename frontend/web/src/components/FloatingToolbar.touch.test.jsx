/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FloatingToolbar from './FloatingToolbar';

vi.mock('../store/graphStore', () => ({
  default: (selector) =>
    selector({
      schema: {
        node_types: {
          Actor: { category: 'domain', icon: 'PersonFill', color: '#3B82F6' },
        },
      },
    }),
}));

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: (key) => key }),
}));

// A minimal fake MediaQueryList — mirrors the one in useViewportMode.test.jsx
// so this file exercises the real hook rather than mocking it away.
function makeMql(matches) {
  return {
    matches,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
}

function setPointer({ coarse }) {
  window.matchMedia = vi.fn((query) => makeMql(query === '(pointer: coarse)' ? coarse : false));
}

describe('FloatingToolbar touch behavior', () => {
  let originalMatchMedia;

  beforeEach(() => {
    originalMatchMedia = window.matchMedia;
  });

  afterEach(() => {
    window.matchMedia = originalMatchMedia;
  });

  it('creates the node directly on tap when the pointer is coarse, with no drag step', async () => {
    setPointer({ coarse: true });
    const user = userEvent.setup();
    const onCreateNode = vi.fn();
    render(<FloatingToolbar onCreateNode={onCreateNode} />);

    const button = screen.getByRole('button', { name: 'Actor' });
    // Native HTML5 drag never fires on a touch pointer, so the item must not
    // rely on it: draggable is off, and a plain tap (click) creates directly.
    expect(button).toHaveAttribute('draggable', 'false');

    await user.click(button);
    expect(onCreateNode).toHaveBeenCalledWith('Actor');
    expect(onCreateNode).toHaveBeenCalledTimes(1);
  });

  it('shows the type label alongside the icon when hover is unavailable (touch)', () => {
    setPointer({ coarse: true });
    render(<FloatingToolbar onCreateNode={vi.fn()} />);

    const button = screen.getByRole('button', { name: 'Actor' });
    expect(button).toHaveTextContent('Actor');
    expect(button.querySelector('.floating-toolbar-item-label')).not.toBeNull();
  });

  it('keeps the desktop drag-and-drop path unchanged for a fine pointer', async () => {
    setPointer({ coarse: false });
    const user = userEvent.setup();
    const onCreateNode = vi.fn();
    render(<FloatingToolbar onCreateNode={onCreateNode} />);

    const button = screen.getByRole('button', { name: 'Actor' });
    expect(button).toHaveAttribute('draggable', 'true');

    // Click-to-create keeps working on desktop too — the two paths coexist.
    await user.click(button);
    expect(onCreateNode).toHaveBeenCalledWith('Actor');

    // The label element is always in the DOM (CSS hides it on hover-capable
    // devices); the hover-only portal tooltip remains the desktop affordance.
    expect(button.querySelector('.floating-toolbar-item-label')).not.toBeNull();
  });
});
