import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import GroupNode from '../src/components/GroupNode';
import { AnnotationContext } from '../src/components/AnnotationContext';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

vi.mock('reactflow', () => ({
  // Expose onResizeEnd via a clickable stub so the test can fire a resize.
  // `isVisible` is honoured because it is what withholds the handles from a
  // locked group; the real NodeResizer renders nothing when it is false.
  NodeResizer: ({ onResizeEnd, isVisible }) =>
    isVisible ? <button data-testid="resize" onClick={() => onResizeEnd?.()} /> : null,
  useReactFlow: () => ({ setNodes: hoisted.setNodes }),
}));

function renderGroup(
  data = { label: 'G', color: '#646cff' },
  notifyChange = vi.fn(),
  notifyRemoteLockedAttempt = vi.fn()
) {
  return {
    notifyChange,
    notifyRemoteLockedAttempt,
    ...render(
      <AnnotationContext.Provider
        value={{ notifyChange, notifyRemoteLockedAttempt, labels: { unlock: 'Unlock' } }}
      >
        <GroupNode id="group-1" data={data} selected />
      </AnnotationContext.Provider>
    ),
  };
}

const REMOTE_CLAIM = { clientId: 'c2', color: '#e6194b', displayName: 'Ada' };

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

describe('GroupNode interaction fixes', () => {
  beforeEach(() => vi.clearAllMocks());

  it('label input opts out of canvas dragging (nodrag)', () => {
    renderGroup();
    fireEvent.doubleClick(screen.getByText('G'));
    expect(screen.getByRole('textbox').className).toContain('nodrag');
  });

  it('closes the context menu when keyboard focus lands outside it (focusin)', async () => {
    renderGroup();
    fireEvent.contextMenu(screen.getByText('G'));
    expect(document.querySelector('.graph-group-context-menu')).not.toBeNull();
    // Dismiss listeners attach on a 0 ms timer so the opening event can't self-close.
    await new Promise((r) => setTimeout(r, 0));
    const outside = document.createElement('input');
    document.body.appendChild(outside);
    fireEvent.focusIn(outside);
    expect(document.querySelector('.graph-group-context-menu')).toBeNull();
    outside.remove();
  });

  it('keeps the context menu open when focus moves inside it', async () => {
    renderGroup();
    fireEvent.contextMenu(screen.getByText('G'));
    await new Promise((r) => setTimeout(r, 0));
    fireEvent.focusIn(screen.getByRole('button', { name: /delete group/i }));
    expect(document.querySelector('.graph-group-context-menu')).not.toBeNull();
  });
});

