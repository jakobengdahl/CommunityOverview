import { describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/react';

import SettingsDialog from '../src/components/SettingsDialog';
import NodeTypeStatsDialog from '../src/components/NodeTypeStatsDialog';
import useGraphStore from '../src/store/graphStore';
import { I18nProvider } from '../src/i18n';

// Capability is covered by the legacy COLOR_MAP (#F97316); DataSet is not
// covered at all and rendered neutral gray in these dialogs before they were
// routed through resolveColor.
const SCHEMA = {
  node_types: {
    Capability: { color: '#F59E0B' },
    DataSet: { color: '#7C3AED' },
  },
};

describe('node type swatches resolve through resolveColor', () => {
  beforeEach(() => {
    window.localStorage.clear();
    useGraphStore.setState({ schema: SCHEMA });
  });

  it('colors the stats dialog dots from the schema', () => {
    const { container } = render(
      <NodeTypeStatsDialog nodesByType={{ Capability: 3, DataSet: 2 }} onClose={() => {}} />
    );

    const dots = container.querySelectorAll('.nts-dialog-dot');
    expect(dots).toHaveLength(2);
    expect(dots[0].style.backgroundColor).toBe('rgb(245, 158, 11)'); // schema, not COLOR_MAP #F97316
    expect(dots[1].style.backgroundColor).toBe('rgb(124, 58, 237)'); // was the neutral gray default
  });

  it('colors the settings dialog dots from the schema', () => {
    const { container } = render(
      <I18nProvider>
        <SettingsDialog
          stats={{ total_nodes: 5, total_edges: 0, nodes_by_type: { Capability: 3, DataSet: 2 } }}
          onClose={() => {}}
        />
      </I18nProvider>
    );

    const dots = container.querySelectorAll('.settings-dialog-type-dot');
    expect(dots).toHaveLength(2);
    expect(dots[0].style.backgroundColor).toBe('rgb(245, 158, 11)');
    expect(dots[1].style.backgroundColor).toBe('rgb(124, 58, 237)');
  });

  it('falls back to the legacy color when the schema leaves a covered type uncolored', () => {
    useGraphStore.setState({ schema: { node_types: {} } });

    const { container } = render(
      <NodeTypeStatsDialog nodesByType={{ Capability: 1 }} onClose={() => {}} />
    );

    const dot = container.querySelector('.nts-dialog-dot');
    expect(dot.style.backgroundColor).toBe('rgb(249, 115, 22)'); // COLOR_MAP.Capability #F97316
  });
});
