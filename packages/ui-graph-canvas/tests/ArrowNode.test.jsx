import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import ArrowNode from '../src/components/ArrowNode';
import { AnnotationContext } from '../src/components/AnnotationContext';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn(), nodes: [] }));

vi.mock('reactflow', () => ({
  useReactFlow: () => ({
    setNodes: hoisted.setNodes,
    getNodes: () => hoisted.nodes,
    screenToFlowPosition: (p) => p,
  }),
}));

function openMenu() {
  fireEvent.contextMenu(document.querySelector('.graph-arrow-node'));
}

// The capability baseline (docs/ANNOTATION_CONTRACT.md) is that a locked
// object "remains selectable but offers only unlock or copy". NoteNode,
// LabelNode, GenericAnnotationNode and FreehandAnnotationNode have all
// rendered a `locked ? <unlock only> : <full menu>` branch for some time;
// ArrowNode was the one kind that did not, so a locked line still offered
// its colour swatches, both arrowhead toggles and Delete — and, having no
// unlock button either, could not be unlocked from the GUI at all.
describe('ArrowNode locked context menu', () => {
  beforeEach(() => {
    hoisted.setNodes.mockClear();
    hoisted.nodes = [];
  });

  const lockedData = { dx: 100, dy: 0, locked: true };

  it('offers only unlock when the line is locked', () => {
    render(<ArrowNode id="a1" type="arrow" data={lockedData} selected={false} />);
    openMenu();
    expect(screen.getByText(/Unlock/)).toBeInTheDocument();
    expect(screen.queryByText(/Delete/)).toBeNull();
    expect(screen.queryByText('Start arrowhead')).toBeNull();
    expect(screen.queryByText('End arrowhead')).toBeNull();
    expect(document.querySelector('.context-menu-colors')).toBeNull();
  });

  it('unlocks the line and keeps it draggable again', () => {
    render(<ArrowNode id="a1" type="arrow" data={lockedData} selected={false} />);
    openMenu();
    fireEvent.click(screen.getByText(/Unlock/));
    const updated = hoisted.setNodes.mock.calls.at(-1)[0]([
      { id: 'a1', data: lockedData, draggable: false },
    ])[0];
    expect(updated.data.locked).toBe(false);
    // patchData recomputes draggability from the new data, so an unlocked,
    // unanchored line becomes draggable in the same update.
    expect(updated.draggable).toBe(true);
  });

  it('offers the full menu when the line is not locked', () => {
    render(<ArrowNode id="a1" type="arrow" data={{ dx: 100, dy: 0 }} selected={false} />);
    openMenu();
    expect(screen.queryByText(/Unlock/)).toBeNull();
    expect(screen.getByText(/Delete/)).toBeInTheDocument();
    expect(document.querySelector('.context-menu-colors')).toBeTruthy();
  });

  it('surfaces the attempt instead of unlocking while another client holds the claim', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt, labels: { unlock: 'Unlock' } }}
      >
        <ArrowNode
          id="a1"
          type="arrow"
          data={{ ...lockedData, remoteSelection: { color: '#f00', displayName: 'Ada' } }}
          selected={false}
        />
      </AnnotationContext.Provider>
    );
    openMenu();
    fireEvent.click(screen.getByText(/Unlock/));
    expect(notifyRemoteLockedAttempt).toHaveBeenCalled();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
  });
});
