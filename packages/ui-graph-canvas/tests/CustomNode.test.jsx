// covers: packages/ui-graph-canvas/src/components/CustomNode.jsx, packages/ui-graph-canvas/src/utils/nodeShapes.js
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { URL as NodeURL } from 'node:url';
import ReactFlow, { ReactFlowProvider } from 'reactflow';

const customNodeCss = readFileSync(
  new NodeURL('../src/components/CustomNode.css', import.meta.url),
  'utf8'
);

vi.mock('reactflow', async (importOriginal) => ({
  ...(await importOriginal()),
  Handle: ({ type }) => (
    <div data-testid={`handle-${type}`} className="react-flow__handle connectionindicator" />
  ),
}));

import CustomNode from '../src/components/CustomNode';
import {
  BAND_TEXT_DARK,
  BAND_TEXT_LIGHT,
  bandTextStyle,
  createNodeShapeResolver,
  nodeShapeActionLayout,
  nodeShapeAccessibleName,
  nodeShapeAreaValues,
  nodeShapeGeometry,
  nodeShapeTextBox,
} from '../src/utils/nodeShapes';

describe('CustomNode theme shadow cascade', () => {
  it.each(['selected', 'highlighted', 'selected highlighted'])(
    'preserves light hover and dark state shadows for %s nodes',
    (state) => {
      // Parse the actual stylesheet: jsdom does not evaluate media queries or :hover.
      const style = document.createElement('style');
      style.textContent = customNodeCss;
      document.head.appendChild(style);
      const node = document.createElement('div');
      node.className = `graph-custom-node ${state} test-hover`;

      const shadowFor = (light, hovered) => {
        node.classList.toggle('test-hover', hovered);
        let shadow;
        let specificity = -1;
        const visit = (rules) => {
          for (const rule of rules) {
            if (rule.media) {
              if (light && rule.conditionText === '(prefers-color-scheme: light)') {
                visit(rule.cssRules);
              }
              continue;
            }
            const selector = rule.selectorText;
            const value = rule.style?.getPropertyValue('box-shadow');
            if (
              !selector ||
              !value ||
              !node.matches(selector.replaceAll(':hover', '.test-hover'))
            ) {
              continue;
            }
            const weight = (selector.match(/[.:][\w-]+/g) || []).length;
            if (weight >= specificity) {
              specificity = weight;
              shadow = value.replace(/\s+/g, ' ').trim();
            }
          }
        };
        visit(style.sheet.cssRules);
        return shadow;
      };

      try {
        const stateShadow = state.includes('selected')
          ? '0 0 0 3px var(--graph-card-focus), 0 0 16px rgba(100, 108, 255, 0.3)'
          : '0 0 12px rgba(100, 108, 255, 0.4)';
        expect(shadowFor(true, true)).toBe('var(--graph-card-shadow-hover)');
        expect(shadowFor(false, true)).toBe(stateShadow);
        expect(shadowFor(true, false)).toBe(stateShadow);
        expect(shadowFor(false, false)).toBe(stateShadow);
      } finally {
        style.remove();
      }
    }
  );
});

