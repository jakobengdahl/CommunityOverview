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
