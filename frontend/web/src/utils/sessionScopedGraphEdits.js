import useGraphStore, { isStaleSessionEpoch } from '../store/graphStore';
import { savedViewMetadataToCanvasMetadata } from './sessionAnnotations';

/**
 * Dialog-driven graph edits guard against stale post-await canvas writes.
 *
 * These handlers were written as "check the dialog is open, await the call,
 * apply the result". Closing the dialogs on a session switch or canvas clear
 * does not save them: the dialog check has already passed by the time the
 * invalidating event lands mid-await. Each therefore captures the relevant
 * epoch before awaiting and drops effects that target stale canvas state.
 *
 * They follow the same rule about what survives a stale request: the persisted
 * mutation is global and permanent, so the user is told it happened either way,
 * but nothing session-scoped is touched — no canvas edit, no sync fan-out, and
 * no dialog state, since by then those belong to the session the user moved to.
 *
 * Two guards apply, and which one a handler needs depends on what its post-await
 * write depends on. A switch bumps sessionEpoch; a clear or any other wholesale
 * replace of the canvas bumps canvasBaselineEpoch but not sessionEpoch. An effect
 * that goes through an in-place store action (updateEdgeData, removeEdge,
 * addNodesToVisualization of something new) is only wrong after a switch. One
 * that writes back a canvas captured before the await, or builds on content that
 * a clear removes, is wrong after either — see captureCanvasScope.
 *
 * The `?view=` URL load in App is the one awaiting handler not routed through
 * here: it races the `?session=` bootstrap load, and which of the two should
 * win when both are in the URL is an open product decision, not a guard.
 *
 * They live here rather than inline in App so the mid-await switch is covered by
 * a test — App itself is not rendered by the suite — following the same reasoning
 * as sessionLifecycle.js.
 */

/**
 * Persist an edge edit, then update this session's canvas and tell collaborators.
 *
 * The PUT is authoritative and stands regardless, so the user is told the edit
 * landed whatever happened to the session. Everything after it is scoped to the
 * session the edit was made in: `edges` is that session's canvas, syncRef points
 * at whichever session is active when the reply lands — so fanning out after a
 * switch would broadcast the edit into a session it does not belong to — and the
 * edge dialog, if one is open by then, is the new session's.
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
  const { sessionEpoch: requestEpoch, canvasBaselineEpoch: requestBaselineEpoch } =
    useGraphStore.getState();
  try {
    await updateEdge(editingEdge.id, updates);
    // Reported at the end of each branch rather than once above them: the
    // session-scoped work below is still inside this try, so announcing success
    // before it runs would let a throw there follow "Edge updated" with "Could
    // not update edge" for a PUT that did land.
    if (
      isStaleSessionEpoch(requestEpoch) ||
      useGraphStore.getState().canvasBaselineEpoch !== requestBaselineEpoch
    ) {
      showNotification('success', 'Edge updated');
      return false;
    }
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
 * Closing the confirmation is session-scoped for the same reason. The switch has
 * already closed this session's copy, so a late close can only reach a *new*
 * confirmation the user opened in the session they moved to — dismissing a
 * delete they never got to confirm.
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
    if (!isStaleSessionEpoch(requestEpoch)) setDeleteDialog(null);
  }
}

/**
 * Capture the session and canvas the caller is about to await on.
 *
 * `sessionChanged` is true once the user has switched session. `canvasReplaced`
 * is also true once this session's canvas was cleared or replaced wholesale —
 * the case sessionEpoch alone misses, since a clear does not bump it.
 *
 * @returns {{ sessionChanged: () => boolean, canvasReplaced: () => boolean }}
 */
export function captureCanvasScope() {
  const { sessionEpoch, canvasBaselineEpoch } = useGraphStore.getState();
  const sessionChanged = () => isStaleSessionEpoch(sessionEpoch);
  return {
    sessionChanged,
    canvasReplaced: () =>
      sessionChanged() || useGraphStore.getState().canvasBaselineEpoch !== canvasBaselineEpoch,
  };
}