describe('CustomNode tooltip', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders tooltip below the node in a top-level layer on hover', () => {
    const rect = {
      top: 100,
      left: 50,
      width: 120,
      height: 40,
      right: 170,
      bottom: 140,
    };

    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue(rect);

    render(
      <CustomNode
        id="node-1"
        selected={false}
        data={{
          label: 'Node 1',
          nodeType: 'Actor',
          color: '#3B82F6',
          description: 'Tooltip body',
          communities: ['A'],
        }}
      />
    );

    fireEvent.mouseEnter(screen.getByText('Node 1').closest('.graph-custom-node'));

    const tooltip = screen.getByText('Tooltip body').closest('.graph-node-tooltip');
    expect(tooltip).toBeInTheDocument();
    expect(tooltip.parentElement).toBe(document.body);
    expect(tooltip.style.top).toBe('148px');
    expect(tooltip.style.left).toBe('110px');
    expect(tooltip.style.zIndex).toBe('99999');
  });

  it('does not render the tooltip when the preview is disabled', () => {
    render(
      <CustomNode
        id="node-1"
        selected={false}
        data={{
          label: 'Node 1',
          nodeType: 'Actor',
          color: '#3B82F6',
          description: 'Tooltip body',
          previewEnabled: false,
        }}
      />
    );
    fireEvent.mouseEnter(screen.getByText('Node 1').closest('.graph-custom-node'));
    expect(screen.queryByText('Tooltip body')).toBeNull();
  });

  it('renders a remote-selection outline and name badge in the collaborator colour', () => {
    render(
      <CustomNode
        id="node-1"
        selected={false}
        data={{
          label: 'Node 1',
          nodeType: 'Actor',
          color: '#3B82F6',
          remoteSelection: { color: '#e6194b', displayName: 'Ada' },
        }}
      />
    );
    const node = screen.getByText('Node 1').closest('.graph-custom-node');
    expect(node.className).toContain('remote-selected');
    expect(node.style.outline).toBe('2px solid #e6194b');
    const badge = screen.getByText('Ada');
    expect(badge.className).toContain('graph-node-remote-badge');
    expect(badge.style.backgroundColor).toBe('rgb(230, 25, 75)');
  });

  it('has no remote marker when remoteSelection is absent', () => {
    render(
      <CustomNode
        id="node-1"
        selected={false}
        data={{ label: 'Node 1', nodeType: 'Actor', color: '#3B82F6' }}
      />
    );
    const node = screen.getByText('Node 1').closest('.graph-custom-node');
    expect(node.className).not.toContain('remote-selected');
    expect(node.style.outline).toBe('');
  });
});

describe('CustomNode pulse overlay', () => {
  it('renders a pulse overlay with the requested style and colour', () => {
    const { container } = render(
      <CustomNode
        id="node-1"
        selected={false}
        data={{
          label: 'Node 1',
          nodeType: 'Actor',
          color: '#3B82F6',
          pulse: { style: 'grow', color: '#ff8800', seq: 1 },
        }}
      />
    );
    const overlay = container.querySelector('.graph-node-pulse');
    expect(overlay).not.toBeNull();
    expect(overlay.className).toContain('graph-node-pulse-grow');
    expect(overlay.style.getPropertyValue('--pulse-color')).toBe('#ff8800');
  });

  it('falls back to glow for an unknown style and to the node colour', () => {
    const { container } = render(
      <CustomNode
        id="node-1"
        selected={false}
        data={{
          label: 'Node 1',
          nodeType: 'Actor',
          color: '#3B82F6',
          pulse: { style: 'explode', seq: 2 },
        }}
      />
    );
    const overlay = container.querySelector('.graph-node-pulse');
    expect(overlay.className).toContain('graph-node-pulse-glow');
    expect(overlay.style.getPropertyValue('--pulse-color')).toBe('#3B82F6');
  });

  it('renders no overlay when pulse is absent', () => {
    const { container } = render(
      <CustomNode
        id="node-1"
        selected={false}
        data={{ label: 'Node 1', nodeType: 'Actor', color: '#3B82F6' }}
      />
    );
    expect(container.querySelector('.graph-node-pulse')).toBeNull();
  });
});

describe('CustomNode staged marker', () => {
  it('draws a staged node apart from a graph node, badged with its state', () => {
    render(
      <CustomNode
        id="node-1"
        selected={false}
        data={{
          label: 'Draft',
          nodeType: 'Actor',
          color: '#3B82F6',
          stagedState: 'created',
          stagedLabel: 'Staged: new',
        }}
      />
    );
    const node = screen.getByText('Draft').closest('.graph-custom-node');
    expect(node.className).toContain('staged');
    expect(node.className).toContain('staged-created');
    expect(screen.getByText('Staged: new').dataset.stagedState).toBe('created');
  });

  it('carries no marker for a node the session has not staged', () => {
    render(
      <CustomNode
        id="node-2"
        selected={false}
        data={{ label: 'Plain', nodeType: 'Actor', color: '#3B82F6' }}
      />
    );
    const node = screen.getByText('Plain').closest('.graph-custom-node');
    expect(node.className).not.toContain('staged');
    expect(document.querySelector('.graph-node-staged-badge')).toBeNull();
  });
});

