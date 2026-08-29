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
      schema: null,
    });
  });

  // Interim founder-directed constraint (2026-08-26, Corp task
  // cb61993e-1154-41cb-9acb-80aaa26991ed): the language switcher is hidden
  // and setLanguage is a no-op while LANGUAGE_SWITCHING_ENABLED is false in
  // src/i18n/index.jsx. The underlying sv.json / SUPPORTED_LANGUAGES /
  // useI18n() mechanism stays intact, so this test documents the interim
  // state rather than the removed capability.
  it('does not show language controls while switching is disabled', () => {
    render(
      <I18nProvider>
        <SettingsDialog
          stats={{ total_nodes: 1, total_edges: 2, nodes_by_type: { Actor: 1 } }}
          onClose={() => {}}
        />
      </I18nProvider>
    );

    expect(screen.queryByText('Language')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Svenska' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'English' })).not.toBeInTheDocument();
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

  it('shows relationship applicability rules in the metamodel details dialog', () => {
    useGraphStore.setState({
      schema: {
        relationship_types: {
          IMPLEMENTS: {
            description: 'Implements',
            source_types: ['Initiative'],
            target_types: ['Legislation'],
          },
        },
      },
    });

    render(
      <I18nProvider>
        <SettingsDialog
          stats={{
            total_nodes: 6,
            total_edges: 1,
            nodes_by_type: {
              Actor: 1,
              Initiative: 1,
              Capability: 1,
              Resource: 1,
              Legislation: 1,
              Goal: 1,
            },
          }}
          onClose={() => {}}
        />
      </I18nProvider>
    );

    fireEvent.click(screen.getByRole('button', { name: 'Details' }));

    expect(screen.getByText('Relationship types')).toBeInTheDocument();
    expect(screen.getByText('IMPLEMENTS')).toBeInTheDocument();
    expect(screen.getByText('Initiative -> Legislation')).toBeInTheDocument();
  });
});
