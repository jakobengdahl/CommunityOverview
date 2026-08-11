/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import AppDialogs from './AppDialogs';

// Stub the dialog children: AppDialogs is a pure registry, so the invariant
// under test is "the right child renders for the right state slot, wired to the
// right callback" — not each child's internals. Each stub exposes its identity
// and the props AppDialogs is responsible for wiring.
vi.mock('./CreateNodeDialog', () => ({
  default: ({ nodeType, onClose, onSave }) => (
    <div data-testid="create-node" data-node-type={nodeType}>
      <button onClick={() => onSave('made')}>save</button>
      <button onClick={onClose}>close</button>
    </div>
  ),
}));
vi.mock('./EditNodeDialog', () => ({
  default: ({ node, onClose, onSave }) => (
    <div data-testid="edit-node" data-node-id={node.id}>
      <button onClick={() => onSave({ name: 'x' })}>save</button>
      <button onClick={onClose}>close</button>
    </div>
  ),
}));
vi.mock('./NodeDetailDialog', () => ({
  default: ({ node, onClose, onEdit, initialView }) => (
    <div data-testid="node-detail" data-node-id={node.id} data-initial-view={initialView}>
      <button onClick={() => onEdit(node.id, node.data)}>edit</button>
      <button onClick={onClose}>close</button>
    </div>
  ),
}));
vi.mock('./EditEdgeDialog', () => ({
  default: ({ onClose, onSave, onDelete }) => (
    <div data-testid="edit-edge">
      <button onClick={() => onSave({ type: 'X' })}>save</button>
      <button onClick={() => onDelete('edge-1')}>delete</button>
      <button onClick={onClose}>close</button>
    </div>
  ),
}));
vi.mock('./ConfirmDialog', () => ({
  default: ({ title, onConfirm, onCancel }) => (
    <div data-testid="confirm" data-title={title}>
      <button onClick={onConfirm}>confirm</button>
      <button onClick={onCancel}>cancel</button>
    </div>
  ),
}));
vi.mock('./InputDialog', () => ({
  default: ({ title, onConfirm, onCancel }) => (
    <div data-testid="input" data-title={title}>
      <button onClick={() => onConfirm('typed')}>confirm</button>
      <button onClick={onCancel}>cancel</button>
    </div>
  ),
}));
vi.mock('./SettingsDialog', () => ({
  default: ({ onExportGraph, onClose }) => (
    <div data-testid="settings">
      <button onClick={onExportGraph}>export</button>
      <button onClick={onClose}>close</button>
    </div>
  ),
}));
vi.mock('./CreateSubscriptionDialog', () => ({
  default: ({ onClose, onSave }) => (
    <div data-testid="subscription">
      <button onClick={() => onSave({ name: 's' })}>save</button>
      <button onClick={onClose}>close</button>
    </div>
  ),
}));
vi.mock('./CreateSkillDialog', () => ({
  default: ({ nodeType, onClose, onSave }) => (
    <div data-testid="skill" data-node-type={nodeType}>
      <button onClick={() => onSave({ name: 'k' })}>save</button>
      <button onClick={onClose}>close</button>
    </div>
  ),
}));
vi.mock('./CreateAgentDialog', () => ({
  default: ({ onClose, onSave }) => (
    <div data-testid="agent">
      <button onClick={() => onSave({ name: 'a' })}>save</button>
      <button onClick={onClose}>close</button>
    </div>
  ),
}));
vi.mock('./CreateActiveKnowledgeCollectionDialog', () => ({
  default: ({ onClose, onSave }) => (
    <div data-testid="akc">
      <button onClick={() => onSave({ name: 'c' })}>save</button>
      <button onClick={onClose}>close</button>
    </div>
  ),
}));

// Identity translator so title-keyed assertions distinguish the shared
// ConfirmDialog / InputDialog instances by their translation key.
const t = (key) => key;

