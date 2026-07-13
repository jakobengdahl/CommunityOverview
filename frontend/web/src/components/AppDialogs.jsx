import CreateNodeDialog from './CreateNodeDialog';
import EditNodeDialog from './EditNodeDialog';
import ConfirmDialog from './ConfirmDialog';
import InputDialog from './InputDialog';
import CreateSubscriptionDialog from './CreateSubscriptionDialog';
import CreateSkillDialog from './CreateSkillDialog';
import CreateAgentDialog from './CreateAgentDialog';
import CreateActiveKnowledgeCollectionDialog from './CreateActiveKnowledgeCollectionDialog';
import EditEdgeDialog from './EditEdgeDialog';
import NodeDetailDialog from './NodeDetailDialog';
import SettingsDialog from './SettingsDialog';

// Renders App's modal dialog / overlay stack from the state held in
// useDialogState plus the store-driven edit dialogs and their handlers
// (STRUCTURE_REVIEW B1 slice 3). Purely presentational: it opens/closes nothing
// on its own — every transition goes through the setters and handlers App owns,
// so behaviour is identical to the previous inline JSX.
function AppDialogs({
  dialogs,
  t,
  nodes,
  stats,
  // Store-driven dialogs (owned by the graph store, not useDialogState)
  editingNode,
  closeEditingNode,
  detailNode,
  closeDetailNode,
  // AKC intro overlay
  akcShortName,
  akcConfig,
  akcIntroShown,
  onAkcIntroShown,
  // Handlers
  onNodeCreated,
  onNodeUpdate,
  onEdit,
  onEdgeUpdate,
  onDeleteEdge,
  onConfirmDelete,
  onConfirmSaveView,
  onExportGraph,
  onConnectSession,
  onRenameSession,
  onConfirmDeleteSession,
  onSaveSubscription,
  onSaveSkill,
  onSaveAgent,
  onSaveAKC,
}) {
  const {
    createNodeType,
    setCreateNodeType,
    editingEdge,
    setEditingEdge,
    deleteDialog,
    setDeleteDialog,
    saveViewDialog,
    setSaveViewDialog,
    isSavingView,
    showSubscriptionDialog,
    setShowSubscriptionDialog,
    editingSubscriptionData,
    setEditingSubscriptionData,
    showAgentDialog,
    setShowAgentDialog,
    editingAgentData,
    setEditingAgentData,
    skillDialogType,
    setSkillDialogType,
    editingSkillData,
    setEditingSkillData,
    showAKCDialog,
    setShowAKCDialog,
    editingAKCData,
    setEditingAKCData,
    settingsOpen,
    setSettingsOpen,
    connectDialogOpen,
    setConnectDialogOpen,
    renameDialog,
    setRenameDialog,
    deleteSessionDialog,
    setDeleteSessionDialog,
  } = dialogs;

  return (
    <>
      {createNodeType && (
        <CreateNodeDialog
          nodeType={createNodeType}
          onClose={() => setCreateNodeType(null)}
          onSave={onNodeCreated}
        />
      )}

      {editingNode && (
        <EditNodeDialog
          node={editingNode}
          onClose={closeEditingNode}
          onSave={(updates) => onNodeUpdate(editingNode.id, updates)}
        />
      )}

      {detailNode && (
        <NodeDetailDialog
          node={detailNode}
          onClose={closeDetailNode}
          onEdit={(nodeId, nodeData) => {
            closeDetailNode();
            onEdit(nodeId, nodeData);
          }}
        />
      )}

      {editingEdge && (
        <EditEdgeDialog
          edge={editingEdge}
          nodes={nodes}
          onClose={() => setEditingEdge(null)}
          onSave={onEdgeUpdate}
          onDelete={(edgeId) => {
            onDeleteEdge(edgeId);
            setEditingEdge(null);
          }}
        />
      )}

      {deleteDialog && (
        <ConfirmDialog
          title={deleteDialog.isMultiple ? 'Delete Nodes' : 'Delete Node'}
          message={
            deleteDialog.isMultiple
              ? `Are you sure you want to delete ${deleteDialog.nodeIds.length} nodes? This action cannot be undone.\n\nNodes to delete:\n• ${deleteDialog.nodeNames.slice(0, 5).join('\n• ')}${deleteDialog.nodeNames.length > 5 ? `\n• ... and ${deleteDialog.nodeNames.length - 5} more` : ''}`
              : `Are you sure you want to delete "${deleteDialog.nodeName}"? This action cannot be undone.`
          }
          confirmText="Delete"
          cancelText="Cancel"
          confirmStyle="danger"
          onConfirm={onConfirmDelete}
          onCancel={() => setDeleteDialog(null)}
        />
      )}

      {saveViewDialog && (
        <InputDialog
          title="Save View"
          label="View name"
          placeholder="Enter a name for this view..."
          confirmText="Save"
          cancelText="Cancel"
          loadingText={t('common.saving')}
          isLoading={isSavingView}
          onConfirm={onConfirmSaveView}
          onCancel={() => setSaveViewDialog(null)}
        />
      )}

      {settingsOpen && (
        <SettingsDialog
          stats={stats}
          onExportGraph={onExportGraph}
          onClose={() => setSettingsOpen(false)}
        />
      )}

      {connectDialogOpen && (
        <InputDialog
          title={t('sessions.connect_session_title')}
          label={t('sessions.connect_session_label')}
          placeholder="1234-5678"
          confirmText={t('sessions.connect')}
          cancelText={t('common.cancel')}
          onConfirm={onConnectSession}
          onCancel={() => setConnectDialogOpen(false)}
        />
      )}

      {renameDialog && (
        <InputDialog
          title={t('sessions.rename_session_title')}
          label={t('sessions.session_name_label')}
          defaultValue={renameDialog.name}
          confirmText={t('common.save')}
          cancelText={t('common.cancel')}
          allowEmpty
          onConfirm={onRenameSession}
          onCancel={() => setRenameDialog(null)}
        />
      )}

      {deleteSessionDialog && (
        <ConfirmDialog
          title={t('sessions.delete_session_title')}
          message={
            deleteSessionDialog.connectedOthers > 0
              ? t('sessions.delete_session_message_multi', {
                  count: deleteSessionDialog.connectedOthers,
                })
              : t('sessions.delete_session_message')
          }
          confirmText={t('sessions.delete_confirm')}
          cancelText={t('common.cancel')}
          confirmStyle="danger"
          onConfirm={onConfirmDeleteSession}
          onCancel={() => setDeleteSessionDialog(null)}
        />
      )}

      {showSubscriptionDialog && (
        <CreateSubscriptionDialog
          onClose={() => {
            setShowSubscriptionDialog(false);
            setEditingSubscriptionData(null);
          }}
          onSave={onSaveSubscription}
          initialData={editingSubscriptionData}
        />
      )}

      {skillDialogType && (
        <CreateSkillDialog
          nodeType={skillDialogType}
          initialData={editingSkillData}
          onClose={() => {
            setSkillDialogType(null);
            setEditingSkillData(null);
          }}
          onSave={onSaveSkill}
        />
      )}

      {showAgentDialog && (
        <CreateAgentDialog
          onClose={() => {
            setShowAgentDialog(false);
            setEditingAgentData(null);
          }}
          onSave={onSaveAgent}
          initialData={editingAgentData}
        />
      )}

      {showAKCDialog && (
        <CreateActiveKnowledgeCollectionDialog
          onClose={() => {
            setShowAKCDialog(false);
            setEditingAKCData(null);
          }}
          onSave={onSaveAKC}
          initialData={editingAKCData}
        />
      )}

      {akcShortName && akcConfig && !akcIntroShown && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.82)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 3000,
          }}
        >
          <div
            style={{
              background: '#1a1a1a',
              border: '1px solid #2e2e2e',
              borderRadius: '16px',
              boxShadow: '0 12px 48px rgba(0,0,0,0.6)',
              padding: '2.5rem 3rem',
              maxWidth: '520px',
              width: '90%',
              textAlign: 'center',
            }}
          >
            <h2 style={{ margin: '0 0 1rem 0', color: '#fff' }}>Knowledge Collection</h2>
            <p
              style={{
                color: '#bbb',
                fontSize: '0.95rem',
                lineHeight: 1.65,
                marginBottom: '1.5rem',
              }}
            >
              The AI assistant has been pre-loaded with special collection instructions.
            </p>
            <button
              onClick={onAkcIntroShown}
              style={{
                padding: '0.7rem 2rem',
                background: '#F59E0B',
                color: '#000',
                border: 'none',
                borderRadius: '8px',
                fontSize: '0.95rem',
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              Open Graph
            </button>
          </div>
        </div>
      )}
    </>
  );
}

export default AppDialogs;
