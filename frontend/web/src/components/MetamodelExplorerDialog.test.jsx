/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import MetamodelExplorerDialog from './MetamodelExplorerDialog';

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: (key) => key, language: 'en' }),
}));

vi.mock('../store/graphStore', () => ({
  default: () => null,
}));

const SCHEMA = {
  node_types: {
    Actor: {
      category: 'domain',
      description: 'Government agencies and organizations',
      color: '#3B82F6',
      icon: 'PersonFill',
      fields: ['name', 'description'],
      labels: { sv: 'Aktör' },
    },
    Initiative: {
      category: 'domain',
      description: 'A funded piece of work',
      color: '#F59E0B',
      fields: ['name'],
    },
    Legislation: {
      category: 'domain',
      description: 'A law or regulation',
      color: '#10B981',
      fields: ['name'],
    },
    Agent: {
      category: 'system',
      description: 'AI agent configuration',
      color: '#EC4899',
      fields: ['name'],
    },
    Skill: {
      category: 'system',
      description: 'Reusable AI skill definition',
      color: '#8B5CF6',
      fields: ['name'],
    },
  },
  relationship_types: {
    IMPLEMENTS: {
      description: 'Initiative implements legislation',
      source_types: ['Initiative'],
      target_types: ['Legislation'],
    },
    RELATES_TO: {
      description: 'General connection',
      source_types: [],
      target_types: [],
    },
    USES_SKILL: {
      description: 'Agent uses a skill',
      source_types: ['Agent'],
      target_types: ['Skill'],
    },
  },
};

const STATS = {
  total_nodes: 8,
  total_edges: 3,
  nodes_by_type: { Actor: 5, Initiative: 2, Legislation: 1 },
  edges_by_type: { IMPLEMENTS: 2 },
};

