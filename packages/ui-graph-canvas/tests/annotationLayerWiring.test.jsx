import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import ArrowNode from '../src/components/ArrowNode';
import LabelNode from '../src/components/LabelNode';
import FreehandAnnotationNode from '../src/components/FreehandAnnotationNode';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn(), nodes: [] }));

vi.mock('reactflow', () => ({
  useReactFlow: () => ({ setNodes: hoisted.setNodes, getNodes: () => hoisted.nodes }),
}));

// The layer row is shared by five annotation context menus
// (AnnotationLayerControls). GenericAnnotationNode.test.jsx and
// NoteNode.test.jsx cover two of them; this covers the other three, so a
// wiring that drifts on one component is caught rather than assumed.
//
// The `locked` case is the reason this file exists. Four of the five menus
// hide the whole row behind a `locked ? <unlock only> : <...>` branch, but
// ArrowNode has no such branch and opens its menu whatever `locked` says —
// so the refusal has to live in `useAnnotationLayer` itself. A per-caller
// check would have left exactly one annotation kind relayerable while locked.
const CASES = [
  {
    name: 'ArrowNode',
    render: (data) => render(<ArrowNode id="x1" type="arrow" data={data} selected={false} />),
    open: () => fireEvent.contextMenu(document.querySelector('.graph-arrow-node')),
    data: { dx: 100, dy: 0 },
    flowType: 'arrow',
  },
  {
    name: 'LabelNode',
    render: (data) => render(<LabelNode id="x1" data={data} selected={false} />),
    open: () => fireEvent.contextMenu(document.querySelector('.graph-label-node')),
    data: { text: 'a label' },
    flowType: 'label',
  },
  {
    name: 'FreehandAnnotationNode',
    render: (data) => render(<FreehandAnnotationNode id="x1" data={data} selected={false} />),
    open: () => fireEvent.contextMenu(document.querySelector('svg')),
    data: {
      points: [
        { x: 0, y: 0 },
        { x: 10, y: 10 },
      ],
    },
    flowType: 'freehand',
  },
];

describe('shared annotation layer row', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.nodes = [];
  });

  for (const c of CASES) {
    describe(c.name, () => {
      it('brings the annotation in front of the others', () => {
        hoisted.nodes = [
          { id: 'x1', type: c.flowType, zIndex: 0 },
          { id: 'other', type: 'note', zIndex: 4 },
        ];
        c.render(c.data);
        c.open();
        fireEvent.click(screen.getByLabelText('Bring to front'));
        const updated = hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes);
        expect(updated[0].zIndex).toBe(5);
      });

      it('refuses a layer change on a locked annotation', () => {
        hoisted.nodes = [
          { id: 'x1', type: c.flowType, zIndex: 0 },
          { id: 'other', type: 'note', zIndex: 4 },
        ];
        c.render({ ...c.data, locked: true });
        c.open();
        // Four of the five menus withhold the row entirely when locked;
        // ArrowNode still renders it, so the click must be refused instead.
        const button = screen.queryByLabelText('Bring to front');
        if (button) fireEvent.click(button);
        expect(hoisted.setNodes).not.toHaveBeenCalled();
      });
    });
  }
});