const RISK = {
  version: 1,
  base: 'triangle-up',
  scale: 1,
  placement: 'centre',
  areas: { first: 'name', second: 'risk_level', third: null },
};
const CARD = {
  version: 1,
  base: 'rectangle',
  scale: 1,
  placement: 'top-left',
  areas: { first: 'type', second: 'name', third: 'summary' },
};
const graphNode = {
  id: 'n1',
  type: 'Risk',
  name: 'Supply delay',
  summary: 'Parts arrive late '.repeat(20),
  risk_level: 'high',
  owner: { name: 'object' },
  hidden: 'undeclared',
  metadata: 'private',
};
const FIELDS = ['risk_level', 'owner', 'metadata'];
const flowNodeTypes = { custom: CustomNode };

function shapedFlowNode(definition, data = {}, node = graphNode) {
  const geometry = nodeShapeGeometry(definition.base, definition.scale);
  const values = nodeShapeAreaValues(definition, node, FIELDS);
  return {
    id: node.id,
    type: 'custom',
    position: { x: 0, y: 0 },
    geometry,
    className: 'graph-shaped-flow-node',
    ariaLabel: nodeShapeAccessibleName(definition, node, values),
    data: {
      label: node.name,
      nodeType: node.type,
      color: '#1e3a8a',
      shapeRender: { definition, values, geometry },
      ...data,
    },
  };
}

function renderFlow(nodes, { onNodesChange = () => {} } = {}) {
  return render(
    <ReactFlowProvider>
      <ReactFlow
        nodes={nodes}
        edges={[]}
        nodeTypes={flowNodeTypes}
        defaultViewport={{ x: 0, y: 0, zoom: 1 }}
        minZoom={0.01}
        onNodesChange={onNodesChange}
      />
    </ReactFlowProvider>
  );
}

function withStylesheet(run) {
  const style = document.createElement('style');
  style.textContent = customNodeCss;
  document.head.appendChild(style);
  try {
    run();
  } finally {
    style.remove();
  }
}