// A dialogs object with every slot closed and every setter spied.
function makeDialogs(overrides = {}) {
  const base = {
    createNodeType: null,
    setCreateNodeType: vi.fn(),
    editingEdge: null,
    setEditingEdge: vi.fn(),
    deleteDialog: null,
    setDeleteDialog: vi.fn(),
    saveViewDialog: null,
    setSaveViewDialog: vi.fn(),
    isSavingView: false,
    showSubscriptionDialog: false,
    setShowSubscriptionDialog: vi.fn(),
    editingSubscriptionData: null,
    setEditingSubscriptionData: vi.fn(),
    showAgentDialog: false,
    setShowAgentDialog: vi.fn(),
    editingAgentData: null,
    setEditingAgentData: vi.fn(),
    skillDialogType: null,
    setSkillDialogType: vi.fn(),
    editingSkillData: null,
    setEditingSkillData: vi.fn(),
    showAKCDialog: false,
    setShowAKCDialog: vi.fn(),
    editingAKCData: null,
    setEditingAKCData: vi.fn(),
    settingsOpen: false,
    setSettingsOpen: vi.fn(),
    connectDialogOpen: false,
    setConnectDialogOpen: vi.fn(),
    renameDialog: null,
    setRenameDialog: vi.fn(),
    deleteSessionDialog: null,
    setDeleteSessionDialog: vi.fn(),
  };
  return { ...base, ...overrides };
}

function renderDialogs(dialogs, props = {}) {
  const handlers = {
    onNodeCreated: vi.fn(),
    onNodeUpdate: vi.fn(),
    onEdit: vi.fn(),
    onEdgeUpdate: vi.fn(),
    onDeleteEdge: vi.fn(),
    onConfirmDelete: vi.fn(),
    onConfirmSaveView: vi.fn(),
    onExportGraph: vi.fn(),
    onConnectSession: vi.fn(),
    onRenameSession: vi.fn(),
    onConfirmDeleteSession: vi.fn(),
    onSaveSubscription: vi.fn(),
    onSaveSkill: vi.fn(),
    onSaveAgent: vi.fn(),
    onSaveAKC: vi.fn(),
    ...props.handlers,
  };
  const utils = render(
    <AppDialogs
      dialogs={dialogs}
      t={t}
      nodes={props.nodes || []}
      stats={props.stats || null}
      editingNode={props.editingNode || null}
      closeEditingNode={props.closeEditingNode || vi.fn()}
      detailNode={props.detailNode || null}
      closeDetailNode={props.closeDetailNode || vi.fn()}
      akcShortName={props.akcShortName || null}
      akcConfig={props.akcConfig || null}
      akcIntroShown={props.akcIntroShown || false}
      onAkcIntroShown={props.onAkcIntroShown || vi.fn()}
      {...handlers}
    />
  );
  return { ...utils, handlers };
}

