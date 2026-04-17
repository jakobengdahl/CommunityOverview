import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import FloatingHeader from '../src/components/FloatingHeader';
import useGraphStore from '../src/store/graphStore';
import { I18nProvider } from '../src/i18n';

describe('FloatingHeader', () => {
  beforeEach(() => {
    window.localStorage.clear();
    useGraphStore.setState({
      showMinimap: false,
      setShowMinimap: useGraphStore.getState().setShowMinimap,
    });
  });

  it('shows GUI language controls in the hamburger menu and persists selection', () => {
    render(
      <I18nProvider>
        <FloatingHeader
          title="Test Graph"
          stats={{ total_nodes: 1, total_edges: 2, nodes_by_type: { Actor: 1 } }}
        />
      </I18nProvider>
    );

    fireEvent.click(screen.getByTitle('Menu'));

    expect(screen.getByText('Language')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Svenska' }));

    expect(window.localStorage.getItem('app_language')).toBe('sv');
  });
});
