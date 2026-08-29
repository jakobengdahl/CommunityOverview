import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import NoteNode from '../src/components/NoteNode';
import { AnnotationContext } from '../src/components/AnnotationContext';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn(), resizerProps: [], nodes: [] }));

vi.mock('reactflow', () => ({
  NodeResizer: (props) => {
    hoisted.resizerProps.push(props);
    return <div data-testid="resizer" />;
  },
  useReactFlow: () => ({ setNodes: hoisted.setNodes, getNodes: () => hoisted.nodes }),
}));

function applyUpdate(node) {
  const call = hoisted.setNodes.mock.calls.at(-1);
  return call[0]([node]);
}

// task-annotation-render-direct-manipulation: a right-click rotation control
// (docs/ANNOTATION_CONTRACT.md's capability baseline names sticky notes among
// the rotatable kinds; previously there was no GUI control at all for it).
describe('NoteNode rotation control', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.resizerProps.length = 0;
  });

  it('opens a context menu with rotation controls on right-click', () => {
    render(<NoteNode id="n1" data={{ rotation: 0 }} selected={false} />);
    fireEvent.contextMenu(screen.getByText('Note'));
    expect(screen.getByLabelText('Rotate left 15°')).toBeInTheDocument();
    expect(screen.getByLabelText('Rotate right 15°')).toBeInTheDocument();
    expect(screen.getByText('0°')).toBeInTheDocument();
  });

  it('rotates right by 15° and normalizes into [0, 360)', () => {
    render(<NoteNode id="n1" data={{ rotation: 350 }} selected={false} />);
    fireEvent.contextMenu(screen.getByText('Note'));
    fireEvent.click(screen.getByLabelText('Rotate right 15°'));
    const [updated] = applyUpdate({ id: 'n1', data: { rotation: 350 } });
    expect(updated.data.rotation).toBe(5);
  });

  it('resets rotation to 0 via the reset button', () => {
    render(<NoteNode id="n1" data={{ rotation: 45 }} selected={false} />);
    fireEvent.contextMenu(screen.getByText('Note'));
    fireEvent.click(screen.getByLabelText('Reset rotation'));
    const [updated] = applyUpdate({ id: 'n1', data: { rotation: 45 } });
    expect(updated.data.rotation).toBe(0);
  });

  it('notifies the annotation context after a rotation change', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{
          notifyChange,
          labels: {
            rotateReset: 'Reset rotation',
            delete: 'Delete',
            color: 'Colour',
            notePlaceholder: 'Note',
          },
        }}
      >
        <NoteNode id="n1" data={{ rotation: 0 }} selected={false} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByText('Note'));
    fireEvent.click(screen.getByLabelText('Reset rotation'));
    expect(notifyChange).toHaveBeenCalledTimes(1);
  });
});

// smallfix-notenode-resizer-ignores-locked: NodeResizer's isVisible was
// `selected` only, unlike GenericAnnotationNode/GroupNode which both gate on
// `&& !locked` — a locked note still showed resize handles and could be
// resized even though drag was already correctly blocked.
describe('NoteNode resize handles respect the locked flag', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.resizerProps.length = 0;
  });

  it('hides resize handles for a locked, selected note', () => {
    render(<NoteNode id="n1" data={{ locked: true }} selected />);
    expect(hoisted.resizerProps.at(-1).isVisible).toBe(false);
  });

  it('shows resize handles for an unlocked, selected note', () => {
    render(<NoteNode id="n1" data={{ locked: false }} selected />);
    expect(hoisted.resizerProps.at(-1).isVisible).toBe(true);
  });

  it('hides resize handles for an unselected note regardless of lock', () => {
    render(<NoteNode id="n1" data={{ locked: false }} selected={false} />);
    expect(hoisted.resizerProps.at(-1).isVisible).toBe(false);
  });
});

