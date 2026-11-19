// covers: frontend/web/src/components/MetamodelExplorerDialog.jsx, frontend/web/src/components/NodeAppearanceDialog.jsx
/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { URL as NodeURL } from 'node:url';
import { render, screen, fireEvent, within, act } from '@testing-library/react';
import MetamodelExplorerDialog from './MetamodelExplorerDialog';
import NodeAppearanceDialog, {
  appearanceOperation,
  currentAppearance,
} from './NodeAppearanceDialog';
import { nodeShapeGeometry, nodeShapeTextBox } from '@community-graph/ui-graph-canvas';

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: (key) => key, language: 'en' }),
}));

vi.mock('../store/graphStore', () => ({
  default: () => null,
}));

// This jsdom doesn't implement the PointerEvent constructor, so
// fireEvent.pointerDown/pointerMove deliver clientX/clientY/pointerId as
// undefined here (verified directly against this environment) even though
// every real target browser supports PointerEvent fully. A MouseEvent
// constructed with a pointer* type carries the same clientX/clientY (Pointer
// events extend MouseEvent) and lets pointerId be attached explicitly, so
// pan-by-drag can still be exercised under this test environment.
function pointerLikeEvent(type, init) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, ...init });
  Object.defineProperty(event, 'pointerId', { value: init.pointerId, configurable: true });
  return event;
}

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
    ASSIGNED_TO: {
      description: 'Agent assigned to an initiative',
      source_types: ['Agent'],
      target_types: ['Initiative'],
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
    // RELATES_TO has no configured applicability; USES_SKILL's endpoints
    // (Agent, Skill) are both hidden system types by default; and
    // ASSIGNED_TO has one endpoint hidden (Agent) and one visible
    // (Initiative), so it has no drawable pair either.
    const edgeLabels = Array.from(container.querySelectorAll('.mme-edge-label')).map(
      (el) => el.textContent
    );
    expect(edgeLabels).toEqual(['IMPLEMENTS']);
  });

  it('hides a relationship type with only one endpoint hidden, in both network and table', () => {
    render(<MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />);

    // ASSIGNED_TO (Agent -> Initiative): Agent is a hidden system type,
    // Initiative is visible — a relationship with a hidden endpoint on
    // either side has no pair it could actually draw.
    expect(screen.queryByText('ASSIGNED_TO')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: /metamodel.view_table/ }));
    expect(screen.queryByText('ASSIGNED_TO')).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('metamodel.show_system_types'));
    expect(screen.getByText('ASSIGNED_TO')).toBeInTheDocument();
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

  it('pans the diagram on pointer drag (works for touch as well as mouse)', () => {
    const { container } = render(
      <MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />
    );
    const svg = container.querySelector('.mme-svg');
    const group = container.querySelector('.mme-svg > g');

    fireEvent(svg, pointerLikeEvent('pointerdown', { clientX: 100, clientY: 100, pointerId: 1 }));
    fireEvent(svg, pointerLikeEvent('pointermove', { clientX: 140, clientY: 130, pointerId: 1 }));

    // jsdom reports a zero-size bounding rect, so the CSS-pixel-to-viewBox
    // ratio falls back to 1:1 here — a 40/30px drag becomes a 40/30 unit pan.
    expect(group.getAttribute('transform')).toBe('translate(40,30) scale(1)');

    fireEvent(svg, pointerLikeEvent('pointerup', { clientX: 140, clientY: 130, pointerId: 1 }));
    fireEvent(svg, pointerLikeEvent('pointermove', { clientX: 999, clientY: 999, pointerId: 1 }));

    // Movement after pointer-up must not keep panning.
    expect(group.getAttribute('transform')).toBe('translate(40,30) scale(1)');
  });

  it('ignores a second pointer touching down mid-drag instead of hijacking the pan', () => {
    const { container } = render(
      <MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />
    );
    const svg = container.querySelector('.mme-svg');
    const group = container.querySelector('.mme-svg > g');

    // Finger A starts a drag.
    fireEvent(svg, pointerLikeEvent('pointerdown', { clientX: 100, clientY: 100, pointerId: 1 }));
    // Finger B touches down mid-drag (a resting finger, or a pinch attempt) —
    // must not reset the in-progress drag's origin.
    fireEvent(svg, pointerLikeEvent('pointerdown', { clientX: 200, clientY: 200, pointerId: 2 }));
    // Finger B moving must not pan either — it was never accepted as a drag.
    fireEvent(svg, pointerLikeEvent('pointermove', { clientX: 500, clientY: 500, pointerId: 2 }));
    expect(group.getAttribute('transform')).toBe('translate(0,0) scale(1)');

    // Finger A continuing its drag still pans correctly, from its own origin.
    fireEvent(svg, pointerLikeEvent('pointermove', { clientX: 140, clientY: 130, pointerId: 1 }));
    expect(group.getAttribute('transform')).toBe('translate(40,30) scale(1)');

    // Finger B lifting must not end finger A's still-active drag.
    fireEvent(svg, pointerLikeEvent('pointerup', { clientX: 500, clientY: 500, pointerId: 2 }));
    fireEvent(svg, pointerLikeEvent('pointermove', { clientX: 150, clientY: 140, pointerId: 1 }));
    expect(group.getAttribute('transform')).toBe('translate(50,40) scale(1)');
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

  it('does not capture the pointer on pointerdown, so a plain click still selects a node type', () => {
    const { container } = render(
      <MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />
    );
    const svg = container.querySelector('.mme-svg');
    const capture = vi.fn();
    svg.setPointerCapture = capture;
    const node = screen.getByRole('button', { name: /^Actor/ });

    // Capturing on pointerdown retargets the click to the <svg> in real browsers.
    node.dispatchEvent(pointerLikeEvent('pointerdown', { pointerId: 1, clientX: 10, clientY: 10 }));
    expect(capture).not.toHaveBeenCalled();
    fireEvent.click(node);
    node.dispatchEvent(pointerLikeEvent('pointerup', { pointerId: 1 }));

    expect(
      within(screen.getByLabelText('metamodel.detail_panel_aria')).getByText('Actor')
    ).not.toBeNull();
  });

  it('captures the pointer once a drag moves past the threshold', () => {
    const { container } = render(
      <MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />
    );
    const svg = container.querySelector('.mme-svg');
    const capture = vi.fn();
    svg.setPointerCapture = capture;

    svg.dispatchEvent(pointerLikeEvent('pointerdown', { pointerId: 1, clientX: 10, clientY: 10 }));
    act(() => {
      svg.dispatchEvent(
        pointerLikeEvent('pointermove', { pointerId: 1, clientX: 40, clientY: 10 })
      );
    });

    expect(capture).toHaveBeenCalledWith(1);
  });

  it('does not capture the pointer for sub-threshold jitter', () => {
    const { container } = render(
      <MetamodelExplorerDialog schema={SCHEMA} stats={STATS} onClose={() => {}} />
    );
    const svg = container.querySelector('.mme-svg');
    const capture = vi.fn();
    svg.setPointerCapture = capture;

    svg.dispatchEvent(pointerLikeEvent('pointerdown', { pointerId: 1, clientX: 10, clientY: 10 }));
    act(() => {
      svg.dispatchEvent(
        pointerLikeEvent('pointermove', { pointerId: 1, clientX: 12, clientY: 11 })
      );
    });

    expect(capture).not.toHaveBeenCalled();
  });
});