/**
 * Persist field updates to one or more nodes, then patch them on this canvas.
 *
 * The write-back replaces the whole canvas with `nodes`/`edges` as they were
 * when the edit started, so it is dropped once that canvas is gone — after a
 * switch it would put the previous session's contents onto the new one, and
 * after a clear it would restore everything the user just cleared.
 * `onApplied` runs only with the write-back: whatever it closes or resets is,
 * by then, state the user opened after moving on.
 *
 * Errors from the API propagate so each caller keeps its own message.
 *
 * @param {Object} params
 * @param {Array<{id: string, updates: Object}>} params.entries  Updates, persisted in order.
 * @param {Function} params.updateNode  API call: persist one node's updates.
 * @param {Array} params.nodes  Canvas nodes when the edit started.
 * @param {Array} params.edges  Canvas edges when the edit started.
 * @param {Function} params.updateVisualization  Store action: replace the canvas.
 * @param {Function} [params.onApplied]  Session-scoped follow-up, e.g. closing the dialog.
 * @returns {Promise<boolean>} Whether the canvas was updated.
 */
export async function persistNodeUpdates({
  entries,
  updateNode,
  nodes,
  edges,
  updateVisualization,
  onApplied,
}) {
  const scope = captureCanvasScope();
  for (const { id, updates } of entries) {
    await updateNode(id, updates);
  }
  if (scope.canvasReplaced()) return false;
  const byId = new Map(entries.map(({ id, updates }) => [id, updates]));
  updateVisualization(
    nodes.map((n) => (byId.has(n.id) ? { ...n, ...byId.get(n.id) } : n)),
    edges
  );
  onApplied?.();
  return true;
}

/**
 * Create nodes (and edges) from a dialog, then show them on this session's canvas.
 *
 * The nodes exist in the graph either way. Drawing them is additive, so a clear
 * in the meantime does not make it wrong — the user still asked to see what they
 * created — but a switch does: the new session never asked for them.
 *
 * Errors from the API propagate so each caller keeps its own message.
 *
 * @param {Object} params
 * @param {Array} params.nodes  Nodes to create.
 * @param {Array} [params.edges]  Edges to create with them.
 * @param {Function} params.addNodes  API call: create nodes and edges.
 * @param {Function} params.addNodesToVisualization  Store action: add to the canvas.
 * @param {Function} params.toCanvas  Map the API result to `{ nodes, edges }` to draw,
 *   or null when there is nothing to draw.
 * @returns {Promise<boolean>} Whether the canvas was updated.
 */
export async function persistNewNodes({
  nodes,
  edges = [],
  addNodes,
  addNodesToVisualization,
  toCanvas,
}) {
  const scope = captureCanvasScope();
  const result = await addNodes(nodes, edges);
  if (scope.sessionChanged()) return false;
  const drawn = toCanvas(result);
  if (!drawn) return false;
  addNodesToVisualization(drawn.nodes, drawn.edges || []);
  return true;
}

/**
 * Add a node's neighbours to this canvas.
 *
 * Nothing is persisted, so a stale result is simply dropped. The expansion hangs
 * off a node on the canvas it was asked from; once that canvas is cleared or
 * switched away the anchor is gone and the neighbours would land unattached.
 *
 * @param {Object} params
 * @param {string} params.nodeId  The node to expand.
 * @param {Array} params.nodes  Canvas nodes when the expansion started.
 * @param {Function} params.getRelatedNodes  API call: fetch the neighbourhood.
 * @param {Function} params.addNodesToVisualization  Store action: add to the canvas.
 * @param {Function} params.showNotification  Surface the outcome to the user.
 * @returns {Promise<boolean>} Whether the result was applied (or reported as empty).
 */
export async function expandNode({
  nodeId,
  nodes,
  getRelatedNodes,
  addNodesToVisualization,
  showNotification,
}) {
  const scope = captureCanvasScope();
  try {
    const result = await getRelatedNodes(nodeId, { depth: 1 });
    if (scope.canvasReplaced()) return false;
    if (result.nodes && result.nodes.length > 0) {
      const existingIds = new Set(nodes.map((n) => n.id));
      const newCount = result.nodes.filter((n) => !existingIds.has(n.id)).length;
      addNodesToVisualization(result.nodes, result.edges || []);
      if (newCount > 0) {
        showNotification('success', `Added ${newCount} new node${newCount !== 1 ? 's' : ''}`);
      } else {
        showNotification('info', 'All related nodes already in view');
      }
    } else {
      showNotification('info', 'No related nodes found');
    }
    return true;
  } catch (error) {
    console.error('Error expanding node:', error);
    showNotification('error', 'Could not expand node');
    return false;
  }
}

