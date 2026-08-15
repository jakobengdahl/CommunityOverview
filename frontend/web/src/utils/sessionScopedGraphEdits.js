import useGraphStore, { isStaleSessionEpoch } from '../store/graphStore';

/**
 * The two graph edits that persist first and then touch session-scoped state.
 *
 * Both were written as "check the dialog is open, await the call, apply the
 * result". Closing the dialogs on a session switch does not save them: the
 * dialog check has already passed by the time the switch lands mid-await. Each
 * therefore captures the session epoch before awaiting and drops the effects
 * that belong to the originating session if it changed.
 *
 * They live here rather than inline in App so the mid-await switch is covered by
 * a test — App itself is not rendered by the suite — following the same reasoning
 * as sessionLifecycle.js.
 */

/**
 * Persist an edge edit, then update this session's canvas and tell collaborators.
 *
 * The PUT is authoritative and stands regardless. Everything after it is scoped
 * to the session the edit was made in: `edges` is that session's canvas, and
 * syncRef points at whichever session is active when the reply lands — so
 * fanning out after a switch would broadcast the edit into a session it does not
 * belong to.
 *
 * @param {Object} params
 * @param {Object} params.editingEdge  The edge being edited, as opened in the dialog.
 * @param {Object} params.updates  Field updates to persist.
 * @param {Function} params.updateEdge  API call: persist the edit.
 * @param {Array} params.nodes  Current canvas nodes.
 * @param {Array} params.edges  Current canvas edges.
 * @param {Function} params.updateVisualization  Store action: replace the canvas.
 * @param {Object} params.syncRef  Ref holding the active session's sync client.
 * @param {Function} params.setEditingEdge  Store action: set/close the edge dialog.
 * @param {Function} params.showNotification  Surface success/failure to the user.
 * @returns {Promise<boolean>} Whether the session-scoped effects were applied.
 */
export async function applyEdgeUpdate({
  editingEdge,
  updates,
  updateEdge,
  nodes,
  edges,
  updateVisualization,
  syncRef,
  setEditingEdge,
  showNotification,
}) {
  const requestEpoch = useGraphStore.getState().sessionEpoch;
  try {
    await updateEdge(editingEdge.id, updates);
    if (isStaleSessionEpoch(requestEpoch)) return false;
    const newEdges = edges.map((e) => (e.id === editingEdge.id ? { ...e, ...updates } : e));
    updateVisualization(nodes, newEdges);
    // Fan the update out to collaborators: both endpoints already exist on
    // their canvases, so nothing else prompts them to re-render the changed
    // edge; without this they show the stale attributes until reload.
    syncRef.current?.sendEdgesUpdated([{ id: editingEdge.id, ...updates }]);
    setEditingEdge(null);
    showNotification('success', 'Edge updated');
    return true;
  } catch (error) {
    console.error('Error updating edge:', error);
    showNotification('error', 'Could not update edge');
    return false;
  }
}

/**
 * Delete the node(s) a confirmation dialog addresses, then drop them from the canvas.
 *
 * The delete is global and stands either way, so the notification reports it
 * whatever happened to the session. removeNode only maintains the canvas of the
 * session the delete was issued from, though: running it after a switch would
 * edit the new session's canvas instead, so it is skipped and that session
 * reconciles through its own load path.
 *
 * @param {Object} params
 * @param {Object} params.deleteDialog  The pending confirmation ({ nodeId | nodeIds, isMultiple }).
 * @param {Function} params.deleteNodes  API call: delete nodes from the graph.
 * @param {Function} params.removeNode  Store action: drop a node from the canvas.
 * @param {Function} params.setDeleteDialog  Store action: set/close the confirmation.
 * @param {Function} params.showNotification  Surface success/failure to the user.
 * @returns {Promise<boolean>} Whether the canvas was updated.
 */
export async function confirmNodeDelete({
  deleteDialog,
  deleteNodes,
  removeNode,
  setDeleteDialog,
  showNotification,
}) {
  const requestEpoch = useGraphStore.getState().sessionEpoch;
  try {
    const ids = deleteDialog.isMultiple ? deleteDialog.nodeIds : [deleteDialog.nodeId];
    await deleteNodes(ids, true);
    const current = !isStaleSessionEpoch(requestEpoch);
    if (current) ids.forEach((id) => removeNode(id));
    showNotification(
      'success',
      deleteDialog.isMultiple ? `${ids.length} nodes deleted` : 'Node deleted'
    );
    return current;
  } catch (error) {
    console.error('Error deleting node(s):', error);
    showNotification('error', 'Could not delete node(s)');
    return false;
  } finally {
    setDeleteDialog(null);
  }
}