describe('CustomNode configured shapes', () => {
  it('draws band, surface, shadow and outline for a configured base and keeps the card otherwise', () => {
    renderFlow([
      shapedFlowNode(CARD),
      // A graph attribute that merely looks like a shape cannot opt in.
      {
        id: 'plain',
        type: 'custom',
        position: { x: 400, y: 0 },
        data: { label: 'Plain', nodeType: 'Actor', shapeRender: { geometry: { anchor: 'x' } } },
      },
    ]);
    const shaped = screen.getByTestId('rf__node-n1');
    expect(shaped.querySelector('.graph-custom-node')).toBeNull();
    const root = shaped.querySelector('.graph-shaped-node.graph-shape-rectangle');
    for (const part of ['shadow', 'body', 'outline']) {
      expect(root.querySelector(`.graph-shape-${part}`).getAttribute('d')).toBe(
        nodeShapeGeometry('rectangle', 1).path
      );
    }
    expect(root.querySelector('.graph-shape-band').style.fill).toBe('#1e3a8a');
    expect(screen.getByTestId('rf__node-plain').querySelector('.graph-custom-node')).not.toBeNull();
  });

  it('binds areas to plain text of declared scalars only and keeps the band when text is None', () => {
    expect(
      nodeShapeAreaValues(
        { areas: { first: 'type', second: 'risk_level', third: 'summary' } },
        graphNode,
        FIELDS
      )
    ).toEqual({ first: 'Risk', second: 'high', third: graphNode.summary });
    expect(
      nodeShapeAreaValues(
        { areas: { first: 'owner', second: 'hidden', third: 'metadata' } },
        graphNode,
        FIELDS
      )
    ).toEqual({ first: '', second: '', third: '' });
    // Declared fields live in metadata and win over a top-level value.
    expect(
      nodeShapeAreaValues(
        { areas: { first: 'risk_level', second: 'owner', third: null } },
        { ...graphNode, metadata: { risk_level: 'low', owner: 'Ada' } },
        FIELDS
      )
    ).toEqual({ first: 'low', second: 'Ada', third: '' });

    const markup = { ...graphNode, name: '<b>bold</b>' };
    const definition = { ...CARD, areas: { first: null, second: 'name', third: 'summary' } };
    renderFlow([shapedFlowNode(definition, {}, markup)]);
    const root = document.querySelector('.graph-shaped-node');
    expect(root.querySelector('.graph-node-label').textContent).toBe('<b>bold</b>');
    expect(root.querySelector('b')).toBeNull();
    expect(root.querySelector('.graph-shape-band')).not.toBeNull();
    expect(root.querySelector('.graph-shape-label-chip')).toBeNull();
    const area = root.querySelector('.graph-shape-text');
    const box = nodeShapeTextBox(nodeShapeGeometry('rectangle', 1), 'top-left');
    expect(parseFloat(area.style.width)).toBe(box.width);
    expect(parseFloat(area.style.height)).toBe(box.height);
    expect(parseFloat(area.style.minHeight)).toBeCloseTo(nodeShapeGeometry('rectangle', 1).text.title * 1.2);
    withStylesheet(() => {
      expect(getComputedStyle(area).overflow).toBe('hidden');
      // Only the summary shrinks in the fixed-height column; the name keeps its lines.
      const shrink = (el) => getComputedStyle(el).flexShrink;
      expect(shrink(area.querySelector('.graph-node-label'))).toBe('0');
      const summary = [...area.children].find((el) => !el.matches('.graph-node-label'));
      expect(shrink(summary)).not.toBe('0');
    });
  });

  it('keeps every placement inside its base and the downward bottom stack off the tip', () => {
    const placements = {
      rectangle: ['top', 'middle', 'bottom'].flatMap((v) =>
        ['left', 'centre', 'right'].map((h) => `${v}-${h}`)
      ),
      'triangle-up': ['centre', 'bottom'],
      'triangle-down': ['centre', 'bottom'],
    };
    placements['rectangle-flat'] = placements.rectangle;
    const resolve = (definition) =>
      createNodeShapeResolver({
        nodeTypes: { Risk: { shape: 'x' } },
        nodeShapes: { x: definition },
      })(graphNode);
    for (const [base, allowed] of Object.entries(placements)) {
      const geometry = nodeShapeGeometry(base, 2);
      for (const placement of allowed) {
        const { x, y, width, height } = nodeShapeTextBox(geometry, placement);
        for (const corner of [
          { x, y },
          { x: x + width, y },
          { x, y: y + height },
          { x: x + width, y: y + height },
        ]) {
          expect(geometry.contains(corner)).toBe(true);
        }
        expect(resolve({ ...CARD, base, placement })?.definition.placement).toBe(placement);
      }
      const foreign = base.startsWith('triangle') ? 'top-left' : 'centre';
      expect(resolve({ ...CARD, base, placement: foreign })).toBeNull();
    }
    const down = nodeShapeGeometry('triangle-down', 1);
    const bottom = nodeShapeTextBox(down, 'bottom');
    expect(bottom.y).toBeGreaterThan(nodeShapeTextBox(down, 'centre').y);
    expect(bottom.y + bottom.height).toBeLessThan(down.height * 0.75);
  });

  it.each([['rectangle-flat', 'bottom-left'], ['triangle-down', 'bottom']])(
    'reserves a full title line at enlarged root font sizes for %s',
    (base, placement) => {
      renderFlow([shapedFlowNode({ ...CARD, base, placement, scale: 1 })]);
      const area = document.querySelector('.graph-shape-text');
      const title = area.querySelector('.graph-node-label');
      const lineHeight = nodeShapeGeometry(base, 1).text.title * 1.2;
      expect(parseFloat(area.style.minHeight)).toBeCloseTo(lineHeight);
      expect(area.style.minHeight).toMatch(/rem$/);
      withStylesheet(() => expect(getComputedStyle(title).lineHeight).toBe('1.2'));
    }
  );

  it('names the node by type, name and mapped values once, hiding the SVG decoration', () => {
    expect(
      nodeShapeAccessibleName(RISK, graphNode, { first: 'Supply delay', second: 'high', third: '' })
    ).toBe('Risk, Supply delay, high');
    renderFlow([shapedFlowNode(CARD)]);
    const wrapper = screen.getByTestId('rf__node-n1');
    // jsdom leaves unmeasured wrappers hidden, so read the name the wrapper
    // button carries; aria-label replaces its content as the spoken name.
    expect(wrapper.getAttribute('role')).toBe('button');
    // The clipped summary is still spoken in full; the type and name are not repeated.
    expect(wrapper.getAttribute('aria-label')).toBe(`Risk, Supply delay, ${graphNode.summary}`);
    expect(wrapper.querySelector('svg').getAttribute('aria-hidden')).toBe('true');
  });

  it('keeps state badges and non-colour cues on a configured shape', () => {
    renderFlow([
      {
        ...shapedFlowNode(RISK, {
          nodeType: 'Skill',
          isDimmed: true,
          markColor: '#ff0000',
          markLabel: 'Watch',
          pulse: { style: 'glow', seq: 1 },
          remoteSelection: { color: '#00aa00', displayName: 'Ada' },
          stagedState: 'created',
          stagedLabel: 'Staged: new',
        }),
        selected: true,
      },
    ]);
    const root = document.querySelector('.graph-shaped-node');
    for (const state of [
      'selected',
      'marked',
      'skill-node',
      'remote-selected',
      'dimmed',
      'staged-created',
    ]) {
      expect(root.classList.contains(state)).toBe(true);
    }
    expect(screen.getByText('Ada').className).toContain('graph-node-remote-badge');
    expect(screen.getByText('Staged: new').dataset.stagedState).toBe('created');
    expect(screen.getByText('★')).toBeTruthy();
    expect(root.querySelector('.graph-node-mark-badge')).not.toBeNull();
    expect(root.querySelector('.graph-node-pulse-glow')).not.toBeNull();
    expect(root.querySelector('.graph-shape-ring-focus')).not.toBeNull();
    withStylesheet(() => {
      const outline = getComputedStyle(root.querySelector('.graph-shape-outline'));
      expect(outline.strokeDasharray || outline.getPropertyValue('stroke-dasharray')).not.toBe('');
      // Stacked rings, drawn widest first, each keep a band past the next one in.
      const rings = [...root.querySelectorAll('.graph-shape-ring')];
      const widths = rings.map((ring) => parseFloat(getComputedStyle(ring).strokeWidth));
      expect(rings.map((ring) => ring.classList[1])).toEqual([
        'graph-shape-ring-keyboard',
        'graph-shape-ring-remote',
        'graph-shape-ring-focus',
        'graph-shape-ring-mark',
      ]);
      for (let i = 1; i < widths.length; i += 1) {
        expect(widths[i - 1] - widths[i]).toBeGreaterThanOrEqual(4);
      }
      // A keyboard-focused node shows its ring even when it is not selected.
      const keyboard = rings[0];
      const wrapper = screen.getByTestId('rf__node-n1');
      // jsdom matches :focus-visible but does not cascade it, so read the rules.
      const strokes = () =>
        [...document.styleSheets]
          .flatMap((sheet) => [...sheet.cssRules])
          .filter((rule) => rule.selectorText && keyboard.matches(rule.selectorText))
          .map((rule) => rule.style.getPropertyValue('stroke'))
          .filter(Boolean);
      // A dimmed, unselected node lifts its dimming while keyboard-focused.
      root.classList.remove('selected', 'remote-selected');
      const opacity = () =>
        [...document.styleSheets]
          .flatMap((sheet) => [...sheet.cssRules])
          .filter((rule) => rule.selectorText && root.matches(rule.selectorText))
          .map((rule) => rule.style.getPropertyValue('opacity'))
          .filter(Boolean)
          .at(-1);
      expect(strokes()).toEqual(['transparent']);
      expect(opacity()).toBe('0.35');
      wrapper.focus();
      expect(strokes().at(-1)).not.toBe('transparent');
      expect(opacity()).toBe('1');
    });
  });

  it('passes the pointer through everything but the painted body and its outside label', () => {
    renderFlow([
      shapedFlowNode({ ...CARD, base: 'triangle-down', placement: 'bottom' }, {
        markColor: '#ff0000',
        remoteSelection: { color: '#00aa00', displayName: 'Ada' },
        stagedState: 'created',
      }),
      { ...shapedFlowNode(CARD), id: 'n2' },
    ]);
    const wrapper = screen.getByTestId('rf__node-n1');
    withStylesheet(() => {
      // React Flow sets pointer-events inline, which only an important rule
      // overrides in a browser; jsdom does not apply that precedence itself.
      const wrapperRule = [...document.styleSheets]
        .flatMap((sheet) => [...sheet.cssRules])
        .find((rule) => rule.selectorText && wrapper.matches(rule.selectorText));
      expect(wrapperRule.style.getPropertyValue('pointer-events')).toBe('none');
      expect(wrapperRule.style.getPropertyPriority('pointer-events')).toBe('important');
      const events = (root, selector) =>
        getComputedStyle(root.querySelector(selector)).pointerEvents;
      expect(events(wrapper, '.graph-shaped-node')).toBe('none');
      expect(events(wrapper, '.graph-shape-body')).toBe('auto');
      // The rem-sized minimum can extend below this fixed-pixel box at an
      // enlarged root font size, into the triangle's transparent corners.
      const text = wrapper.querySelector('.graph-shape-text');
      expect(parseFloat(text.style.minHeight) * 64).toBeGreaterThan(parseFloat(text.style.height));
      expect(events(wrapper, '.graph-shape-text')).toBe('none');
      expect(events(wrapper, '.graph-shape-text .graph-node-label')).toBe('none');
      expect(events(wrapper, '.graph-shape-text .graph-node-summary')).toBe('none');
      // A triangle's outside label is a visible label, so it is hit too.
      expect(events(wrapper, '.graph-shape-outside-label .graph-shape-label-chip')).toBe('auto');
      // Badges lie beyond the body.
      for (const outside of [
        '.graph-node-mark-badge',
        '.graph-node-remote-badge',
        '.graph-node-staged-badge',
      ]) {
        expect(events(wrapper, outside)).toBe('none');
      }
      // The outside label stacks above the body and the badges beside it.
      const zIndex = (selector) =>
        Number(getComputedStyle(wrapper.querySelector(selector)).zIndex) || 0;
      expect(zIndex('.graph-shape-outside-label')).toBeGreaterThan(
        Math.max(zIndex('.graph-shape-svg'), zIndex('.graph-node-remote-badge'))
      );
      // The triangle's remote badge ends where the outside label row begins.
      const badge = wrapper.querySelector('.graph-node-remote-badge');
      const label = wrapper.querySelector('.graph-shape-outside-label');
      expect(badge.style.top).toBe(label.style.top);
      expect(badge.style.transform).toBe('translateY(-100%)');
      // Handles are hit as on the card: React Flow's connectable rule applies
      // unopposed, and they sit on the body edge of every base.
      const handleRules = [...document.styleSheets]
        .flatMap((sheet) => [...sheet.cssRules])
        .filter(
          (rule) =>
            rule.selectorText &&
            wrapper.querySelector('.react-flow__handle').matches(rule.selectorText)
        );
      expect(
        handleRules.map((rule) => rule.style.getPropertyValue('pointer-events')).filter(Boolean)
      ).toEqual([]);
      for (const base of ['rectangle', 'rectangle-flat', 'triangle-up', 'triangle-down']) {
        const geometry = nodeShapeGeometry(base);
        expect(geometry.contains({ x: geometry.width / 2, y: 0 })).toBe(true);
        expect(geometry.contains({ x: geometry.width / 2, y: geometry.height })).toBe(true);
      }
      expect(events(screen.getByTestId('rf__node-n2'), '.graph-shape-label-chip')).toBe('auto');
    });
  });

  it.each(
    ['rectangle', 'rectangle-flat', 'triangle-up', 'triangle-down'].flatMap((base) => [
      [base, 0.1],
      [base, 10],
    ])
  )(
    'keeps every action button of a %s body (scale %s) inside it and off the text',
    (base, scale) => {
      const geometry = nodeShapeGeometry(base, scale);
      const placements = base.startsWith('triangle')
        ? ['centre', 'bottom']
        : ['top-left', 'middle-centre', 'bottom-right'];
      const inside = ({ x, y, size }) =>
        [
          { x, y },
          { x: x + size, y },
          { x, y: y + size },
          { x: x + size, y: y + size },
        ].every((corner) => geometry.contains(corner));
      for (const placement of placements) {
        for (const count of [1, 2]) {
          const { buttons, inset } = nodeShapeActionLayout(geometry, placement, count);
          const box = nodeShapeTextBox(geometry, placement);
          expect(buttons).toHaveLength(count);
          for (const button of buttons) {
            expect(inside(button)).toBe(true);
            expect(button.x).toBeGreaterThanOrEqual(box.x + box.width - inset);
          }
        }
      }

      // Selected rather than hovered: the buttons stay visible without a hover path.
      const placement = placements[1];
      renderFlow([
        {
          ...shapedFlowNode({ ...CARD, base, placement, scale }, { onExpand() {}, onEdit() {} }),
          selected: true,
        },
      ]);
      const text = document.querySelector('.graph-shape-text');
      const box = nodeShapeTextBox(geometry, placement);
      const reserved = box.x + box.width - parseFloat(text.style.paddingRight);
      const rendered = ['Show related nodes', 'Edit node'].map((title) => {
        const { left, top, width, height } = screen.getByTitle(title).style;
        expect(parseFloat(width)).toBe(parseFloat(height));
        return { x: parseFloat(left), y: parseFloat(top), size: parseFloat(width) };
      });
      for (const button of rendered) {
        expect(inside(button)).toBe(true);
        expect(button.x).toBeGreaterThanOrEqual(reserved);
      }
      expect(rendered[1].y).toBeGreaterThanOrEqual(rendered[0].y + rendered[0].size);
      withStylesheet(() => {
        expect(getComputedStyle(text).boxSizing).toBe('border-box');
        for (const button of document.querySelectorAll('.graph-shaped-node button')) {
          expect(getComputedStyle(button).transform).toBe('none');
        }
      });
    }
  );

  it('picks band text with at least 4.5:1 contrast or an opaque backing', () => {
    expect(bandTextStyle('#767676')).toEqual({ color: BAND_TEXT_LIGHT });
    expect(bandTextStyle('#777777')).toEqual({ color: BAND_TEXT_DARK });
    expect(bandTextStyle('#fde047')).toEqual({ color: BAND_TEXT_DARK });
    expect(bandTextStyle('tomato').background).toBe('var(--graph-card-surface)');
  });

  it('selects by keyboard and then offers the related-node action without a pointer', () => {
    const onNodesChange = vi.fn();
    const onExpand = vi.fn();
    const { rerender } = renderFlow([shapedFlowNode(CARD, { onExpand })], { onNodesChange });
    const wrapper = screen.getByTestId('rf__node-n1');
    wrapper.focus();
    fireEvent.keyDown(wrapper, { key: 'Enter' });
    expect(onNodesChange).toHaveBeenCalledWith([{ id: 'n1', type: 'select', selected: true }]);
    rerender(
      <ReactFlowProvider>
        <ReactFlow
          nodes={[{ ...shapedFlowNode(CARD, { onExpand }), selected: true }]}
          edges={[]}
          nodeTypes={flowNodeTypes}
        />
      </ReactFlowProvider>
    );
    fireEvent.click(screen.getByTitle('Show related nodes'));
    expect(onExpand).toHaveBeenCalledTimes(1);
  });
});
