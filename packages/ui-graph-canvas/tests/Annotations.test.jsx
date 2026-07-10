import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import NoteNode from '../src/components/NoteNode';
import LabelNode from '../src/components/LabelNode';
import ArrowNode from '../src/components/ArrowNode';
import { AnnotationContext } from '../src/components/AnnotationContext';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

vi.mock('reactflow', () => ({
  NodeResizer: () => <div data-testid="resizer" />,
  useReactFlow: () => ({ setNodes: hoisted.setNodes }),
}));

const labels = { color: 'Colour', delete: 'Delete', notePlaceholder: 'Note', labelPlaceholder: 'Label' };

function renderWithContext(ui, notifyChange = vi.fn()) {
  return {
    notifyChange,
    ...render(
      <AnnotationContext.Provider value={{ notifyChange, labels }}>
        {ui}
      </AnnotationContext.Provider>
    ),
  };
}

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

  it('commits edited text and notifies on blur', () => {
    const { notifyChange } = renderWithContext(<NoteNode id="note-1" data={{ text: '' }} selected />);
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
    const { notifyChange } = renderWithContext(<NoteNode id="note-1" data={{ text: 'x' }} selected />);
    fireEvent.contextMenu(screen.getByText('x'));
    fireEvent.click(screen.getByRole('button', { name: /delete/i }));
    expect(notifyChange).toHaveBeenCalledTimes(1);
    const updater = hoisted.setNodes.mock.calls[0][0];
    expect(updater([{ id: 'note-1' }, { id: 'other' }])).toEqual([{ id: 'other' }]);
  });
});

describe('LabelNode', () => {
  beforeEach(() => vi.clearAllMocks());

  it('commits edited text on Enter', () => {
    const { notifyChange } = renderWithContext(<LabelNode id="label-1" data={{ text: '' }} selected />);
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
    const { notifyChange } = renderWithContext(<LabelNode id="label-1" data={{ text: 'x' }} selected />);
    fireEvent.contextMenu(screen.getByText('x'));
    const colorButtons = document.querySelectorAll('.color-button');
    expect(colorButtons.length).toBeGreaterThan(0);
    fireEvent.click(colorButtons[1]);
    expect(hoisted.setNodes).toHaveBeenCalledTimes(1);
    expect(notifyChange).toHaveBeenCalledTimes(1);
  });
});

describe('ArrowNode', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders an arrow line', () => {
    const { container } = renderWithContext(<ArrowNode id="arrow-1" data={{ dx: 160, dy: 0 }} selected={false} />);
    expect(container.querySelectorAll('line').length).toBeGreaterThan(0);
  });

  it('deletes itself and notifies from the context menu', () => {
    const { notifyChange } = renderWithContext(<ArrowNode id="arrow-1" data={{}} selected />);
    fireEvent.contextMenu(document.querySelector('.graph-arrow-node'));
    fireEvent.click(screen.getByRole('button', { name: /delete/i }));
    expect(notifyChange).toHaveBeenCalledTimes(1);
    const updater = hoisted.setNodes.mock.calls[0][0];
    expect(updater([{ id: 'arrow-1' }, { id: 'keep' }])).toEqual([{ id: 'keep' }]);
  });
});
