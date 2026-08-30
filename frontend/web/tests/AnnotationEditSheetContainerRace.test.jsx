import { useMemo, useState } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';

import MobileShell from '../src/components/MobileShell';
import useGraphStore from '../src/store/graphStore';
// Deep-imported straight from the package's source (not the package's public
// entrypoint) — same real components `AnnotationEditTrigger.test.jsx` in
// ui-graph-canvas exercises, unmocked, so this test's failure mode is the
// hook's own guard logic, not a stand-in for it.
import NoteNode from '../../../packages/ui-graph-canvas/src/components/NoteNode';
import { AnnotationContext } from '../../../packages/ui-graph-canvas/src/components/AnnotationContext';

// task-annotation-responsive-bottom-toolbox — regression coverage for the
// mobile Edit sheet's "opens and immediately self-closes on every tap" bug.
//
// Root cause (see useAnnotationEditTrigger.js's guard effect): the container
// this hook watches (`editSheet.container`, sourced from GraphCanvas's
// `annotationEditSheetPortalContainer` prop) does not arrive in the same
// commit as the `contextMenu = {sheet: true}` state the Edit button sets.
// The real path is a two-hop callback-ref handoff:
//
//   1. NoteNode's Edit button calls `editSheet.requestOpen()` (opens
//      MobileShell's `'detail'` surface) AND sets its own local
//      `contextMenu = {sheet: true}`, in the same event handler.
//   2. MobileShell re-renders with the surface open and mounts the sheet's
//      (empty) container `<div ref={onAnnotationEditSheetContainerChange}>`.
//      React invokes that CALLBACK ref during the commit phase; the
//      `setState` inside it (App.jsx's `setMobileAnnotationEditContainer`)
//      schedules a SEPARATE render/commit, so this first commit still has
//      `editSheet.container === null`.
//   3. React flushes this first commit's passive effects — including the
//      hook's own guard effect — BEFORE the second commit (the one with a
//      real container) ever renders.
//
// A test that only ever passes a *static* `editSheet.container` (mounted
// from the start, or permanently null) never exercises step 2's handoff at
// all, so it cannot catch a guard that reacts to the container being null on
// arrival exactly the same way it reacts to the container going missing
// after being shown — which is the actual bug. This test instead wires up
// the REAL `MobileShell` (unmocked) with a harness that reproduces App.jsx's
// own `mobileAnnotationEditContainer`/`detailSheetController` state and
// callback-ref plumbing verbatim (see App.jsx's `annotationEditSheetPortalContainer`/
// `onRequestAnnotationEditSheet`/`onCloseAnnotationEditSheet`/
// `onAnnotationEditSheetContainerChange` wiring), and a real `NoteNode`
// consuming a real `AnnotationContext.Provider` whose `editSheet` value is
// re-derived every render exactly the way `GraphCanvas.jsx`'s own
// `annotationContextValue` memo does (a pure prop/state derivation, no extra
// render hop of its own) — so the ReactFlow-heavy internals GraphCanvas
// would otherwise need are irrelevant to this race and safely left out,
// without hiding any part of the actual timing.

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

vi.mock('reactflow', () => ({
  NodeResizer: () => <div data-testid="resizer" />,
  useReactFlow: () => ({
    setNodes: hoisted.setNodes,
    screenToFlowPosition: (p) => p,
    getNodes: () => [],
  }),
}));

vi.mock('../src/i18n', () => ({
  useI18n: () => ({
    t: (key, _params, fallback) => fallback ?? key,
  }),
}));

// These MobileShell surfaces (Search/Create/Chat/Menu) play no part in the
// Edit-sheet race — stubbed out exactly as MobileShell.test.jsx already
// does, so a failure here can only mean the Edit-sheet wiring itself broke.
vi.mock('../src/components/FloatingSearch', () => ({
  default: () => <div data-testid="floating-search" />,
}));
vi.mock('../src/components/FloatingToolbar', () => ({
  default: () => <div data-testid="floating-toolbar" />,
}));
vi.mock('../src/components/SessionDrawer', () => ({
  default: ({ open }) => (open ? <div data-testid="session-drawer" /> : null),
}));
vi.mock('../src/components/ChatPanel', () => ({
  default: () => <div data-testid="chat-panel" />,
}));

const labels = {
  color: 'Colour',
  delete: 'Delete',
  notePlaceholder: 'Note',
  textSize: 'Text size',
  opacity: 'Opacity',
  editAnnotation: 'Edit',
  layer: 'Layer',
  layerFront: 'Bring to front',
  layerBack: 'Send to back',
  rotation: 'Rotation',
  rotateLeft: 'Rotate left',
  rotateRight: 'Rotate right',
  rotateReset: 'Reset rotation',
  duplicate: 'Duplicate',
};

