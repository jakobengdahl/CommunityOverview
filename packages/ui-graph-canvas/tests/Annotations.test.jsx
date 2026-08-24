import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import NoteNode from '../src/components/NoteNode';
import LabelNode from '../src/components/LabelNode';
import ArrowNode from '../src/components/ArrowNode';
import { AnnotationContext } from '../src/components/AnnotationContext';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

vi.mock('reactflow', () => ({
  NodeResizer: () => <div data-testid="resizer" />,
  useReactFlow: () => ({
    setNodes: hoisted.setNodes,
    screenToFlowPosition: (p) => p,
    getNodes: () => [],
  }),
}));

const labels = {
  color: 'Colour',
  delete: 'Delete',
  notePlaceholder: 'Note',
  labelPlaceholder: 'Label',
  textSize: 'Text size',
  arrowStartHead: 'Start arrowhead',
  arrowEndHead: 'End arrowhead',
};

function renderWithContext(ui, notifyChange = vi.fn(), notifyRemoteLockedAttempt = vi.fn()) {
  return {
    notifyChange,
    notifyRemoteLockedAttempt,
    ...render(
      <AnnotationContext.Provider value={{ notifyChange, notifyRemoteLockedAttempt, labels }}>
        {ui}
      </AnnotationContext.Provider>
    ),
  };
}

// A live claim held by another client (task-annotation-shared-session-realtime).
const REMOTE_CLAIM = { clientId: 'c2', color: '#e6194b', displayName: 'Ada' };

describe('NoteNode', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows the placeholder when empty and the text when set', () => {
    const { rerender } = renderWithContext(<NoteNode id="note-1" data={{}} selected={false} />);
    expect(screen.getByText('Note')).toBeInTheDocument();
    rerender(
      <AnnotationContext.Provider value={{ notifyChange: vi.fn(), labels }}>
        <NoteNode id="note-1" data={{ text: 'Hello' }} selected={false} />
      </AnnotationContext.Provider>
    );
    expect(screen.getByText('Hello')).toBeInTheDocument();
  });

  // Notes and labels render through their own components rather than
  // GenericAnnotationNode, so their rotation is wired separately and needs its
  // own coverage. A label's rotation is reachable today (the generic MCP tools
  // accept `label`); a note's is not - the sticky-note tools take no rotation
  // argument - so this pins the wiring ahead of the source that will set it.
  it('draws a rotation on the note body', () => {
    const { container } = renderWithContext(
      <NoteNode id="note-1" data={{ text: 'x', rotation: 30 }} selected={false} />
    );
    expect(container.querySelector('.graph-note-node').style.transform).toBe('rotate(30deg)');
  });

  it('leaves an unrotated note untransformed', () => {
    const { container } = renderWithContext(
      <NoteNode id="note-1" data={{ text: 'x' }} selected={false} />
    );
    expect(container.querySelector('.graph-note-node').style.transform).toBe('');
  });

  it('commits edited text and notifies on blur', () => {
    const { notifyChange } = renderWithContext(
      <NoteNode id="note-1" data={{ text: '' }} selected />
    );
    fireEvent.doubleClick(screen.getByText('Note'));
    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'Remember this' } });
    fireEvent.blur(input);
    expect(hoisted.setNodes).toHaveBeenCalledTimes(1);
    expect(notifyChange).toHaveBeenCalledTimes(1);
    // The updater sets the note text on the matching node.
    const updater = hoisted.setNodes.mock.calls[0][0];
    const result = updater([{ id: 'note-1', data: { text: '' } }]);
    expect(result[0].data.text).toBe('Remember this');
  });

  it('deletes itself and notifies from the context menu', () => {
    const { notifyChange } = renderWithContext(
      <NoteNode id="note-1" data={{ text: 'x' }} selected />
    );
    fireEvent.contextMenu(screen.getByText('x'));
    fireEvent.click(screen.getByRole('button', { name: /delete/i }));
    expect(notifyChange).toHaveBeenCalledTimes(1);
    const updater = hoisted.setNodes.mock.calls[0][0];
    expect(updater([{ id: 'note-1' }, { id: 'other' }])).toEqual([{ id: 'other' }]);
  });

  it('changes the note text size from the context menu', () => {
    const { notifyChange } = renderWithContext(
      <NoteNode id="note-1" data={{ text: 'x' }} selected />
    );
    fireEvent.contextMenu(screen.getByText('x'));
    const sizeButtons = document.querySelectorAll('.size-button');
    expect(sizeButtons.length).toBe(4);
    fireEvent.click(sizeButtons[3]);
    expect(notifyChange).toHaveBeenCalledTimes(1);
    const updater = hoisted.setNodes.mock.calls[0][0];
    expect(updater([{ id: 'note-1', data: { text: 'x' } }])[0].data.fontSize).toBe(24);
  });
});

