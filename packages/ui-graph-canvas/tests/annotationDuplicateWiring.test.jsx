import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import ArrowNode from '../src/components/ArrowNode';
import LabelNode from '../src/components/LabelNode';
import FreehandAnnotationNode from '../src/components/FreehandAnnotationNode';
import {
  useAnnotationDuplicate,
  DUPLICATE_OFFSET,
} from '../src/components/AnnotationDuplicateControl';
import { AnnotationContext } from '../src/components/AnnotationContext';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn(), nodes: [] }));

vi.mock('reactflow', () => ({
  useReactFlow: () => ({ setNodes: hoisted.setNodes, getNodes: () => hoisted.nodes }),
}));

// The duplicate action is shared by five annotation context menus
// (AnnotationDuplicateControl), the same way the layer row is
// (annotationLayerWiring.test.jsx). This covers the three kinds that share
// this file's pattern there — ArrowNode, LabelNode, FreehandAnnotationNode —
// so a wiring that drifts on one component is caught rather than assumed.
// NoteNode.test.jsx and GenericAnnotationNode.test.jsx cover the other two.
//
// Unlike the layer row, duplicate is offered in BOTH the locked and unlocked
// branches of every menu (the capability baseline is "a locked object
// remains selectable but offers only unlock or copy") — so the interesting
// case here is not "is it withheld while locked" but "does a locked source
// still produce an *unlocked* copy", the PR #515/#517 bug class this task
// exists to guard against.
const CASES = [
  {
    name: 'ArrowNode',
    render: (data) => render(<ArrowNode id="x1" type="arrow" data={data} selected={false} />),
    open: () => fireEvent.contextMenu(document.querySelector('.graph-arrow-node')),
    data: { dx: 100, dy: 0, color: '#111827' },
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

describe('shared annotation duplicate control', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.nodes = [];
  });

  for (const c of CASES) {
    describe(c.name, () => {
      it('creates a new, offset, unlocked copy and leaves the source untouched', () => {
        const source = { id: 'x1', type: c.flowType, position: { x: 50, y: 60 }, data: c.data };
        hoisted.nodes = [source];
        c.render(c.data);
        c.open();
        fireEvent.click(screen.getByText(/Duplicate/));
        const updated = hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes);
        expect(updated).toHaveLength(2);
        const [original, copy] = updated;
        expect(original).toBe(source); // untouched, same reference
        expect(copy.id).not.toBe('x1');
        expect(copy.type).toBe(c.flowType);
        expect(copy.position).toEqual({ x: 50 + DUPLICATE_OFFSET, y: 60 + DUPLICATE_OFFSET });
        expect(copy.data.locked).toBe(false);
      });

      it('offers duplicate on a locked annotation and never locks the copy', () => {
        const lockedData = { ...c.data, locked: true };
        // `draggable: false` mirrors what `overlayToFlowNode` stamps onto a
        // locked node's top level at hydration time — the field the bug this
        // test guards against inherits verbatim from `...source` instead of
        // recomputing from the copy's (now-unlocked) data.
        const source = {
          id: 'x1',
          type: c.flowType,
          position: { x: 0, y: 0 },
          data: lockedData,
          draggable: false,
        };
        hoisted.nodes = [source];
        c.render(lockedData);
        c.open();
        expect(screen.getByText(/Duplicate/)).toBeInTheDocument();
        fireEvent.click(screen.getByText(/Duplicate/));
        const updated = hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes);
        const copy = updated.find((n) => n.id !== 'x1');
        expect(copy).toBeDefined();
        expect(copy.data.locked).toBe(false);
        // The source itself stays locked — duplicating never mutates it.
        expect(updated.find((n) => n.id === 'x1').data.locked).toBe(true);
        // Regression: an unlocked `data.locked` copy must actually be
        // draggable, not "phantom-locked" by a stale top-level `draggable`
        // inherited from the locked source.
        expect(copy.draggable).not.toBe(false);
      });
    });
  }
});

// The hook itself, independent of any one menu's markup — same split as
// useAnnotationLayer's own harness-driven section below it in
// annotationLayerWiring.test.jsx.
describe('useAnnotationDuplicate', () => {
  function Harness({ id = 'x1', data }) {
    const duplicate = useAnnotationDuplicate(id, data);
    return (
      <button type="button" onClick={duplicate}>
        go
      </button>
    );
  }

  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.nodes = [];
  });

  it('publishes the duplicate as a create, not a debounced edit', () => {
    const notifyChange = vi.fn();
    hoisted.nodes = [{ id: 'x1', type: 'note', position: { x: 0, y: 0 }, data: { text: 'hi' } }];
    render(
      <AnnotationContext.Provider value={{ notifyChange }}>
        <Harness data={{ text: 'hi' }} />
      </AnnotationContext.Provider>
    );
    fireEvent.click(screen.getByText('go'));
    expect(notifyChange).toHaveBeenCalledWith('create');
  });

  it('refuses and surfaces the attempt when another client holds the claim', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    hoisted.nodes = [{ id: 'x1', type: 'note', position: { x: 0, y: 0 }, data: {} }];
    render(
      <AnnotationContext.Provider value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt }}>
        <Harness data={{ remoteSelection: { color: '#f00', displayName: 'Ada' } }} />
      </AnnotationContext.Provider>
    );
    fireEvent.click(screen.getByText('go'));
    expect(notifyRemoteLockedAttempt).toHaveBeenCalled();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
  });

  it('is a no-op when the source id is not found among the live nodes', () => {
    hoisted.nodes = [{ id: 'other', type: 'note', position: { x: 0, y: 0 }, data: {} }];
    render(<Harness id="missing" data={{}} />);
    fireEvent.click(screen.getByText('go'));
    expect(hoisted.setNodes).not.toHaveBeenCalled();
  });

  it('copies attachment/anchor content verbatim onto an unlocked copy', () => {
    // An unlocked, attached source is an ordinary valid state (the binding
    // is only ever dropped at the moment something is *locked*), so the
    // copy should keep it — only `locked` itself is forced.
    const data = { text: 'x', attachment: { target_id: 'g1', target_type: 'node' } };
    hoisted.nodes = [{ id: 'x1', type: 'label', position: { x: 0, y: 0 }, data }];
    render(<Harness data={data} />);
    fireEvent.click(screen.getByText('go'));
    const updated = hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes);
    const copy = updated.find((n) => n.id !== 'x1');
    expect(copy.data.attachment).toEqual({ target_id: 'g1', target_type: 'node' });
    expect(copy.data.locked).toBe(false);
  });

  it('drops a stale remoteSelection marker rather than copying it onto the new id', () => {
    // getNodes()'s copy of the source still carries a remoteSelection
    // marker, but the `data` prop passed to the hook (what the remote-lock
    // guard above reads) does not — the two can be one tick apart in a live
    // session. This isolates the strip from that guard: the call proceeds
    // (nothing here is remote-locked from the hook's point of view), and the
    // marker must still not land on the new id, which nothing has claimed.
    const sourceData = { text: 'x', remoteSelection: { color: '#f00', displayName: 'Ada' } };
    hoisted.nodes = [{ id: 'x1', type: 'label', position: { x: 0, y: 0 }, data: sourceData }];
    render(<Harness data={{ text: 'x' }} />);
    fireEvent.click(screen.getByText('go'));
    const updated = hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes);
    const copy = updated.find((n) => n.id !== 'x1');
    expect(copy.data.remoteSelection).toBeUndefined();
  });
});