describe('MetamodelExplorerDialog', () => {
  it('renders only domain node types in the network view by default', () => {
    render(<MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />);

    // Domain types are rendered as SVG nodes (accessible via their aria-label).
    expect(screen.getByRole('button', { name: /^Actor, 5$/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Initiative, 2$/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Legislation, 1$/ })).toBeInTheDocument();

    // System types are hidden until the toggle is switched on.
    expect(screen.queryByRole('button', { name: /^Agent/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Skill/ })).not.toBeInTheDocument();
  });

  it('reveals system types once the toggle is checked', () => {
    render(<MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />);

    fireEvent.click(screen.getByLabelText('metamodel.show_system_types'));

    expect(screen.getByRole('button', { name: /^Agent$/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Skill$/ })).toBeInTheDocument();
  });

  it('draws an edge only for the configured relationship, not for every visible pair', () => {
    const { container } = render(
      <MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />
    );

    // Only IMPLEMENTS (Initiative -> Legislation) is drawn as an SVG edge:
    // RELATES_TO has no configured applicability and USES_SKILL's endpoints
    // (Agent, Skill) are hidden system types by default.
    const edgeLabels = Array.from(container.querySelectorAll('.mme-edge-label')).map(
      (el) => el.textContent
    );
    expect(edgeLabels).toEqual(['IMPLEMENTS']);
  });

  it('lists fully unconstrained relationship types separately instead of inventing edges', () => {
    const { container } = render(
      <MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />
    );

    expect(screen.getByText('metamodel.unconstrained_title')).toBeInTheDocument();
    const unconstrainedSection = container.querySelector('.mme-unconstrained');
    expect(within(unconstrainedSection).getByText('RELATES_TO')).toBeInTheDocument();
  });

  it('shows node type detail — description, count and fields — after selecting it', () => {
    render(<MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />);

    fireEvent.click(screen.getByRole('button', { name: /^Actor, 5$/ }));

    expect(screen.getByText('Government agencies and organizations')).toBeInTheDocument();
    expect(screen.getByText('name, description')).toBeInTheDocument();
  });

  it('filters node types by name in both views', () => {
    render(<MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />);

    fireEvent.change(screen.getByLabelText('metamodel.filter_aria_label'), {
      target: { value: 'legisl' },
    });

    expect(screen.getByRole('button', { name: /^Legislation, 1$/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Actor/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Initiative/ })).not.toBeInTheDocument();
  });

  it('renders an accessible table fallback reflecting the same effective schema', () => {
    render(<MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />);

    fireEvent.click(screen.getByRole('tab', { name: /metamodel.view_table/ }));

    const tables = screen.getAllByRole('table');
    expect(tables).toHaveLength(2);

    const nodeTable = tables[0];
    expect(within(nodeTable).getByText('Actor')).toBeInTheDocument();
    expect(within(nodeTable).getByText('5')).toBeInTheDocument();
    // The "Label (sv)" column always shows the schema's sv translation,
    // regardless of the current UI language (the mocked language is 'en',
    // and the schema never carries an 'en' entry in `labels`).
    expect(within(nodeTable).getByText('Aktör')).toBeInTheDocument();
    // System types stay hidden in the table too, honouring the same toggle.
    expect(within(nodeTable).queryByText('Agent')).not.toBeInTheDocument();

    const relTable = tables[1];
    expect(within(relTable).getByText('IMPLEMENTS')).toBeInTheDocument();
    // Unconfigured applicability renders as "any type", never an invented rule.
    const relatesRow = within(relTable).getByText('RELATES_TO').closest('tr');
    expect(within(relatesRow).getAllByText('metamodel.any_type')).toHaveLength(2);
    // USES_SKILL is bound entirely to system types (Agent, Skill), which are
    // hidden by default — it must not reference types the table doesn't show.
    expect(within(relTable).queryByText('USES_SKILL')).not.toBeInTheDocument();
  });

  it('reveals a relationship type in the table once its system-type endpoints are shown', () => {
    render(<MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />);

    fireEvent.click(screen.getByRole('tab', { name: /metamodel.view_table/ }));
    expect(screen.queryByText('USES_SKILL')).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('metamodel.show_system_types'));

    expect(screen.getByText('USES_SKILL')).toBeInTheDocument();
  });

  it('shows an unknown-count placeholder rather than fabricating a zero when stats are absent', () => {
    render(<MetamodelExplorerDialog schema={SCHEMA} stats={undefined} onClose={() => {}} />);

    fireEvent.click(screen.getByRole('tab', { name: /metamodel.view_table/ }));
    const nodeTable = screen.getAllByRole('table')[0];
    const actorRow = within(nodeTable).getByText('Actor').closest('tr');
    expect(within(actorRow).getByText('metamodel.count_unknown')).toBeInTheDocument();
  });

  it('reflects the effective schema passed in rather than any hardcoded type list', () => {
    const customSchema = {
      node_types: {
        Widget: { category: 'domain', description: 'A custom widget type', fields: [] },
      },
      relationship_types: {},
    };

    render(<MetamodelExplorerDialog schema={customSchema} stats={{}} onClose={() => {}} />);

    expect(screen.getByRole('button', { name: /^Widget$/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Actor/ })).not.toBeInTheDocument();
  });

  it('never renders editing affordances — this view is read-only by design', () => {
    render(<MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />);

    expect(screen.getByText('metamodel.readonly_note')).toBeInTheDocument();
    const buttons = screen.getAllByRole('button').map((b) => b.textContent.toLowerCase());
    expect(buttons.some((text) => text.includes('save'))).toBe(false);
    expect(buttons.some((text) => text.includes('edit'))).toBe(false);
    expect(buttons.some((text) => text.includes('delete'))).toBe(false);
  });

  it('calls onClose when Escape is pressed', () => {
    const onClose = vi.fn();
    render(<MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={onClose} />);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onClose).toHaveBeenCalled();
  });

  it('exposes zoom and reset controls for pan/zoom without requiring mouse gestures', () => {
    render(<MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />);

    expect(screen.getByRole('button', { name: 'metamodel.zoom_in' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'metamodel.zoom_out' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'metamodel.reset_view' })).toBeInTheDocument();
  });

  it('zooms in place around the diagram center rather than drifting from the origin', () => {
    const { container } = render(
      <MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />
    );
    const group = container.querySelector('.mme-svg > g');
    expect(group.getAttribute('transform')).toBe('translate(0,0) scale(1)');

    fireEvent.click(screen.getByRole('button', { name: 'metamodel.zoom_in' }));

    // At k=1.2 with no pan, the anchor point (300, 260) must stay fixed:
    // translate = CENTER * (1 - k) = 300*(1-1.2) = -60, 260*(1-1.2) = -52.
    const match = group
      .getAttribute('transform')
      .match(/^translate\(([-.\d]+),([-.\d]+)\) scale\(([-.\d]+)\)$/);
    expect(match).not.toBeNull();
    const [, tx, ty, k] = match.map(Number);
    expect(tx).toBeCloseTo(-60);
    expect(ty).toBeCloseTo(-52);
    expect(k).toBeCloseTo(1.2);
  });

  it('resets zoom and pan together', () => {
    const { container } = render(
      <MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />
    );
    const group = container.querySelector('.mme-svg > g');

    fireEvent.click(screen.getByRole('button', { name: 'metamodel.zoom_in' }));
    fireEvent.click(screen.getByRole('button', { name: 'metamodel.reset_view' }));

    expect(group.getAttribute('transform')).toBe('translate(0,0) scale(1)');
  });
});
