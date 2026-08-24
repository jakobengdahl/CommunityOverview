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
