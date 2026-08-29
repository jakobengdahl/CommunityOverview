import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import LabelNode from '../src/components/LabelNode';
import { AnnotationContext } from '../src/components/AnnotationContext';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

vi.mock('reactflow', () => ({
  useReactFlow: () => ({ setNodes: hoisted.setNodes }),
}));

// task-annotation-render-direct-manipulation: a right-click rotation control
// (docs/ANNOTATION_CONTRACT.md's capability baseline names labels/callouts
// among the rotatable kinds; previously there was no GUI control at all for
// it — see NoteNode.test.jsx for the sticky-note counterpart).
describe('LabelNode rotation control', () => {
  beforeEach(() => hoisted.setNodes.mockClear());

  function applyUpdate(node) {
    const call = hoisted.setNodes.mock.calls.at(-1);
    return call[0]([node]);
  }

  it('opens a context menu with rotation controls on right-click', () => {
    render(<LabelNode id="l1" data={{ rotation: 0 }} selected={false} />);
    fireEvent.contextMenu(screen.getByText('Label'));
    expect(screen.getByLabelText('Rotate left 15°')).toBeInTheDocument();
    expect(screen.getByLabelText('Rotate right 15°')).toBeInTheDocument();
  });

  it('rotates left by 15° and normalizes into [0, 360)', () => {
    render(<LabelNode id="l1" data={{ rotation: 5 }} selected={false} />);
    fireEvent.contextMenu(screen.getByText('Label'));
    fireEvent.click(screen.getByLabelText('Rotate left 15°'));
    const [updated] = applyUpdate({ id: 'l1', data: { rotation: 5 } });
    expect(updated.data.rotation).toBe(350);
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
            labelPlaceholder: 'Label',
          },
        }}
      >
        <LabelNode id="l1" data={{ rotation: 0 }} selected={false} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByText('Label'));
    fireEvent.click(screen.getByLabelText('Reset rotation'));
    expect(notifyChange).toHaveBeenCalledTimes(1);
  });
});

// task-shared-editable-text-hook: LabelNode's double-click/blur/Escape/
// live-sync/Enter-commits text editing now runs through the shared
// useEditableText hook (packages/ui-graph-canvas/src/hooks/useEditableText.js)
// rather than its own copy of the state machine — see NoteNode.test.jsx's
// "inline text editing" describe block for the counterpart on the
// `<textarea>` components, where Enter does *not* commit.
describe('LabelNode inline text editing', () => {
  beforeEach(() => hoisted.setNodes.mockClear());

  function applyUpdate(node) {
    const call = hoisted.setNodes.mock.calls.at(-1);
    return call[0]([node])[0];
  }

  it('enters edit mode on double-click, showing an input seeded with the current text', () => {
    render(<LabelNode id="l1" data={{ text: 'Hello' }} selected={false} />);
    fireEvent.doubleClick(screen.getByText('Hello'));
    expect(screen.getByRole('textbox')).toHaveValue('Hello');
  });

  it('syncs every keystroke live and commits the trimmed value on blur', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider value={{ notifyChange, labels: { labelPlaceholder: 'Label' } }}>
        <LabelNode id="l1" data={{ text: 'Hello' }} selected={false} />
      </AnnotationContext.Provider>
    );
    fireEvent.doubleClick(screen.getByText('Hello'));
    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'typing' } });
    expect(applyUpdate({ id: 'l1', data: { text: 'Hello' } }).data.text).toBe('typing');
    expect(notifyChange).toHaveBeenCalledWith('text');

    fireEvent.change(input, { target: { value: '  world  ' } });
    fireEvent.blur(input);
    expect(applyUpdate({ id: 'l1', data: { text: 'Hello' } }).data.text).toBe('world');
  });

  it('commits the trimmed value on Enter — a label is a single-line input', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider value={{ notifyChange, labels: { labelPlaceholder: 'Label' } }}>
        <LabelNode id="l1" data={{ text: 'Hello' }} selected={false} />
      </AnnotationContext.Provider>
    );
    fireEvent.doubleClick(screen.getByText('Hello'));
    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: '  world  ' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(applyUpdate({ id: 'l1', data: { text: 'Hello' } }).data.text).toBe('world');
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('cancels the edit on Escape, reverting to the stored text without writing it', () => {
    render(<LabelNode id="l1" data={{ text: 'Hello' }} selected={false} />);
    fireEvent.doubleClick(screen.getByText('Hello'));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'discard me' } });
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' });
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.getByText('Hello')).toBeInTheDocument();
  });

  it('refuses to enter edit mode while another client holds the selection claim', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{
          notifyChange: vi.fn(),
          notifyRemoteLockedAttempt,
          labels: { labelPlaceholder: 'Label' },
        }}
      >
        <LabelNode
          id="l1"
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
    render(<LabelNode id="l1" data={{ text: 'Hello', locked: true }} selected={false} />);
    fireEvent.doubleClick(screen.getByText('Hello'));
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('closes the editor and discards the pending edit when a lock arrives mid-edit', () => {
    const notifyChange = vi.fn();
    const { rerender } = render(
      <AnnotationContext.Provider value={{ notifyChange, labels: { labelPlaceholder: 'Label' } }}>
        <LabelNode id="l1" data={{ text: 'Hello' }} selected={false} />
      </AnnotationContext.Provider>
    );
    fireEvent.doubleClick(screen.getByText('Hello'));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'typed before lock' } });
    hoisted.setNodes.mockClear();
    notifyChange.mockClear();
    rerender(
      <AnnotationContext.Provider value={{ notifyChange, labels: { labelPlaceholder: 'Label' } }}>
        <LabelNode id="l1" data={{ text: 'Hello', locked: true }} selected={false} />
      </AnnotationContext.Provider>
    );
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.getByText('Hello')).toBeInTheDocument();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });
});