describe('LabelNode', () => {
  beforeEach(() => vi.clearAllMocks());

  it('draws a rotation on the label', () => {
    const { container } = renderWithContext(
      <LabelNode id="label-1" data={{ text: 'x', rotation: -15 }} selected={false} />
    );
    expect(container.querySelector('.graph-label-node').style.transform).toBe('rotate(-15deg)');
  });

  it('leaves an unrotated label untransformed', () => {
    const { container } = renderWithContext(
      <LabelNode id="label-1" data={{ text: 'x' }} selected={false} />
    );
    expect(container.querySelector('.graph-label-node').style.transform).toBe('');
  });

  it('commits edited text on Enter', () => {
    const { notifyChange } = renderWithContext(
      <LabelNode id="label-1" data={{ text: '' }} selected />
    );
    fireEvent.doubleClick(screen.getByText('Label'));
    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'Region A' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(hoisted.setNodes).toHaveBeenCalledTimes(1);
    expect(notifyChange).toHaveBeenCalledTimes(1);
    const updater = hoisted.setNodes.mock.calls[0][0];
    expect(updater([{ id: 'label-1', data: {} }])[0].data.text).toBe('Region A');
  });

  it('recolours from the context menu', () => {
    const { notifyChange } = renderWithContext(
      <LabelNode id="label-1" data={{ text: 'x' }} selected />
    );
    fireEvent.contextMenu(screen.getByText('x'));
    const colorButtons = document.querySelectorAll('.color-button');
    expect(colorButtons.length).toBeGreaterThan(0);
    fireEvent.click(colorButtons[1]);
    expect(hoisted.setNodes).toHaveBeenCalledTimes(1);
    expect(notifyChange).toHaveBeenCalledTimes(1);
  });

  it('changes the label text size from the context menu', () => {
    const { notifyChange } = renderWithContext(
      <LabelNode id="label-1" data={{ text: 'x' }} selected />
    );
    fireEvent.contextMenu(screen.getByText('x'));
    const sizeButtons = document.querySelectorAll('.size-button');
    expect(sizeButtons.length).toBe(4);
    fireEvent.click(sizeButtons[2]);
    expect(notifyChange).toHaveBeenCalledTimes(1);
    const updater = hoisted.setNodes.mock.calls[0][0];
    expect(updater([{ id: 'label-1', data: { text: 'x' } }])[0].data.fontSize).toBe(20);
  });
});

