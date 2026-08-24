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

  it('unlocks a locked label and notifies the annotation context', () => {
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
    const [updated] = call[0]([{ id: 'l1', data: { locked: true } }]);
    expect(updated.data.locked).toBe(false);
    expect(notifyChange).toHaveBeenCalledWith('style');
  });

  it('still shows the full context menu when unlocked', () => {
    render(<LabelNode id="l1" data={{ locked: false }} selected={false} />);
    fireEvent.contextMenu(screen.getByText('Label'));
    expect(screen.getByLabelText('Rotate left 15°')).toBeInTheDocument();
    expect(screen.getByText(/Delete/)).toBeInTheDocument();
  });
});
