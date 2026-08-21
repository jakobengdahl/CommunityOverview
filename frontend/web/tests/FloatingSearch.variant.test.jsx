import { describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import FloatingSearch from '../src/components/FloatingSearch';
import useGraphStore from '../src/store/graphStore';

describe('FloatingSearch variant prop', () => {
  beforeEach(() => {
    useGraphStore.setState({ nodes: [], hiddenNodeIds: [], federationDepth: 1, stats: {} });
  });

  it('defaults to the floating (fixed) chrome, unchanged from before the shell split', () => {
    const { container } = render(<FloatingSearch />);
    const bar = container.querySelector('.floating-search');
    expect(bar).not.toBeNull();
    expect(bar).not.toHaveClass('floating-search--sheet');
  });

  it('adds the sheet modifier class when mounted inside MobileShell’s BottomSheet', () => {
    const { container } = render(<FloatingSearch variant="sheet" />);
    const bar = container.querySelector('.floating-search');
    expect(bar).toHaveClass('floating-search--sheet');
  });
});
