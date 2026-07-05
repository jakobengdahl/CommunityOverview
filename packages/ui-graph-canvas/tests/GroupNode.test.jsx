import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import GroupNode from '../src/components/GroupNode';
import { AnnotationContext } from '../src/components/AnnotationContext';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

vi.mock('reactflow', () => ({
  // Expose onResizeEnd via a clickable stub so the test can fire a resize.
  NodeResizer: ({ onResizeEnd }) => (
    <button data-testid="resize" onClick={() => onResizeEnd?.()} />
  ),
  useReactFlow: () => ({ setNodes: hoisted.setNodes }),
}));

function renderGroup(data = { label: 'G', color: '#646cff' }, notifyChange = vi.fn()) {
  return {
    notifyChange,
    ...render(
      <AnnotationContext.Provider value={{ notifyChange, labels: {} }}>
        <GroupNode id="group-1" data={data} selected />
      </AnnotationContext.Provider>
    ),
  };
}

describe('GroupNode host notification (design step 6)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('notifies on rename and writes the new label', () => {
    const { notifyChange } = renderGroup();
    fireEvent.doubleClick(screen.getByText('G'));
    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'Team map' } });
    fireEvent.blur(input);
    expect(notifyChange).toHaveBeenCalledTimes(1);
    const updater = hoisted.setNodes.mock.calls.at(-1)[0];
    expect(updater([{ id: 'group-1', data: { label: 'G' } }])[0].data.label).toBe('Team map');
  });

  it('does not notify when the label is unchanged', () => {
    const { notifyChange } = renderGroup();
    fireEvent.doubleClick(screen.getByText('G'));
    const input = screen.getByRole('textbox');
    fireEvent.blur(input); // committed unchanged
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('notifies on colour change', () => {
    const { notifyChange } = renderGroup();
    fireEvent.contextMenu(screen.getByText('G'));
    // The colour menu renders through a portal to document.body.
    const colorButtons = document.querySelectorAll('.color-button');
    expect(colorButtons.length).toBeGreaterThan(0);
    fireEvent.click(colorButtons[1]);
    expect(notifyChange).toHaveBeenCalledTimes(1);
  });

  it('notifies on delete', () => {
    const { notifyChange } = renderGroup();
    fireEvent.contextMenu(screen.getByText('G'));
    fireEvent.click(screen.getByRole('button', { name: /delete group/i }));
    expect(notifyChange).toHaveBeenCalledTimes(1);
  });

  it('notifies on resize end', () => {
    const { notifyChange } = renderGroup();
    fireEvent.click(screen.getByTestId('resize'));
    expect(notifyChange).toHaveBeenCalledTimes(1);
  });
});