describe('AppDialogs registry', () => {
  it('renders nothing when every slot is closed', () => {
    const { container } = renderDialogs(makeDialogs());
    expect(container.querySelector('[data-testid]')).toBeNull();
  });

  it('renders the create-node dialog and wires save + close', () => {
    const dialogs = makeDialogs({ createNodeType: 'Actor' });
    const { handlers } = renderDialogs(dialogs);
    const el = screen.getByTestId('create-node');
    expect(el.getAttribute('data-node-type')).toBe('Actor');
    fireEvent.click(screen.getByText('save'));
    expect(handlers.onNodeCreated).toHaveBeenCalled();
    fireEvent.click(screen.getByText('close'));
    expect(dialogs.setCreateNodeType).toHaveBeenCalledWith(null);
  });

  it('routes the delete-node confirm to onConfirmDelete and cancel to setDeleteDialog', () => {
    const dialogs = makeDialogs({
      deleteDialog: { nodeId: 'n1', nodeName: 'Node 1', isMultiple: false },
    });
    const { handlers } = renderDialogs(dialogs);
    expect(screen.getByTestId('confirm').getAttribute('data-title')).toBe('Delete Node');
    fireEvent.click(screen.getByText('confirm'));
    expect(handlers.onConfirmDelete).toHaveBeenCalled();
    fireEvent.click(screen.getByText('cancel'));
    expect(dialogs.setDeleteDialog).toHaveBeenCalledWith(null);
  });

  it('distinguishes the delete-session confirm from the delete-node confirm by title', () => {
    const dialogs = makeDialogs({ deleteSessionDialog: { id: 's1', connectedOthers: 0 } });
    const { handlers } = renderDialogs(dialogs);
    expect(screen.getByTestId('confirm').getAttribute('data-title')).toBe(
      'sessions.delete_session_title'
    );
    fireEvent.click(screen.getByText('confirm'));
    expect(handlers.onConfirmDeleteSession).toHaveBeenCalled();
  });

  it('routes the three InputDialog instances to their own handlers by title', () => {
    // Save View
    const saveView = makeDialogs({ saveViewDialog: { viewData: {} } });
    const r1 = renderDialogs(saveView);
    expect(screen.getByTestId('input').getAttribute('data-title')).toBe('Save View');
    fireEvent.click(screen.getByText('confirm'));
    expect(r1.handlers.onConfirmSaveView).toHaveBeenCalledWith('typed');
    r1.unmount();

    // Connect
    const connect = makeDialogs({ connectDialogOpen: true });
    const r2 = renderDialogs(connect);
    expect(screen.getByTestId('input').getAttribute('data-title')).toBe(
      'sessions.connect_session_title'
    );
    fireEvent.click(screen.getByText('confirm'));
    expect(r2.handlers.onConnectSession).toHaveBeenCalledWith('typed');
    r2.unmount();

    // Rename
    const rename = makeDialogs({ renameDialog: { id: 's1', name: 'Old' } });
    const r3 = renderDialogs(rename);
    expect(screen.getByTestId('input').getAttribute('data-title')).toBe(
      'sessions.rename_session_title'
    );
    fireEvent.click(screen.getByText('confirm'));
    expect(r3.handlers.onRenameSession).toHaveBeenCalledWith('typed');
  });

  it('renders the store-driven edit-node dialog and wires update + close', () => {
    const closeEditingNode = vi.fn();
    const { handlers } = renderDialogs(makeDialogs(), {
      editingNode: { id: 'n7', data: {} },
      closeEditingNode,
    });
    expect(screen.getByTestId('edit-node').getAttribute('data-node-id')).toBe('n7');
    fireEvent.click(screen.getByText('save'));
    expect(handlers.onNodeUpdate).toHaveBeenCalledWith('n7', { name: 'x' });
    fireEvent.click(screen.getByText('close'));
    expect(closeEditingNode).toHaveBeenCalled();
  });

  it('closes the detail dialog before delegating to onEdit', () => {
    const closeDetailNode = vi.fn();
    const { handlers } = renderDialogs(makeDialogs(), {
      detailNode: { id: 'n9', data: { type: 'Actor' } },
      closeDetailNode,
    });
    fireEvent.click(screen.getByText('edit'));
    expect(closeDetailNode).toHaveBeenCalled();
    expect(handlers.onEdit).toHaveBeenCalledWith('n9', { type: 'Actor' });
  });

  it('wires the settings dialog export action', () => {
    const dialogs = makeDialogs({ settingsOpen: true });
    const { handlers } = renderDialogs(dialogs);
    fireEvent.click(screen.getByText('export'));
    expect(handlers.onExportGraph).toHaveBeenCalled();
    fireEvent.click(screen.getByText('close'));
    expect(dialogs.setSettingsOpen).toHaveBeenCalledWith(false);
  });

  it('clears both flag and edit-target when a subscription dialog closes', () => {
    const dialogs = makeDialogs({
      showSubscriptionDialog: true,
      editingSubscriptionData: { id: 'sub1' },
    });
    renderDialogs(dialogs);
    fireEvent.click(screen.getByText('close'));
    expect(dialogs.setShowSubscriptionDialog).toHaveBeenCalledWith(false);
    expect(dialogs.setEditingSubscriptionData).toHaveBeenCalledWith(null);
  });

  it('shows the AKC intro overlay only while short name + config are set and not yet dismissed', () => {
    const onAkcIntroShown = vi.fn();
    const { rerender } = renderDialogs(makeDialogs(), {
      akcShortName: 'demo',
      akcConfig: { title: 'x' },
      akcIntroShown: false,
      onAkcIntroShown,
    });
    fireEvent.click(screen.getByText('Open Graph'));
    expect(onAkcIntroShown).toHaveBeenCalled();

    // Once dismissed, the overlay is gone.
    rerender(
      <AppDialogs
        dialogs={makeDialogs()}
        t={t}
        nodes={[]}
        akcShortName="demo"
        akcConfig={{ title: 'x' }}
        akcIntroShown
        onAkcIntroShown={onAkcIntroShown}
      />
    );
    expect(screen.queryByText('Open Graph')).toBeNull();
  });
});