// task-annotation-shared-session-realtime: a group box is itself an
// annotation kind, so it gets the same exclusive-lease treatment as
// note/label/arrow/generic annotations.
describe('GroupNode remote selection claim exclusivity', () => {
  beforeEach(() => vi.clearAllMocks());

  it('refuses a rename while another client holds the claim, notifying instead', () => {
    const { notifyChange, notifyRemoteLockedAttempt } = renderGroup({
      label: 'G',
      remoteSelection: REMOTE_CLAIM,
    });
    fireEvent.doubleClick(screen.getByText('G'));
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('refuses to open the context menu while another client holds the claim', () => {
    const { notifyRemoteLockedAttempt } = renderGroup({
      label: 'G',
      remoteSelection: REMOTE_CLAIM,
    });
    fireEvent.contextMenu(screen.getByText('G'));
    expect(document.querySelector('.graph-group-context-menu')).toBeNull();
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
  });

  it("renders the claiming collaborator's badge and outline", () => {
    const { container } = renderGroup({ label: 'G', remoteSelection: REMOTE_CLAIM });
    expect(container.querySelector('.graph-node-remote-badge').textContent).toBe('Ada');
    expect(container.querySelector('.graph-group-node').style.outline).toContain('#e6194b');
  });

  it('edits normally once no claim is present', () => {
    const { notifyChange } = renderGroup({ label: 'G' });
    fireEvent.doubleClick(screen.getByText('G'));
    expect(screen.getByRole('textbox')).toBeInTheDocument();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Team map' } });
    fireEvent.blur(screen.getByRole('textbox'));
    expect(notifyChange).toHaveBeenCalledWith('text');
  });
});

// The capability baseline (docs/ANNOTATION_CONTRACT.md) is that a locked
// object "remains selectable but offers only unlock or copy". Every overlay
// kind has rendered a `locked ? <unlock only> : <full menu>` branch since
// PR #455 closed the last one (`line`). `group` did not — it read the remote
// claim above but never the persisted flag, which the group translators were
// dropping before it could reach this component. A group locked over MCP
// therefore showed its full Group Color / Hide Group / Delete Group menu with
// no way to unlock it from the GUI at all.
describe('GroupNode locked context menu', () => {
  beforeEach(() => vi.clearAllMocks());

  const lockedData = { label: 'G', color: '#646cff', locked: true };

  it('offers only unlock when the group is locked', () => {
    renderGroup(lockedData);
    fireEvent.contextMenu(screen.getByText('G'));
    expect(screen.getByRole('button', { name: /unlock/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /delete group/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /hide group/i })).toBeNull();
    expect(document.querySelector('.context-menu-colors')).toBeNull();
  });

  it('unlocks the group and publishes the change', () => {
    const { notifyChange } = renderGroup(lockedData);
    fireEvent.contextMenu(screen.getByText('G'));
    fireEvent.click(screen.getByRole('button', { name: /unlock/i }));
    const updater = hoisted.setNodes.mock.calls.at(-1)[0];
    expect(updater([{ id: 'group-1', data: lockedData }])[0].data.locked).toBe(false);
    // Without this the unlock is purely local: no op is published, so the
    // group is locked again on reload and no collaborator ever sees it.
    expect(notifyChange).toHaveBeenCalledWith('style');
  });

  it('withholds the resize handles while the group is locked', () => {
    renderGroup(lockedData);
    // The NodeResizer stub renders only when isVisible is true.
    expect(screen.queryByTestId('resize')).toBeNull();
  });

  it('keeps the full menu and the resizer when the group is not locked', () => {
    renderGroup();
    expect(screen.getByTestId('resize')).toBeInTheDocument();
    fireEvent.contextMenu(screen.getByText('G'));
    expect(screen.queryByRole('button', { name: /unlock/i })).toBeNull();
    expect(screen.getByRole('button', { name: /delete group/i })).toBeInTheDocument();
    expect(document.querySelectorAll('.color-button')).toHaveLength(6);
  });

  it('refuses to open the rename input while the group is locked', () => {
    const { notifyChange } = renderGroup(lockedData);
    fireEvent.doubleClick(screen.getByText('G'));
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('discards a pending rename when the lock arrives while the input is open', () => {
    const notifyChange = vi.fn();
    const notifyRemoteLockedAttempt = vi.fn();
    const { rerender } = renderGroup(
      { label: 'G', color: '#646cff' },
      notifyChange,
      notifyRemoteLockedAttempt
    );
    fireEvent.doubleClick(screen.getByText('G'));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Team map' } });
    rerender(
      <AnnotationContext.Provider
        value={{ notifyChange, notifyRemoteLockedAttempt, labels: { unlock: 'Unlock' } }}
      >
        <GroupNode id="group-1" data={lockedData} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.blur(screen.getByRole('textbox'));
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('surfaces the attempt instead of unlocking while another client holds the claim', () => {
    // The menu cannot be opened under a remote claim, so the unlock handler is
    // reached only if the claim arrives while the menu is already open. It must
    // still refuse rather than write.
    const { notifyChange, notifyRemoteLockedAttempt, rerender } = renderGroup(lockedData);
    fireEvent.contextMenu(screen.getByText('G'));
    rerender(
      <AnnotationContext.Provider
        value={{ notifyChange, notifyRemoteLockedAttempt, labels: { unlock: 'Unlock' } }}
      >
        <GroupNode id="group-1" data={{ ...lockedData, remoteSelection: REMOTE_CLAIM }} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.click(screen.getByRole('button', { name: /unlock/i }));
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });
});
