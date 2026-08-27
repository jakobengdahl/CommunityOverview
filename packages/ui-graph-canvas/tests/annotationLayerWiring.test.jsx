import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import ArrowNode from '../src/components/ArrowNode';
import LabelNode from '../src/components/LabelNode';
import FreehandAnnotationNode from '../src/components/FreehandAnnotationNode';
import { useAnnotationLayer } from '../src/components/AnnotationLayerControls';
import { AnnotationContext } from '../src/components/AnnotationContext';
import { LAYER_FRONT } from '../src/utils/annotationLayers';

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
// hid the whole row behind a `locked ? <unlock only> : <...>` branch;
// ArrowNode had no such branch and opened its menu whatever `locked` said,
// so relying on each caller's markup left exactly one annotation kind
// relayerable while locked. ArrowNode has since gained that branch too, so
// its case below now passes because the whole menu is unlock-only rather
// than because the row withholds itself. The row still withholds itself when
// locked (AnnotationLayerControls returns null) and the hook refuses
// independently
// of any markup — the two are pinned separately below, so neither guard can
// be dropped without a test failing.
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

      it('sends the annotation behind the others', () => {
        hoisted.nodes = [
          { id: 'x1', type: c.flowType, zIndex: 0 },
          { id: 'other', type: 'note', zIndex: -2 },
        ];
        c.render(c.data);
        c.open();
        fireEvent.click(screen.getByLabelText('Send to back'));
        const updated = hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes);
        expect(updated[0].zIndex).toBe(-3);
      });

      it('refuses a layer change on a locked annotation', () => {
        hoisted.nodes = [
          { id: 'x1', type: c.flowType, zIndex: 0 },
          { id: 'other', type: 'note', zIndex: 4 },
        ];
        c.render({ ...c.data, locked: true });
        c.open();
        // The row is withheld from a locked annotation in all five menus
        // (AnnotationLayerControls returns null), so a locked annotation
        // never meets a visibly present but inert control. The hook's own
        // refusal is pinned separately, below.
        expect(screen.queryByLabelText('Bring to front')).toBeNull();
        expect(screen.queryByLabelText('Send to back')).toBeNull();
      });
    });
  }
});

// The hook refuses independently of whether any caller's markup happens to
// hide the row. That separation is the point: relying on each menu's own
// `locked` branch is exactly how a locked line stayed relayerable in the
// first place, back when ArrowNode was the one menu without such a branch.
// Every menu has one now, so no caller renders the row for a locked
// annotation and the markup-level assertions above cannot fail — driving the
// hook directly is what keeps the guarantee pinned.
describe('useAnnotationLayer refuses independently of the markup', () => {
  function Harness({ data }) {
    const changeLayer = useAnnotationLayer('x1', data);
    return (
      <button type="button" onClick={() => changeLayer(LAYER_FRONT)}>
        go
      </button>
    );
  }

  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.nodes = [
      { id: 'x1', type: 'note', zIndex: 0 },
      { id: 'other', type: 'note', zIndex: 4 },
    ];
  });

  it('refuses when the annotation carries the persisted locked flag', () => {
    render(<Harness data={{ locked: true }} />);
    fireEvent.click(screen.getByText('go'));
    expect(hoisted.setNodes).not.toHaveBeenCalled();
  });

  it('refuses and surfaces the attempt when another client holds the claim', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    render(
      <AnnotationContext.Provider value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt }}>
        <Harness data={{ remoteSelection: { color: '#f00', displayName: 'Ada' } }} />
      </AnnotationContext.Provider>
    );
    fireEvent.click(screen.getByText('go'));
    expect(notifyRemoteLockedAttempt).toHaveBeenCalled();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
  });

  it('acts when the annotation is neither locked nor claimed', () => {
    render(<Harness data={{}} />);
    fireEvent.click(screen.getByText('go'));
    expect(hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes)[0].zIndex).toBe(5);
  });
});