// smallfix-annotation-rotated-resize-handles: a rotated note must grow along
// its own axes, not the canvas's — see resolveRotatedResizeGeometry.test.js
// for the underlying math.
describe('NoteNode rotation-aware resize', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.resizerProps.length = 0;
  });

  it('rotates the resize handles along with the note', () => {
    const { container } = render(<NoteNode id="n1" data={{ rotation: 30 }} selected />);
    const wrap = container.querySelector('.graph-annotation-rotate-wrap');
    expect(wrap.style.transform).toBe('rotate(30deg)');
    expect(wrap.contains(screen.getByTestId('resizer'))).toBe(true);
  });

  it('remaps a resize gesture on a rotated note through its rotation', () => {
    render(<NoteNode id="n1" data={{ rotation: 90 }} selected />);
    const props = hoisted.resizerProps.at(-1);
    props.onResizeStart(null, { x: 0, y: 0, width: 100, height: 50 });
    props.onResizeEnd(null, { x: 0, y: 0, width: 150, height: 80 });
    const [updated] = applyUpdate({
      id: 'n1',
      position: { x: 0, y: 0 },
      style: { width: 100, height: 50 },
    });
    expect(updated.position.x).toBeCloseTo(-40, 9);
    expect(updated.position.y).toBeCloseTo(10, 9);
    expect(updated.style.width).toBe(150);
    expect(updated.style.height).toBe(80);
  });

  it('notifies the annotation context after a resize with no rotation change needed', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider value={{ notifyChange, labels: { notePlaceholder: 'Note' } }}>
        <NoteNode id="n1" data={{}} selected />
      </AnnotationContext.Provider>
    );
    hoisted.resizerProps.at(-1).onResizeEnd();
    expect(notifyChange).toHaveBeenCalledTimes(1);
  });
});

// smallfix-annotation-context-menus-ignore-lock: the accepted capability
// baseline is "a locked object remains selectable but offers only unlock or
// copy".
describe('NoteNode locked context menu', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.resizerProps.length = 0;
  });

  it('shows only an unlock action for a locked note, hiding colour/size/rotation/delete', () => {
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), labels: { unlock: 'Unlock', notePlaceholder: 'Note' } }}
      >
        <NoteNode id="n1" data={{ locked: true }} selected={false} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByText('Note'));
    expect(screen.getByText(/Unlock/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Rotate left 15°')).toBeNull();
    expect(screen.queryByText(/Delete/)).toBeNull();
  });

  it('unlocks a locked note, notifies the annotation context, and makes it draggable again', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{ notifyChange, labels: { unlock: 'Unlock', notePlaceholder: 'Note' } }}
      >
        <NoteNode id="n1" data={{ locked: true }} selected={false} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByText('Note'));
    fireEvent.click(screen.getByText(/Unlock/));
    const [updated] = applyUpdate({ id: 'n1', data: { locked: true }, draggable: false });
    expect(updated.data.locked).toBe(false);
    expect(updated.draggable).toBe(true);
    expect(notifyChange).toHaveBeenCalledWith('style');
  });

  it('still shows the full context menu when unlocked', () => {
    render(<NoteNode id="n1" data={{ locked: false }} selected={false} />);
    fireEvent.contextMenu(screen.getByText('Note'));
    expect(screen.getByLabelText('Rotate left 15°')).toBeInTheDocument();
    expect(screen.getByText(/Delete/)).toBeInTheDocument();
  });
});

// smallfix-annotation-colour-swatch-no-active-marker: the swatch grid had no
// indication of the note's current colour at all, unlike
// FreehandAnnotationNode's picker (FreehandAnnotationNode.jsx:298).
describe('NoteNode colour swatches', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.resizerProps.length = 0;
  });

  it('marks the swatch matching the current colour as active, and no other', () => {
    render(<NoteNode id="n1" data={{ color: '#86EFAC' }} selected={false} />);
    fireEvent.contextMenu(screen.getByText('Note'));

    const buttons = [...document.querySelectorAll('.color-button')];
    const active = buttons.find((b) => b.style.backgroundColor === 'rgb(134, 239, 172)');
    const inactive = buttons.find((b) => b.style.backgroundColor === 'rgb(254, 240, 138)');
    expect(active.className).toContain('active');
    expect(inactive.className).not.toContain('active');
  });
});