describe('NodeAppearanceDialog', () => {
  const EXAMPLE_SHAPE = {
    version: 1,
    base: 'rectangle-flat',
    scale: 2,
    placement: 'middle-centre',
    areas: { first: 'type', second: 'name', third: null },
  };
  const DEFAULT_SHAPE = {
    ...EXAMPLE_SHAPE,
    base: 'rectangle',
    scale: 1,
    placement: 'top-left',
    areas: { first: 'type', second: 'name', third: 'summary' },
  };
  const CONFIG_SCHEMA = {
    ...SCHEMA,
    node_types: { ...SCHEMA.node_types, Actor: { ...SCHEMA.node_types.Actor, shape: 'example' } },
    node_shapes: { example: EXAMPLE_SHAPE },
  };
  const node = { id: 'a', name: 'First', type: 'Actor' };
  const props = {
    node,
    schema: SCHEMA,
    color: '#FBBF24',
    shapeOverrides: { b: { builtin: true } },
    typeShapes: {},
    nodes: [node, { id: 'b', name: 'Second', type: 'Actor' }],
    onClose: vi.fn(),
    onApply: vi.fn(),
  };
  it('applies an unchanged built-in appearance and turns edits into an inline shape', () => {
    const apply = vi.fn();
    const view = render(<NodeAppearanceDialog {...props} onApply={apply} />);
    expect(document.querySelector('.graph-custom-node')).toBeTruthy();
    expect(document.querySelector('.graph-shaped-node')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'node_appearance.apply' }));
    expect(apply).toHaveBeenLastCalledWith('node', { builtin: true });
    fireEvent.change(screen.getByLabelText('node_appearance.scale'), { target: { value: '2' } });
    fireEvent.click(screen.getByRole('button', { name: 'node_appearance.apply' }));
    expect(apply.mock.calls[1][1].inline.scale).toBe(2);
    expect(document.querySelector('.graph-custom-node')).toBeNull();
    // The canvas renderer draws the band in the type colour, with band text
    // that stays readable on a light colour.
    const chip = () => document.querySelector('.graph-shape-label-chip');
    expect(chip().textContent).toBe('Actor');
    expect(chip().style.color).toBe('rgb(0, 0, 0)');
    expect(document.querySelector('.graph-shape-band').style.fill).toBe('#FBBF24');
    fireEvent.change(screen.getByLabelText('node_appearance.base'), {
      target: { value: 'triangle-up' },
    });
    expect(chip().textContent).toBe('Actor');
    view.unmount();
    render(
      <NodeAppearanceDialog {...props} shapeOverrides={{ a: { builtin: true } }} onApply={apply} />
    );
    fireEvent.click(screen.getByRole('button', { name: 'node_appearance.apply' }));
    expect(apply).toHaveBeenLastCalledWith('node', { builtin: true });
  });
  it('shows retained overrides on every tab when applying to a type', () => {
    render(<NodeAppearanceDialog {...props} />);
    fireEvent.click(screen.getByRole('tab', { name: 'node_appearance.apply' }));
    fireEvent.click(screen.getByLabelText('node_appearance.scope_type'));
    for (const tab of ['shape', 'text', 'apply']) {
      fireEvent.click(screen.getByRole('tab', { name: `node_appearance.${tab}` }));
      expect(screen.getByText(/Second/)).toBeTruthy();
      expect(screen.getByRole('button', { name: 'node_appearance.apply' }).disabled).toBe(false);
    }
  });
  it('keeps a draft isolated and restores focus on Escape', () => {
    const opener = document.createElement('button');
    document.body.append(opener);
    opener.focus();
    const view = render(<NodeAppearanceDialog {...props} />);
    fireEvent.change(screen.getByLabelText('node_appearance.base'), {
      target: { value: 'triangle-up' },
    });
    expect(props.onApply).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(props.onClose).toHaveBeenCalled();
    view.unmount();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });
  it.each(['light', 'dark'])(
    'draws the %s preview stage on that theme canvas background in either page scheme',
    (theme) => {
      // Parse the canvas stylesheet: jsdom does not evaluate media queries.
      const style = document.createElement('style');
      style.textContent = readFileSync(
        new NodeURL(
          '../../../../packages/ui-graph-canvas/src/components/GraphCanvas.css',
          import.meta.url
        ),
        'utf8'
      );
      document.head.appendChild(style);
      const backgroundFor = (element, pageLight) => {
        let background;
        let specificity = -1;
        const visit = (rules) => {
          for (const rule of rules) {
            if (rule.media) {
              if (pageLight && rule.conditionText === '(prefers-color-scheme: light)') {
                visit(rule.cssRules);
              }
              continue;
            }
            const value = rule.style?.getPropertyValue('background');
            if (!value || !element.matches(rule.selectorText)) continue;
            const weight = (rule.selectorText.match(/[.:][\w-]+/g) || []).length;
            if (weight >= specificity) {
              specificity = weight;
              background = value;
            }
          }
        };
        visit(style.sheet.cssRules);
        return background;
      };
      try {
        render(<NodeAppearanceDialog {...props} />);
        fireEvent.change(screen.getByLabelText('node_appearance.theme'), {
          target: { value: theme },
        });
        const stage = document.querySelector('.appearance-preview-stage');
        const canvas = document.createElement('div');
        canvas.className = 'graph-canvas-container';
        const expected = backgroundFor(canvas, theme === 'light');
        expect(backgroundFor(stage, true)).toBe(expected);
        expect(backgroundFor(stage, false)).toBe(expected);
      } finally {
        style.remove();
      }
    }
  );
  it('validates scale and applies a type draft while preserving bindings', () => {
    const apply = vi.fn();
    render(<NodeAppearanceDialog {...props} onApply={apply} />);
    fireEvent.change(screen.getByLabelText('node_appearance.scale'), { target: { value: '11' } });
    expect(screen.getByRole('button', { name: 'node_appearance.apply' }).disabled).toBe(true);
    const statuses = screen.getAllByRole('status');
    expect(statuses.every((status) => !status.textContent)).toBe(true);
    // A placement reset is announced even while the scale is invalid.
    fireEvent.change(screen.getByLabelText('node_appearance.base'), {
      target: { value: 'triangle-up' },
    });
    expect(statuses.map((status) => status.textContent)).toContain(
      'node_appearance.placement_reset'
    );
    fireEvent.change(screen.getByLabelText('node_appearance.scale'), { target: { value: '2' } });
    expect(statuses.map((status) => status.textContent)).toContain(
      'node_appearance.preview_updated'
    );
    fireEvent.click(screen.getByRole('tab', { name: 'node_appearance.apply' }));
    fireEvent.click(screen.getByLabelText('node_appearance.scope_type'));
    expect(screen.getByText(/Second/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'node_appearance.apply' }));
    expect(apply).toHaveBeenCalledWith('type', {
      inline: {
        version: 1,
        base: 'triangle-up',
        scale: 2,
        placement: 'centre',
        areas: { first: 'type', second: 'name', third: 'summary' },
      },
    });
  });
  it('supports keyboard tabs and traps focus inside the modal', () => {
    const canvas = document.createElement('div');
    document.body.append(canvas);
    const shortcut = vi.fn();
    document.addEventListener('keydown', shortcut, true);
    const view = render(<NodeAppearanceDialog {...props} />);
    expect(canvas.hasAttribute('inert')).toBe(true);
    const first = screen.getByRole('tab', { name: 'node_appearance.shape' });
    first.focus();
    fireEvent.keyDown(first, { key: 'End' });
    expect(
      screen.getByRole('tab', { name: 'node_appearance.apply' }).getAttribute('aria-selected')
    ).toBe('true');
    fireEvent.keyDown(document.activeElement, { key: 'Home' });
    expect(document.activeElement).toBe(first);
    const last = screen.getByRole('button', { name: 'node_appearance.apply' });
    last.focus();
    fireEvent.keyDown(last, { key: 'Tab' });
    expect(screen.getByRole('dialog').contains(document.activeElement)).toBe(true);
    expect(document.activeElement).not.toBe(last);
    fireEvent.keyDown(last, { key: 'Delete' });
    expect(shortcut).not.toHaveBeenCalled();
    view.unmount();
    expect(canvas.hasAttribute('inert')).toBe(false);
    document.removeEventListener('keydown', shortcut, true);
    canvas.remove();
  });
  it('applies an explicitly selected configuration reference to the node', () => {
    const apply = vi.fn();
    const definition = {
      version: 1,
      base: 'rectangle',
      scale: 1,
      placement: 'top-left',
      areas: { first: 'type', second: 'name', third: 'missing' },
    };
    render(
      <NodeAppearanceDialog
        {...props}
        schema={{ ...SCHEMA, node_shapes: { example: definition } }}
        onApply={apply}
      />
    );
    fireEvent.change(screen.getByLabelText('node_appearance.config_shape'), {
      target: { value: 'example' },
    });
    fireEvent.click(screen.getByRole('tab', { name: 'node_appearance.text' }));
    // An unavailable binding is named and warned about, never shown as None.
    const binding = screen.getByLabelText('node_appearance.area_third');
    expect(binding.value).toBe('missing');
    expect(binding.selectedOptions[0].textContent).toContain('node_appearance.unavailable');
    expect(document.getElementById(binding.getAttribute('aria-describedby'))).toBeTruthy();
    expect(
      [...screen.getByLabelText('node_appearance.area_second').options].find(
        (option) => option.value === 'name'
      ).textContent
    ).toBe('node_appearance.field_name');
    const textBox = screen.getByText('node_appearance.unavailable').parentElement;
    const box = nodeShapeTextBox(nodeShapeGeometry('rectangle', 1), 'top-left');
    expect(textBox.style.left).toBe(`${box.x}px`);
    expect(textBox.style.top).toBe(`${box.y}px`);
    expect(textBox.style.width).toBe(`${box.width}px`);
    expect(textBox.style.height).toBe(`${box.height}px`);
    fireEvent.click(screen.getByRole('button', { name: 'node_appearance.apply' }));
    expect(apply).toHaveBeenCalledWith('node', { ref: 'config:example' });
  });
  it.each([
    ['a built-in node override', { shapeOverrides: { a: { builtin: true } } }, 'node'],
    ['a configuration default', { schema: CONFIG_SCHEMA }, 'config'],
    ['a type default', { typeShapes: { Actor: { inline: EXAMPLE_SHAPE } } }, 'type'],
    ['an unresolvable reference', { shapeOverrides: { a: { ref: 'saved:gone' } } }, 'fallback'],
  ])('applies %s unchanged as the original value and reports its source', (_, extra, source) => {
    const apply = vi.fn();
    const merged = { ...props, ...extra };
    const original = currentAppearance(
      node,
      merged.schema,
      merged.shapeOverrides,
      merged.typeShapes
    );
    expect(original.source).toBe(source);
    render(<NodeAppearanceDialog {...merged} onApply={apply} />);
    expect(screen.getByText(new RegExp(`node_appearance.source_${source}`))).toBeTruthy();
    // A fallback is drawn as the built-in card it reports.
    expect(Boolean(document.querySelector('.graph-custom-node'))).toBe(
      source === 'fallback' || source === 'node'
    );
    fireEvent.click(screen.getByRole('button', { name: 'node_appearance.apply' }));
    // A base edit and its reversal are no edit at all.
    const base = screen.getByLabelText('node_appearance.base');
    const start = base.value;
    fireEvent.change(base, { target: { value: 'triangle-down' } });
    fireEvent.change(base, { target: { value: start } });
    fireEvent.click(screen.getByRole('button', { name: 'node_appearance.apply' }));
    const value = source === 'config' ? { ref: 'config:example' } : original.selection;
    expect(apply.mock.calls[0][1]).toEqual(value);
    expect(apply.mock.calls[1][1]).toEqual(
      source === 'node' || source === 'fallback' ? { inline: DEFAULT_SHAPE } : value
    );
  });
  it('constructs one revision-checked operation for each scope', () => {
    const selection = { builtin: true };
    const revisions = { 'node:a': 2, 'type:Actor': 3 };
    expect(appearanceOperation('node', node, selection, revisions, 'id')).toEqual({
      op: 'node_shape_set',
      version: 1,
      operation_id: 'id',
      node_id: 'a',
      selection,
      base_revision: 2,
    });
    expect(appearanceOperation('type', node, selection, revisions, 'id')).toEqual({
      op: 'type_shape_set',
      version: 1,
      operation_id: 'id',
      type: 'Actor',
      selection,
      base_revision: 3,
      clear_node_id: 'a',
      clear_base_revision: 2,
    });
  });
  const click = (name) => act(async () => fireEvent.click(screen.getByRole('button', { name })));
  it('saves a named shape and copies apart from Apply, and a failed Apply keeps both for retry', async () => {
    const apply = vi.fn();
    const create = vi.fn(async (key, name, definition) => ({
      key,
      name,
      definition,
      revision: create.mock.calls.length,
      deleted: false,
    }));
    const view = render(
      <NodeAppearanceDialog {...props} savedShapes={{}} library={{ create }} onApply={apply} />
    );
    fireEvent.click(screen.getByRole('tab', { name: 'node_appearance.apply' }));
    fireEvent.change(screen.getByLabelText('node_appearance.shape_name'), {
      target: { value: 'My card!' },
    });
    await click('node_appearance.save_named');
    const [key, name, definition] = create.mock.calls[0];
    expect(key).toMatch(/^my-card-[0-9a-f]{8}$/);
    expect([name, definition]).toEqual(['My card!', DEFAULT_SHAPE]);
    expect(apply).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'node_appearance.apply' }));
    view.rerender(
      <NodeAppearanceDialog
        {...props}
        savedShapes={{}}
        library={{ create }}
        onApply={apply}
        error="failed"
      />
    );
    fireEvent.click(screen.getByRole('button', { name: 'node_appearance.apply' }));
    expect(apply.mock.calls).toEqual([
      ['node', { ref: `saved:${key}` }],
      ['node', { ref: `saved:${key}` }],
    ]);
    await click('node_appearance.save_copy');
    expect(create.mock.calls[1][0]).not.toBe(key);
    expect(create.mock.calls[1][2]).toEqual(DEFAULT_SHAPE);
    expect(screen.getByRole('button', { name: 'node_appearance.delete_saved' })).toBeTruthy();
    // A fetch read after both writes omits them: they were deleted elsewhere.
    view.rerender(
      <NodeAppearanceDialog
        {...props}
        savedShapes={{}}
        savedShapesRevision={2}
        library={{ create }}
        onApply={apply}
      />
    );
    expect(screen.queryByRole('button', { name: 'node_appearance.delete_saved' })).toBeNull();
    await click('node_appearance.review_latest');
    expect(document.querySelector('.graph-custom-node')).toBeTruthy();
  });
  it('edits and deletes a saved shape only after a usage warning on the reviewed revision', async () => {
    const apply = vi.fn();
    const library = {
      usage: vi.fn(async () => ({
        revision: library.usage.mock.calls.length < 4 ? 3 : 5,
        usage: 4,
      })),
      update: vi
        .fn()
        .mockRejectedValueOnce(Object.assign(new Error('stale'), { status: 409 }))
        .mockImplementation(async (key, name, definition) => ({
          key,
          name,
          definition,
          revision: 5,
          deleted: false,
        })),
      remove: vi.fn(async (key) => ({ key, revision: 6, deleted: true })),
      refresh: vi.fn(),
    };
    render(
      <NodeAppearanceDialog
        {...props}
        shapeOverrides={{ a: { ref: 'saved:card' } }}
        savedShapes={{
          card: { name: 'Card', definition: EXAMPLE_SHAPE, revision: 3 },
          other: { name: 'Other', definition: DEFAULT_SHAPE, revision: 1 },
        }}
        library={library}
        onApply={apply}
      />
    );
    fireEvent.change(screen.getByLabelText('node_appearance.scale'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('tab', { name: 'node_appearance.apply' }));
    await click('node_appearance.edit_saved');
    expect(screen.getByText('node_appearance.edit_warning')).toBeTruthy();
    expect(library.update).not.toHaveBeenCalled();
    // Switching shapes ends the review: it can never write Other's content into Card.
    const pickSaved = (value) => {
      fireEvent.click(screen.getByRole('tab', { name: 'node_appearance.shape' }));
      fireEvent.change(screen.getByLabelText('node_appearance.saved_shape'), {
        target: { value },
      });
    };
    pickSaved('other');
    pickSaved('card');
    fireEvent.change(screen.getByLabelText('node_appearance.scale'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('tab', { name: 'node_appearance.apply' }));
    expect(screen.queryByRole('button', { name: 'node_appearance.confirm_edit' })).toBeNull();
    await click('node_appearance.edit_saved');
    await click('node_appearance.confirm_edit');
    // A stale review commits nothing and must be reviewed again.
    expect(screen.getByText('node_appearance.library_conflict')).toBeTruthy();
    expect(library.refresh).toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'node_appearance.confirm_edit' })).toBeNull();
    await click('node_appearance.edit_saved');
    await click('node_appearance.confirm_edit');
    const edited = { ...EXAMPLE_SHAPE, scale: 3 };
    expect(library.update.mock.calls.map((call) => call.slice(0, 5))).toEqual([
      ['card', 'Card', edited, 3, 4],
      ['card', 'Card', edited, 3, 4],
    ]);
    fireEvent.click(screen.getByRole('button', { name: 'node_appearance.apply' }));
    expect(apply).toHaveBeenLastCalledWith('node', { ref: 'saved:card' });
    await click('node_appearance.delete_saved');
    expect(screen.getByText('node_appearance.delete_warning')).toBeTruthy();
    await click('node_appearance.confirm_delete');
    expect(library.remove).toHaveBeenCalledWith('card', 5, 4);
    // The deleted reference previews as the built-in card.
    expect(document.querySelector('.graph-custom-node')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'node_appearance.delete_saved' })).toBeNull();
  });
  it.each([
    ['a known', { name: 'Card', definition: EXAMPLE_SHAPE, revision: 3 }, DEFAULT_SHAPE],
    ['a not yet fetched', null, EXAMPLE_SHAPE],
  ])(
    'keeps a draft started from %s entry on its start when the library moves, until the user reviews',
    async (_, start, latest) => {
      const apply = vi.fn();
      const library = {
        usage: vi.fn(async () => ({ revision: 4, usage: 2 })),
        update: vi.fn(async (key, name, definition) => ({ key, name, definition, revision: 7 })),
        refresh: vi.fn(),
      };
      const view = (card, revision) => (
        <NodeAppearanceDialog
          {...props}
          shapeOverrides={{ a: { ref: 'saved:card' } }}
          savedShapes={card && { card }}
          savedShapesRevision={revision}
          library={library}
          onApply={apply}
        />
      );
      const { rerender } = render(view(start, start?.revision ?? null));
      fireEvent.change(screen.getByLabelText('node_appearance.scale'), { target: { value: '3' } });
      fireEvent.click(screen.getByRole('tab', { name: 'node_appearance.apply' }));
      if (start) {
        // A stale server revision at the warning commits nothing.
        await click('node_appearance.edit_saved');
        expect(screen.queryByRole('button', { name: 'node_appearance.confirm_edit' })).toBeNull();
        expect(library.refresh).toHaveBeenCalled();
      }
      // The library now holds revision 4: the draft stays on what it started from.
      rerender(view({ name: 'Card', definition: latest, revision: 4 }, 4));
      const edit = () => screen.getByRole('button', { name: 'node_appearance.edit_saved' });
      expect(edit().disabled).toBe(true);
      fireEvent.click(screen.getByRole('button', { name: 'node_appearance.apply' }));
      expect(apply).toHaveBeenLastCalledWith('node', {
        inline: { ...(start?.definition ?? DEFAULT_SHAPE), scale: 3 },
      });
      // Only the user's review moves the draft's own change onto revision 4.
      await click('node_appearance.review_latest');
      await click('node_appearance.edit_saved');
      await click('node_appearance.confirm_edit');
      expect(library.update).toHaveBeenCalledWith('card', 'Card', { ...latest, scale: 3 }, 4, 2);
    }
  );
});