describe('ArrowNode', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders an arrow line', () => {
    const { container } = renderWithContext(
      <ArrowNode id="arrow-1" data={{ dx: 160, dy: 0 }} selected={false} />
    );
    expect(container.querySelectorAll('line').length).toBeGreaterThan(0);
  });

  it('draws a head at the end by default, none at the start', () => {
    const { container } = renderWithContext(
      <ArrowNode id="arrow-1" data={{ dx: 160, dy: 0 }} selected={false} />
    );
    const visible = container.querySelectorAll('line')[1];
    expect(visible.getAttribute('marker-end')).toBe('url(#graph-arrow-head-arrow-1)');
    expect(visible.getAttribute('marker-start')).toBeNull();
  });

  it('renders a plain line when both heads are off', () => {
    const { container } = renderWithContext(
      <ArrowNode
        id="arrow-1"
        data={{ dx: 160, dy: 0, startArrow: false, endArrow: false }}
        selected={false}
      />
    );
    const visible = container.querySelectorAll('line')[1];
    expect(visible.getAttribute('marker-end')).toBeNull();
    expect(visible.getAttribute('marker-start')).toBeNull();
  });

  it('shows two endpoint handles when selected', () => {
    const { container } = renderWithContext(
      <ArrowNode id="arrow-1" data={{ dx: 160, dy: 0 }} selected />
    );
    expect(container.querySelectorAll('circle.graph-arrow-handle').length).toBe(2);
  });

  it('toggles the start head from the context menu', () => {
    const { notifyChange } = renderWithContext(
      <ArrowNode id="arrow-1" data={{ dx: 160, dy: 0, endArrow: true }} selected={false} />
    );
    fireEvent.contextMenu(document.querySelector('.graph-arrow-node'));
    const toggle = screen.getByText('Start arrowhead');
    fireEvent.click(toggle);
    expect(notifyChange).toHaveBeenCalledTimes(1);
    const updater = hoisted.setNodes.mock.calls[0][0];
    expect(updater([{ id: 'arrow-1', data: { startArrow: false } }])[0].data.startArrow).toBe(true);
  });

  it('deletes itself and notifies from the context menu', () => {
    const { notifyChange } = renderWithContext(<ArrowNode id="arrow-1" data={{}} selected />);
    fireEvent.contextMenu(document.querySelector('.graph-arrow-node'));
    fireEvent.click(screen.getByRole('button', { name: /delete/i }));
    expect(notifyChange).toHaveBeenCalledTimes(1);
    const updater = hoisted.setNodes.mock.calls[0][0];
    expect(updater([{ id: 'arrow-1' }, { id: 'keep' }])).toEqual([{ id: 'keep' }]);
  });

  it('hides the endpoint handles for a locked arrow even when selected', () => {
    const { container } = renderWithContext(
      <ArrowNode id="arrow-1" data={{ dx: 160, dy: 0, locked: true }} selected />
    );
    expect(container.querySelectorAll('circle.graph-arrow-handle').length).toBe(0);
  });

  it('keeps a locked arrow non-draggable after a style change (colour)', () => {
    const { notifyChange } = renderWithContext(
      <ArrowNode id="arrow-1" data={{ dx: 160, dy: 0, locked: true }} selected={false} />
    );
    fireEvent.contextMenu(document.querySelector('.graph-arrow-node'));
    const colorButtons = document.querySelectorAll('.color-button');
    fireEvent.click(colorButtons[1]);
    expect(notifyChange).toHaveBeenCalledTimes(1);
    const updater = hoisted.setNodes.mock.calls[0][0];
    const result = updater([
      { id: 'arrow-1', data: { dx: 160, dy: 0, locked: true }, draggable: false },
    ]);
    expect(result[0].data.locked).toBe(true);
    expect(result[0].draggable).toBe(false);
  });

  it('stays draggable after a style change on an unlocked, unanchored arrow', () => {
    renderWithContext(
      <ArrowNode id="arrow-1" data={{ dx: 160, dy: 0, locked: false }} selected={false} />
    );
    fireEvent.contextMenu(document.querySelector('.graph-arrow-node'));
    const colorButtons = document.querySelectorAll('.color-button');
    fireEvent.click(colorButtons[1]);
    const updater = hoisted.setNodes.mock.calls[0][0];
    const result = updater([
      { id: 'arrow-1', data: { dx: 160, dy: 0, locked: false }, draggable: true },
    ]);
    expect(result[0].draggable).toBe(true);
  });

  it('ignores an in-flight endpoint move once the arrow becomes locked', () => {
    renderWithContext(<ArrowNode id="arrow-1" data={{ dx: 160, dy: 0 }} selected />);
    const [startHandle] = document.querySelectorAll('circle.graph-arrow-handle');
    fireEvent.pointerDown(startHandle, { clientX: 0, clientY: 0 });
    const moveEvent = new MouseEvent('pointermove', {
      bubbles: true,
      cancelable: true,
      clientX: 20,
      clientY: 5,
    });
    window.dispatchEvent(moveEvent);
    expect(hoisted.setNodes).toHaveBeenCalledTimes(1);
    const updater = hoisted.setNodes.mock.calls[0][0];
    // Simulate a realtime lock landing between drag-start and this move: the
    // updater reads the fresh node from state, not the (now stale) render-time
    // `data` prop, and must refuse to touch its geometry.
    const lockedNode = {
      id: 'arrow-1',
      position: { x: 0, y: 0 },
      data: { dx: 160, dy: 0, locked: true },
    };
    const result = updater([lockedNode]);
    expect(result[0]).toBe(lockedNode);
  });
});

