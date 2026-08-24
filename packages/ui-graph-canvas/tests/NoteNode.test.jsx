import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import NoteNode from '../src/components/NoteNode';
import { AnnotationContext } from '../src/components/AnnotationContext';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

vi.mock('reactflow', () => ({
  NodeResizer: () => null,
  useReactFlow: () => ({ setNodes: hoisted.setNodes }),
}));

// task-annotation-render-direct-manipulation: a right-click rotation control
// (docs/ANNOTATION_CONTRACT.md's capability baseline names sticky notes among
// the rotatable kinds; previously there was no GUI control at all for it).
describe('NoteNode rotation control', () => {
  beforeEach(() => hoisted.setNodes.mockClear());

  function applyUpdate(node) {
    const call = hoisted.setNodes.mock.calls.at(-1);
    return call[0]([node]);
  }

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