/**
 * Persist a drag-connect, then draw the edge and tell collaborators.
 *
 * The edge exists in the graph either way. Drawing it needs both endpoints on
 * the canvas the drag happened on, so it is dropped once that canvas is cleared
 * or switched away, and so is the fan-out: after a switch syncRef points at the
 * new session, and after a clear the endpoints are gone for collaborators too.
 *
 * @param {Object} params
 * @param {string} params.source  Source node id.
 * @param {string} params.target  Target node id.
 * @param {Function} params.addEdge  API call: create the edge.
 * @param {Function} params.addNodesToVisualization  Store action: add to the canvas.
 * @param {Object} params.syncRef  Ref holding the active session's sync client.
 * @param {Function} params.showNotification  Surface failure to the user.
 * @returns {Promise<boolean>} Whether the edge was drawn.
 */
export async function connectNodes({
  source,
  target,
  addEdge,
  addNodesToVisualization,
  syncRef,
  showNotification,
}) {
  const scope = captureCanvasScope();
  try {
    const result = await addEdge(source, target);
    if (!result.success || !result.edge) {
      // The edge is only drawn once persisted, so a non-success response must
      // surface an error rather than silently leaving nothing on the canvas.
      showNotification('error', 'Could not create connection');
      return false;
    }
    if (scope.canvasReplaced()) return false;
    addNodesToVisualization([], [result.edge]);
    // Fan the new edge out to collaborators. Both endpoints already exist
    // on their canvases, so nothing else prompts them to re-hydrate it
    // (no node was added); without this the edge renders only locally.
    syncRef.current?.sendEdgesAdded([result.edge]);
    return true;
  } catch (error) {
    console.error('Error creating edge:', error);
    showNotification('error', 'Could not create connection');
    return false;
  }
}

/**
 * Delete an edge from the graph, then from this canvas and collaborators' canvases.
 *
 * The delete is global and stands, so the user is told either way. removeEdge
 * acts on the store in place and is a no-op for an edge a clear already removed,
 * so only a switch drops it — together with the fan-out, which would otherwise
 * go to the new session.
 *
 * @param {Object} params
 * @param {string} params.edgeId  The edge to delete.
 * @param {Function} params.deleteEdge  API call: delete the edge.
 * @param {Function} params.removeEdge  Store action: drop the edge from the canvas.
 * @param {Object} params.syncRef  Ref holding the active session's sync client.
 * @param {Function} params.showNotification  Surface success/failure to the user.
 * @returns {Promise<boolean>} Whether the session-scoped effects were applied.
 */
export async function deleteEdgeEverywhere({
  edgeId,
  deleteEdge,
  removeEdge,
  syncRef,
  showNotification,
}) {
  const scope = captureCanvasScope();
  try {
    const result = await deleteEdge(edgeId);
    if (!result?.success) {
      throw new Error('Could not delete edge');
    }
    const current = !scope.sessionChanged();
    if (current) {
      removeEdge(edgeId);
      // Fan the deletion out to collaborators. Both endpoints already exist on
      // their canvases, so nothing else prompts them to drop the edge (no node
      // was removed); without this the edge lingers on their canvas until reload.
      syncRef.current?.sendEdgesRemoved([edgeId]);
    }
    showNotification('success', 'Edge deleted');
    return current;
  } catch (error) {
    console.error('Error deleting edge:', error);
    showNotification('error', 'Could not delete edge');
    return false;
  }
}

/**
 * Persist an edge's new relationship type, then retype it here and for collaborators.
 *
 * Same shape as deleteEdgeEverywhere: the write is global and reported either
 * way, updateEdgeData edits the store in place, and only a switch drops the
 * local edit and the fan-out.
 *
 * @param {Object} params
 * @param {string} params.edgeId  The edge to retype.
 * @param {string|null} params.type  New type; empty resets to RELATES_TO.
 * @param {Function} params.updateEdge  API call: persist the edit.
 * @param {Function} params.updateEdgeData  Store action: patch one edge in place.
 * @param {Object} params.syncRef  Ref holding the active session's sync client.
 * @param {Function} params.showNotification  Surface success/failure to the user.
 * @returns {Promise<boolean>} Whether the session-scoped effects were applied.
 */
