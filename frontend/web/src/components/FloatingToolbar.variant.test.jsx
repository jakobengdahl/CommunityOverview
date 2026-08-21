import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/react';
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

describe('FloatingToolbar variant prop', () => {
  it('defaults to the floating (fixed) chrome, unchanged from before the shell split', () => {
    const { container } = render(<FloatingToolbar />);
    const toolbar = container.querySelector('.floating-toolbar');
    expect(toolbar).not.toBeNull();
    expect(toolbar).not.toHaveClass('floating-toolbar--sheet');
  });

  it('adds the sheet modifier class when mounted inside MobileShell’s BottomSheet', () => {
    const { container } = render(<FloatingToolbar variant="sheet" />);
    const toolbar = container.querySelector('.floating-toolbar');
    expect(toolbar).toHaveClass('floating-toolbar--sheet');
  });
});