describe('remote selection claim exclusivity (task-annotation-shared-session-realtime)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('NoteNode: refuses a text commit while another client holds the claim', () => {
    const { notifyChange, notifyRemoteLockedAttempt } = renderWithContext(
      <NoteNode id="note-1" data={{ text: 'x', remoteSelection: REMOTE_CLAIM }} selected />
    );
    fireEvent.doubleClick(screen.getByText('x'));
    // Blocked at the double-click too: no textarea is offered to type into.
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
    expect(notifyChange).not.toHaveBeenCalled();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
  });

  it('NoteNode: refuses a colour change while another client holds the claim', () => {
    const { notifyChange, notifyRemoteLockedAttempt } = renderWithContext(
      <NoteNode id="note-1" data={{ text: 'x', remoteSelection: REMOTE_CLAIM }} selected />
    );
    fireEvent.contextMenu(screen.getByText('x'));
    fireEvent.click(document.querySelectorAll('.color-button')[0]);
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
    expect(notifyChange).not.toHaveBeenCalled();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
  });

  it('NoteNode: refuses delete while another client holds the claim', () => {
    const { notifyChange, notifyRemoteLockedAttempt } = renderWithContext(
      <NoteNode id="note-1" data={{ text: 'x', remoteSelection: REMOTE_CLAIM }} selected />
    );
    fireEvent.contextMenu(screen.getByText('x'));
    fireEvent.click(screen.getByRole('button', { name: /delete/i }));
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
    expect(notifyChange).not.toHaveBeenCalled();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
  });

  it('NoteNode: edits normally once no claim is present', () => {
    const { notifyChange } = renderWithContext(
      <NoteNode id="note-1" data={{ text: 'x' }} selected />
    );
    fireEvent.doubleClick(screen.getByText('x'));
    expect(screen.getByRole('textbox')).toBeInTheDocument();
    fireEvent.blur(screen.getByRole('textbox'));
    expect(notifyChange).toHaveBeenCalledWith('text');
  });

  it('LabelNode: refuses a text commit while another client holds the claim', () => {
    const { notifyChange, notifyRemoteLockedAttempt } = renderWithContext(
      <LabelNode id="label-1" data={{ text: 'x', remoteSelection: REMOTE_CLAIM }} selected />
    );
    fireEvent.doubleClick(screen.getByText('x'));
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('ArrowNode: refuses a colour change while another client holds the claim', () => {
    const { notifyChange, notifyRemoteLockedAttempt } = renderWithContext(
      <ArrowNode
        id="arrow-1"
        data={{ dx: 160, dy: 0, remoteSelection: REMOTE_CLAIM }}
        selected={false}
      />
    );
    fireEvent.contextMenu(document.querySelector('.graph-arrow-node'));
    fireEvent.click(document.querySelectorAll('.color-button')[0]);
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
    expect(notifyChange).not.toHaveBeenCalled();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
  });

  it('ArrowNode: hides endpoint handles while another client holds the claim, even when selected', () => {
    const { container } = renderWithContext(
      <ArrowNode id="arrow-1" data={{ dx: 160, dy: 0, remoteSelection: REMOTE_CLAIM }} selected />
    );
    expect(container.querySelectorAll('circle.graph-arrow-handle').length).toBe(0);
  });

  it('ArrowNode: ignores an in-flight endpoint move once a remote claim lands mid-drag', () => {
    // Mirrors the existing "becomes locked" case above: the drag starts while
    // unclaimed (the handle is present and armable), then a claim from
    // another client lands before the next move — moveEndpoint reads the
    // fresh node from state, not the stale render-time `data` prop, and must
    // refuse to touch its geometry.
    renderWithContext(<ArrowNode id="arrow-1" data={{ dx: 160, dy: 0 }} selected />);
    const [startHandle] = document.querySelectorAll('circle.graph-arrow-handle');
    fireEvent.pointerDown(startHandle, { clientX: 0, clientY: 0 });
    const moveEvent = new MouseEvent('pointermove', {
      bubbles: true,
      cancelable: true,
      clientX: 20,
      clientY: 5,
    });
    window.dispatchEvent(moveEvent);
    expect(hoisted.setNodes).toHaveBeenCalledTimes(1);
    const updater = hoisted.setNodes.mock.calls[0][0];
    const remotelyLockedNode = {
      id: 'arrow-1',
      position: { x: 0, y: 0 },
      data: { dx: 160, dy: 0, remoteSelection: REMOTE_CLAIM },
    };
    const result = updater([remotelyLockedNode]);
    expect(result[0]).toBe(remotelyLockedNode);
  });
});