describe('LabelNode colour defaults', () => {
  beforeEach(() => hoisted.setNodes.mockClear());

  it('uses a neutral fallback colour when no label colour is stored', () => {
    const { container } = render(
      <LabelNode id="l1" data={{ text: 'Label text' }} selected={false} />
    );
    expect(container.querySelector('.graph-label-node').style.color).toBe('rgb(100, 116, 139)');
  });

  it('keeps the near-white colour available as a selectable swatch', () => {
    render(<LabelNode id="l1" data={{ text: 'Label text' }} selected={false} />);
    fireEvent.contextMenu(screen.getByText('Label text'));

    const swatches = [...document.querySelectorAll('.color-button')].map(
      (button) => button.style.backgroundColor
    );
    expect(swatches).toContain('rgb(230, 237, 243)');
  });

  // smallfix-annotation-colour-swatch-no-active-marker: the swatch grid had
  // no indication of the label's current colour at all, unlike
  // FreehandAnnotationNode's picker (FreehandAnnotationNode.jsx:298).
  it('marks the swatch matching the current colour as active, and no other', () => {
    render(<LabelNode id="l1" data={{ text: 'Label text', color: '#FDE047' }} selected={false} />);
    fireEvent.contextMenu(screen.getByText('Label text'));

    const buttons = [...document.querySelectorAll('.color-button')];
    const active = buttons.find((b) => b.style.backgroundColor === 'rgb(253, 224, 71)');
    const inactive = buttons.find((b) => b.style.backgroundColor === 'rgb(230, 237, 243)');
    expect(active.className).toContain('active');
    expect(inactive.className).not.toContain('active');
  });
});

// smallfix-annotation-context-menus-ignore-lock: the accepted capability
// baseline is "a locked object remains selectable but offers only unlock or
// copy".
describe('LabelNode locked context menu', () => {
  beforeEach(() => hoisted.setNodes.mockClear());

  it('shows only an unlock action for a locked label, hiding colour/size/rotation/delete', () => {
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), labels: { unlock: 'Unlock', labelPlaceholder: 'Label' } }}
      >
        <LabelNode id="l1" data={{ locked: true }} selected={false} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByText('Label'));
    expect(screen.getByText(/Unlock/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Rotate left 15°')).toBeNull();
    expect(screen.queryByText(/Delete/)).toBeNull();
  });

  it('unlocks a locked label, notifies the annotation context, and makes it draggable again', () => {
    const notifyChange = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{ notifyChange, labels: { unlock: 'Unlock', labelPlaceholder: 'Label' } }}
      >
        <LabelNode id="l1" data={{ locked: true }} selected={false} />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(screen.getByText('Label'));
    fireEvent.click(screen.getByText(/Unlock/));
    const call = hoisted.setNodes.mock.calls.at(-1);
    const [updated] = call[0]([{ id: 'l1', data: { locked: true }, draggable: false }]);
    expect(updated.data.locked).toBe(false);
    expect(updated.draggable).toBe(true);
    expect(notifyChange).toHaveBeenCalledWith('style');
  });

  it('still shows the full context menu when unlocked', () => {
    render(<LabelNode id="l1" data={{ locked: false }} selected={false} />);
    fireEvent.contextMenu(screen.getByText('Label'));
    expect(screen.getByLabelText('Rotate left 15°')).toBeInTheDocument();
    expect(screen.getByText(/Delete/)).toBeInTheDocument();
  });
});