function mobileShellProps() {
  return {
    sessionId: 'session-1',
    onClear: vi.fn(),
    sessions: [],
    currentSessionId: 'session-1',
    onNewSession: vi.fn(),
    onConnectSession: vi.fn(),
    onSelectSession: vi.fn(),
    onRenameSession: vi.fn(),
    onDeleteSession: vi.fn(),
    onCopySessionLink: vi.fn(),
    onCopyTriggerUrl: vi.fn(),
    onOpenSettings: vi.fn(),
    onOpenActivity: vi.fn(),
    canvasLocked: false,
    onToggleLock: vi.fn(),
    suspendEscape: false,
    onCreateNodeForType: vi.fn(),
    onCreateAgent: vi.fn(),
    onCreateSubscription: vi.fn(),
    onSaveView: vi.fn(),
    onCreateGroup: vi.fn(),
    onCreateActiveKnowledgeCollection: vi.fn(),
    llmAvailable: true,
    akcShortName: undefined,
    onAnnotationSheetContainerChange: vi.fn(),
  };
}

// Mirrors frontend/web/src/App.jsx's own `mobileAnnotationEditContainer` /
// `detailSheetController` state and the exact props it passes to GraphCanvas
// (`annotationEditSheetPortalContainer`, `onRequestAnnotationEditSheet`,
// `onCloseAnnotationEditSheet`) and to MobileShell
// (`onAnnotationEditSheetContainerChange`, `onDetailSheetControllerReady`),
// plus GraphCanvas's own `editSheet` derivation (its `annotationContextValue`
// memo) — real components, real callback refs, only ReactFlow's own
// rendering internals swapped for a direct `AnnotationContext.Provider`.
function AppHarness() {
  const [mobileAnnotationEditContainer, setMobileAnnotationEditContainer] = useState(null);
  const [detailSheetController, setDetailSheetController] = useState(null);

  const editSheet = useMemo(
    () => ({
      capable: Boolean(detailSheetController?.open),
      container: mobileAnnotationEditContainer,
      requestOpen: detailSheetController?.open ?? null,
      requestClose: detailSheetController?.close ?? null,
    }),
    [mobileAnnotationEditContainer, detailSheetController]
  );

  return (
    <>
      <AnnotationContext.Provider
        value={{
          notifyChange: () => {},
          notifyRemoteLockedAttempt: () => {},
          attachNearby: () => {},
          labels,
          editSheet,
        }}
      >
        <NoteNode id="note-1" data={{ text: 'Hello' }} selected />
      </AnnotationContext.Provider>
      <MobileShell
        {...mobileShellProps()}
        onAnnotationEditSheetContainerChange={setMobileAnnotationEditContainer}
        onDetailSheetControllerReady={setDetailSheetController}
      />
    </>
  );
}

describe('mobile Edit sheet — container callback-ref race (task-annotation-responsive-bottom-toolbox)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    useGraphStore.setState({ chatPanelOpen: false });
  });

  it('stays open across the container handoff instead of flashing open and immediately self-closing', async () => {
    render(<AppHarness />);

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));

    // The sheet surface itself must be open...
    const container = screen.getByTestId('mobile-annotation-edit-sheet-container');
    expect(container).toBeInTheDocument();
    // ...and, critically, the menu content must actually have been portalled
    // into it — this is the exact assertion the old guard fails: it closes
    // `contextMenu` in the same flush that would otherwise let this render,
    // one render before the container it was waiting for arrives.
    const menu = container.querySelector('.graph-annotation-context-menu.sheet');
    expect(menu).not.toBeNull();
    expect(container.textContent).toContain('Colour');

    // Not just a flash: let a further tick of scheduling pass (any effect
    // outside the click's own act-wrapped flush) and confirm it is STILL
    // open and visible, not silently closed back out from under the user.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const containerAfter = screen.getByTestId('mobile-annotation-edit-sheet-container');
    expect(containerAfter).toBeInTheDocument();
    expect(containerAfter.querySelector('.graph-annotation-context-menu.sheet')).not.toBeNull();
  });

  it('still closes for real when the sheet is dismissed after genuinely showing (the guard this hook is meant to keep)', async () => {
    render(<AppHarness />);

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(
      screen
        .getByTestId('mobile-annotation-edit-sheet-container')
        .querySelector('.graph-annotation-context-menu.sheet')
    ).not.toBeNull();

    // The Graph nav item closes every surface, same as any other way the
    // host can close the sheet out from under an open menu (switching
    // tabs, the sheet's own close control).
    fireEvent.click(screen.getByLabelText('mobile_nav.graph'));

    expect(screen.queryByTestId('mobile-annotation-edit-sheet-container')).not.toBeInTheDocument();
    expect(document.querySelector('.graph-annotation-context-menu')).toBeNull();
  });
});
