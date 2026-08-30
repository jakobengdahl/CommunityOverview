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
      remoteLease: REMOTE_CLAIM,
    });
    fireEvent.doubleClick(screen.getByText('G'));
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('refuses to open the context menu while another client holds the claim', () => {
    const { notifyRemoteLockedAttempt } = renderGroup({
      label: 'G',
      remoteLease: REMOTE_CLAIM,
    });
    fireEvent.contextMenu(screen.getByText('G'));
    expect(document.querySelector('.graph-group-context-menu')).toBeNull();
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
  });

  it("renders the claiming collaborator's badge and outline", () => {
    const { container } = renderGroup({ label: 'G', remoteLease: REMOTE_CLAIM });
    expect(container.querySelector('.graph-node-remote-badge').textContent).toBe('Ada');
    expect(container.querySelector('.graph-group-node').style.outline).toContain('#e6194b');
  });

  it('a mere remoteSelection (no edit lease) still shows the badge but never refuses a rename', () => {
    // task-annotation-exclusive-edit-leases: selection stays a purely
    // cosmetic marker — it must never gate a rename or menu open.
    const { container, notifyRemoteLockedAttempt } = renderGroup({
      label: 'G',
      remoteSelection: REMOTE_CLAIM,
    });
    expect(container.querySelector('.graph-node-remote-badge').textContent).toBe('Ada');
    fireEvent.doubleClick(screen.getByText('G'));
    expect(screen.getByRole('textbox')).toBeInTheDocument();
    expect(notifyRemoteLockedAttempt).not.toHaveBeenCalled();
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
// no way to unlock it from the GUI at all. `group` now follows the baseline
// exactly: Unlock and nothing else.
describe('GroupNode locked context menu', () => {
  beforeEach(() => vi.clearAllMocks());

  const lockedData = { label: 'G', color: '#646cff', locked: true };

  // The locked menu offers Unlock and nothing else. The unlocked menu has two
  // other actions — recolour, an edit, and Delete Group, which destroys the
  // box — and a lock refuses both, so there is nothing left for this branch to
  // carry. This case pins that by the menu's text and its element count rather
  // than by its buttons: see the three assertions at the end, and the note
  // there on what they do not cover.
  it('offers unlock and nothing else when the group is locked', () => {
    renderGroup(lockedData);
    fireEvent.contextMenu(screen.getByText('G'));
    expect(screen.getByRole('button', { name: /unlock/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /hide group/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /delete group/i })).toBeNull();
    expect(document.querySelector('.context-menu-colors')).toBeNull();
    expect(document.querySelectorAll('.color-button')).toHaveLength(0);
    // The menu holds exactly one element, it is the unlock button, and the
    // only text is that button's label. A `div` or `a` carrying onClick is a
    // working control that every role- and button-scoped assertion above sees
    // as absent, so the guard has to be structural rather than by role.
    //
    // The subtree count is deliberate and it is the assertion that does the
    // work. Counting direct children instead — which is what shipped here
    // twice — lets a control hide one wrapper down, or nest a text-free span
    // inside the unlock button where `textContent` cannot see it. Both
    // survivors were found against exactly that. Counting the whole subtree
    // forbids every one of them at once, whatever tag it uses and whatever
    // event it listens for.
    //
    // The price is that a legitimate icon `<span>` inside the unlock button
    // fails this case. That is the intended trade, not an oversight: adding
    // an element to a locked group's menu is exactly the change that should
    // stop and make someone confirm the menu still offers nothing but
    // Unlock. Update this assertion deliberately if that day comes.
    //
    // What this does NOT cover: a destructive handler added to the unlock
    // button itself, which changes no shape. The unlock case below covers its
    // click path — but only because that case counts calls; asserting on the
    // last `setNodes` and on `toHaveBeenCalledWith` alone missed a
    // `removeGroupKeepChildren()` added ahead of the unlock body, since the
    // destructive call was neither the last one nor the one matched. A
    // handler on a strictly non-click event of that one button is still
    // uncovered.
    const menu = document.querySelector('.graph-group-context-menu');
    expect(menu.textContent).toBe('🔓 Unlock');
    expect(menu.querySelectorAll('*')).toHaveLength(1);
    expect(menu.firstElementChild).toBe(screen.getByRole('button', { name: /unlock/i }));
  });

  // Delete Group is now the single path to removeGroupKeepChildren. It used to
  // share that handler with a Hide Group button whose label promised something
  // it did not do — both dissolved the box and kept the members — so Hide was
  // removed rather than made real. This pins what the surviving button does,
  // because "keeps the members" is the part a user has to be able to rely on:
  // deleting a group must not take the nodes inside it with it.
  it('dissolves the group but keeps its members when Delete Group is used', () => {
    const { notifyChange } = renderGroup();
    fireEvent.contextMenu(screen.getByText('G'));
    fireEvent.click(screen.getByRole('button', { name: /delete group/i }));
    const updater = hoisted.setNodes.mock.calls.at(-1)[0];
    const remaining = updater([
      { id: 'group-1', data: { label: 'G' }, position: { x: 10, y: 20 } },
      { id: 'n1', parentId: 'group-1', position: { x: 5, y: 5 } },
    ]);
    expect(remaining.map((n) => n.id)).toEqual(['n1']);
    // Un-parented and re-based to absolute coordinates, so the member stays
    // where it looked rather than jumping to the canvas origin.
    expect(remaining[0].parentId).toBeUndefined();
    expect(remaining[0].position).toEqual({ x: 15, y: 25 });
    expect(notifyChange).toHaveBeenCalledWith('delete');
  });

  // Hide Group was a second, differently-labelled Delete. This guards against
  // it coming back, which is what this PR exists to prevent. Two assertions,
  // and it is worth being exact about what each one forbids, because they are
  // not the same shape.
  //
  // The text is the on-axis guard: nothing in this menu may say hide in any
  // form, whatever tag carries it. The pattern covers "hidden" and "hiding"
  // as well as "hide", because "Hidden members" is a hide-flavoured label
  // that a bare /hide/ misses — `hidden` does not contain `hide`. The shared
  // `hid` is what the alternation factors out.
  // That is the claim this PR is about, so a future "Unhide" or "Hidden
  // members" failing here is correct: it should stop and make someone confirm
  // the button is not creeping back under a softer name.
  //
  // The button count is a deliberate pin, not a growth allowance. An honest
  // new `<button>` — a Rename, say — fails it, and whoever adds one should
  // update the number consciously, the same trade the locked case takes with
  // its subtree count above. What slips through both is a differently-worded
  // non-button control (`<div role="button">Archive Group</div>`); that gap is
  // real and accepted, but it is a gap, not a policy of letting the menu grow.
  it('offers colour and delete on an unlocked group, and nothing that hides', () => {
    renderGroup();
    fireEvent.contextMenu(screen.getByText('G'));
    expect(screen.getByRole('button', { name: /delete group/i })).toBeInTheDocument();
    expect(document.querySelector('.context-menu-title').textContent).toBe('Group Color');
    expect(document.querySelectorAll('.color-button')).toHaveLength(6);
    const menu = document.querySelector('.graph-group-context-menu');
    // Not `queryByRole('button', …)`: a `div` or `a` labelled Hide Group is a
    // working control that a role query reports as absent. Text sees it.
    expect(menu.textContent).not.toMatch(/hid(e|den|ing)/i);
    // Six swatches, the non-drag size control's Apply button
    // (task-annotation-accessible-shared-controls' AnnotationSizeControl —
    // the two <input type="number"> elements are not <button>s, so only this
    // one adds to the count) and Delete Group — eight total, consciously
    // updated from the pre-existing seven per this test's own comment above.
    expect(menu.querySelectorAll('button')).toHaveLength(8);
  });

  it('unlocks the group and publishes the change', () => {
    const { notifyChange } = renderGroup(lockedData);
    fireEvent.contextMenu(screen.getByText('G'));
    fireEvent.click(screen.getByRole('button', { name: /unlock/i }));
    // Counts, not just contents. Reading only the last `setNodes` call and
    // matching `notifyChange` with any call let a `removeGroupKeepChildren()`
    // inserted ahead of the unlock body pass every assertion here — clicking
    // Unlock destroyed the group and 21/21 still went green. Unlock does
    // exactly one of each; anything else on this button is a second.
    expect(hoisted.setNodes).toHaveBeenCalledTimes(1);
    expect(notifyChange).toHaveBeenCalledTimes(1);
    const updater = hoisted.setNodes.mock.calls.at(-1)[0];
    const updated = updater([{ id: 'group-1', data: lockedData, draggable: false }])[0];
    expect(updated.data.locked).toBe(false);
    // No local path recomputes a group's `draggable`, so clearing only
    // `data.locked` would give back the menu but leave the box pinned until
    // reload. It must go back to `undefined`, not `true`: ReactFlow treats an
    // explicit boolean as an override of the canvas-wide `nodesDraggable`
    // switch, so `true` would keep the group draggable during a freehand
    // stroke, which that switch exists to prevent.
    expect(updated.draggable).toBeUndefined();
    expect('draggable' in updated).toBe(true);
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

  // smallfix-groupnode-colour-swatch-no-active-marker: the swatch grid had no
  // indication of the group's current colour at all, the same gap PR #510
  // fixed for the sibling annotation menus (see ArrowNode.test.jsx's "marks
  // the swatch matching the current colour as active" case).
  it('marks the swatch matching the current colour as active, and no other', () => {
    renderGroup({ label: 'G', color: '#F97316' });
    fireEvent.contextMenu(screen.getByText('G'));

    const buttons = [...document.querySelectorAll('.color-button')];
    const active = buttons.find((b) => b.style.backgroundColor === 'rgb(249, 115, 22)');
    const inactive = buttons.find((b) => b.style.backgroundColor === 'rgb(100, 108, 255)');
    expect(active.className).toContain('active');
    expect(inactive.className).not.toContain('active');
  });

  it('refuses to open the rename input while the group is locked', () => {
    const { notifyChange } = renderGroup(lockedData);
    fireEvent.doubleClick(screen.getByText('G'));
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('closes the rename input as soon as the lock arrives, rather than letting it fill', () => {
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
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('drops the abandoned draft, so a later unlock does not resurrect it', () => {
    // Closing the input is not enough on its own: `editedLabel` is component
    // state that outlives it. Without the reset, unlocking and double-clicking
    // again pre-fills the input with the text the lock refused, and the next
    // blur commits it as a rename nobody asked for.
    const notifyChange = vi.fn();
    const notifyRemoteLockedAttempt = vi.fn();
    const unlockedData = { label: 'G', color: '#646cff' };
    const { rerender } = renderGroup(unlockedData, notifyChange, notifyRemoteLockedAttempt);
    const withData = (data) => (
      <AnnotationContext.Provider
        value={{ notifyChange, notifyRemoteLockedAttempt, labels: { unlock: 'Unlock' } }}
      >
        <GroupNode id="group-1" data={data} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.doubleClick(screen.getByText('G'));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Abandoned' } });
    rerender(withData(lockedData));
    rerender(withData(unlockedData));
    fireEvent.doubleClick(screen.getByText('G'));
    expect(screen.getByRole('textbox')).toHaveValue('G');
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
        <GroupNode id="group-1" data={{ ...lockedData, remoteLease: REMOTE_CLAIM }} selected />
      </AnnotationContext.Provider>
    );
    fireEvent.click(screen.getByRole('button', { name: /unlock/i }));
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });
});
