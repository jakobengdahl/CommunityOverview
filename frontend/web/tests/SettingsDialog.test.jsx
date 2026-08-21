import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import SettingsDialog from '../src/components/SettingsDialog';
import useGraphStore from '../src/store/graphStore';
import { I18nProvider } from '../src/i18n';

describe('SettingsDialog', () => {
  beforeEach(() => {
    window.localStorage.clear();
    useGraphStore.setState({
      showMinimap: false,
      setShowMinimap: useGraphStore.getState().setShowMinimap,
    });
  });

  it('shows GUI language controls and persists selection', () => {
    render(
      <I18nProvider>
        <SettingsDialog
          stats={{ total_nodes: 1, total_edges: 2, nodes_by_type: { Actor: 1 } }}
          onClose={() => {}}
        />
      </I18nProvider>
    );

    expect(screen.getByText('Language')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Svenska' }));

    expect(window.localStorage.getItem('app_language')).toBe('sv');
  });

  it('toggles the minimap setting', () => {
    render(
      <I18nProvider>
        <SettingsDialog stats={null} onClose={() => {}} />
      </I18nProvider>
    );

    fireEvent.click(screen.getByRole('button', { name: /Show minimap/i }));

    expect(useGraphStore.getState().showMinimap).toBe(true);
  });

  it('toggles the assistant panel setting and persists the explicit choice', () => {
    useGraphStore.setState({ chatPanelOpen: true });
    render(
      <I18nProvider>
        <SettingsDialog stats={null} onClose={() => {}} />
      </I18nProvider>
    );

    fireEvent.click(screen.getByRole('button', { name: /Assistant panel open/i }));

    expect(useGraphStore.getState().chatPanelOpen).toBe(false);
    expect(window.localStorage.getItem('chat_panel_open')).toBe('false');
  });

  it('resets the assistant panel to the application default', () => {
    useGraphStore.setState({
      chatPanelOpen: false,
      presentation: { default_chat_collapsed: false },
    });
    window.localStorage.setItem('chat_panel_open', 'false');
    render(
      <I18nProvider>
        <SettingsDialog stats={null} onClose={() => {}} />
      </I18nProvider>
    );

    fireEvent.click(screen.getByRole('button', { name: /Reset to default/i }));

    expect(useGraphStore.getState().chatPanelOpen).toBe(true);
    expect(window.localStorage.getItem('chat_panel_open')).toBeNull();
  });

  it('closes on Escape', () => {
    const onClose = vi.fn();
    render(
      <I18nProvider>
        <SettingsDialog stats={null} onClose={onClose} />
      </I18nProvider>
    );

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