export async function setEdgeType({
  edgeId,
  type,
  updateEdge,
  updateEdgeData,
  syncRef,
  showNotification,
}) {
  const scope = captureCanvasScope();
  try {
    await updateEdge(edgeId, { type: type || null });
    const current = !scope.sessionChanged();
    if (current) {
      const nextType = type || 'RELATES_TO';
      updateEdgeData(edgeId, { type: nextType });
      // Fan the type change out to collaborators: both endpoints already exist
      // on their canvases, so nothing else prompts them to re-render the edge;
      // without this they keep showing the old type until reload.
      syncRef.current?.sendEdgesUpdated([{ id: edgeId, type: nextType }]);
    }
    showNotification('success', 'Connection type updated');
    return current;
  } catch (error) {
    console.error('Error updating edge type:', error);
    showNotification('error', 'Could not update connection');
    return false;
  }
}

/**
 * Load a SavedView node's contents onto the canvas, replacing what is there.
 *
 * The canvas is cleared before the node fetches, so the scope is captured after
 * that clear. A switch or a later wholesale replace — another saved view, a
 * session load, the clear action — supersedes this load, and its nodes must not
 * be added on top of whatever replaced them.
 *
 * @param {Object} params
 * @param {Object} params.nodeData  The SavedView node.
 * @param {Function} params.getNodeDetails  API call: fetch one node with its edges.
 * @param {Function} params.clearVisualization  Store action: empty the canvas.
 * @param {Function} params.addNodesToVisualization  Store action: add to the canvas.
 * @param {Function} params.setPendingGroups  Store action: queue groups to restore.
 * @param {Function} params.setPendingAnnotations  Store action: queue annotations to restore.
 * @param {Function} params.showNotification  Surface success/failure to the user.
 * @returns {Promise<boolean>} Whether the load completed on the canvas it started on.
 */
export async function loadSavedViewNode({
  nodeData,
  getNodeDetails,
  clearVisualization,
  addNodesToVisualization,
  setPendingGroups,
  setPendingAnnotations,
  showNotification,
}) {
  try {
    const nodeIds = nodeData.metadata?.node_ids || [];
    const positions = nodeData.metadata?.positions || {};
    const savedEdges = nodeData.metadata?.edges || [];
    const savedViewAnnotations = savedViewMetadataToCanvasMetadata(nodeData.metadata || {});
    if (nodeIds.length > 0) {
      clearVisualization();
      const scope = captureCanvasScope();
      const details = await Promise.all(nodeIds.map((id) => getNodeDetails(id).catch(() => null)));
      if (scope.canvasReplaced()) return false;
      const loadedNodes = details
        .filter((d) => d?.success)
        .map((d) => {
          const n = d.node;
          if (positions[n.id]) {
            return { ...n, _savedPosition: positions[n.id] };
          }
          return n;
        });
      if (loadedNodes.length > 0) {
        let edgesToLoad = savedEdges.length > 0 ? savedEdges : [];
        if (edgesToLoad.length === 0) {
          const loadedIds = new Set(loadedNodes.map((n) => n.id));
          const savedEdgeIds = new Set(nodeData.metadata?.edge_ids || []);
          for (const d of details) {
            if (d?.edges) {
              const relevant = d.edges.filter(
                (e) =>
                  loadedIds.has(e.source) &&
                  loadedIds.has(e.target) &&
                  (savedEdgeIds.size === 0 || savedEdgeIds.has(e.id))
              );
              edgesToLoad.push(...relevant);
            }
          }
        }
        const edgeMap = new Map(edgesToLoad.map((e) => [e.id, e]));
        addNodesToVisualization(loadedNodes, Array.from(edgeMap.values()));
        if (savedViewAnnotations.groups.length > 0) {
          setPendingGroups({
            groups: savedViewAnnotations.groups,
            parentIds: savedViewAnnotations.parentIds,
          });
        }
        if (savedViewAnnotations.annotations.length > 0) {
          setPendingAnnotations(savedViewAnnotations.annotations);
        }
      }
    }
    showNotification('info', `Loaded saved view: ${nodeData.name || nodeData.label}`);
    return true;
  } catch (err) {
    console.error('Error loading saved view:', err);
    showNotification('error', 'Could not load saved view');
    return false;
  }
}