// task-shared-editable-text-hook: NoteNode's double-click/blur/Escape/live-sync
// text editing now runs through the shared useEditableText hook
// (packages/ui-graph-canvas/src/hooks/useEditableText.js) rather than its own
// copy of the state machine. These pin the same behaviour
// GenericAnnotationNode.test.jsx's "inline text editing" describe block pins
// for its `text`/`shape` kinds, so a regression in the shared hook is caught
// here too.
describe('NoteNode inline text editing', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
  });

  it('enters edit mode on double-click, showing a textarea seeded with the current text', () => {
    render(<NoteNode id="n1" data={{ text: 'Hello' }} selected={false} />);
    fireEvent.doubleClick(screen.getByText('Hello'));
    expect(screen.getByRole('textbox')).toHaveValue('Hello');
  });

  it('syncs every keystroke live and commits the trimmed value on blur', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider value={{ notifyChange, labels: { notePlaceholder: 'Note' } }}>
        <NoteNode id="n1" data={{ text: 'Hello' }} selected={false} />
      </AnnotationContext.Provider>
    );
    fireEvent.doubleClick(screen.getByText('Hello'));
    const textarea = screen.getByRole('textbox');
    fireEvent.change(textarea, { target: { value: 'typing' } });
    const [afterKeystroke] = applyUpdate({ id: 'n1', data: { text: 'Hello' } });
    expect(afterKeystroke.data.text).toBe('typing');
    expect(notifyChange).toHaveBeenCalledWith('text');

    fireEvent.change(textarea, { target: { value: '  world  ' } });
    fireEvent.blur(textarea);
    const [afterCommit] = applyUpdate({ id: 'n1', data: { text: 'Hello' } });
    expect(afterCommit.data.text).toBe('world');
  });

  it('cancels the edit on Escape, reverting to the stored text without writing it', () => {
    render(<NoteNode id="n1" data={{ text: 'Hello' }} selected={false} />);
    fireEvent.doubleClick(screen.getByText('Hello'));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'discard me' } });
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' });
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.getByText('Hello')).toBeInTheDocument();
  });

  it('does not commit on Enter — a note is a multi-line textarea', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider value={{ notifyChange, labels: { notePlaceholder: 'Note' } }}>
        <NoteNode id="n1" data={{ text: 'Hello' }} selected={false} />
      </AnnotationContext.Provider>
    );
    fireEvent.doubleClick(screen.getByText('Hello'));
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
    expect(screen.getByRole('textbox')).toBeInTheDocument();
    expect(notifyChange).not.toHaveBeenCalledWith('text');
  });

  it('refuses to enter edit mode while another client holds the selection claim', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{
          notifyChange: vi.fn(),
          notifyRemoteLockedAttempt,
          labels: { notePlaceholder: 'Note' },
        }}
      >
        <NoteNode
          id="n1"
          data={{ text: 'Hello', remoteSelection: { color: '#f00', displayName: 'Ada' } }}
          selected={false}
        />
      </AnnotationContext.Provider>
    );
    fireEvent.doubleClick(screen.getByText('Hello'));
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
  });

  // smallfix-locked-annotation-text-still-editable-by-doubleclick: the persisted lock gates every
  // context menu already; the double-click editor was the one path around it.
  it('refuses to enter edit mode while locked', () => {
    render(<NoteNode id="n1" data={{ text: 'Hello', locked: true }} selected={false} />);
    fireEvent.doubleClick(screen.getByText('Hello'));
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('closes the editor and discards the pending edit when a lock arrives mid-edit', () => {
    const notifyChange = vi.fn();
    const { rerender } = render(
      <AnnotationContext.Provider value={{ notifyChange, labels: { notePlaceholder: 'Note' } }}>
        <NoteNode id="n1" data={{ text: 'Hello' }} selected={false} />
      </AnnotationContext.Provider>
    );
    fireEvent.doubleClick(screen.getByText('Hello'));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'typed before lock' } });
    hoisted.setNodes.mockClear();
    notifyChange.mockClear();
    rerender(
      <AnnotationContext.Provider value={{ notifyChange, labels: { notePlaceholder: 'Note' } }}>
        <NoteNode id="n1" data={{ text: 'Hello', locked: true }} selected={false} />
      </AnnotationContext.Provider>
    );
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.getByText('Hello')).toBeInTheDocument();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });
});

// The layer row is shared by every annotation context menu
// (AnnotationLayerControls); GenericAnnotationNode.test.jsx pins the generic
// kinds' wiring, this pins that a dedicated per-type editor gets the same
// control rather than a parallel implementation of its own.
describe('NoteNode layer controls', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.nodes = [];
  });

  it('brings a note in front of the annotation above it', () => {
    hoisted.nodes = [
      { id: 'n1', type: 'note', zIndex: 0 },
      { id: 'a2', type: 'label', zIndex: 1 },
    ];
    render(<NoteNode id="n1" data={{ text: 'hi' }} />);
    fireEvent.contextMenu(screen.getByText('hi'));
    fireEvent.click(screen.getByLabelText('Bring to front'));
    expect(hoisted.setNodes.mock.calls.at(-1)[0](hoisted.nodes)[0].zIndex).toBe(2);
  });

  it('offers no layer controls on a locked note', () => {
    render(<NoteNode id="n1" data={{ text: 'hi', locked: true }} />);
    fireEvent.contextMenu(screen.getByText('hi'));
    expect(screen.queryByLabelText('Bring to front')).toBeNull();
  });
});
