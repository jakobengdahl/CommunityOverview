/**
 * Leave for the graph that owns a node.
 *
 * The server resolves the route and decides, from the request's own graph
 * access, whether there is one to hand back at all; every branch below is a
 * status it reported rather than something this module inferred from the node
 * in hand. Keeping the mapping from status to user-visible outcome in one
 * place is what stops the search-result action and the canvas context-menu
 * action from answering the same four defined behaviours differently —
 * permission denial, unavailable graph, cross-deployment and backlink.
 *
 * Opening in a new tab keeps the current session alive to come back to, and a
 * hop to another deployment is confirmed first because it leaves this
 * installation.
 */

import * as api from '../services/api';

/**
 * @param {Object} params
 * @param {string} params.nodeId - Node to teleport from
 * @param {string} [params.sessionId] - Session offered as the way back
 * @param {string} [params.searchQuery] - Search context to carry along
 * @param {Function} params.t - Translation function
 * @param {Function} params.showNotification - (type, message) => void
 * @param {Function} [params.confirm] - Confirmation gate, defaults to window.confirm
 * @param {Function} [params.openUrl] - Navigator, defaults to window.open in a new tab
 * @param {Function} [params.resolve] - Resolver, defaults to api.resolveTeleport
 * @returns {Promise<string>} The status acted on, or 'failed'
 */
export async function teleportToSourceGraph({
  nodeId,
  sessionId = '',
  searchQuery = '',
  t,
  showNotification,
  confirm,
  openUrl,
  resolve,
}) {
  const resolveTarget = resolve || api.resolveTeleport;
  const ask = confirm || ((message) => window.confirm(message));
  const open = openUrl || ((url) => window.open(url, '_blank', 'noopener,noreferrer'));

  let target;
  try {
    target = await resolveTarget(nodeId, { sessionId, searchQuery });
  } catch (err) {
    console.error('Teleport resolve failed:', err);
    showNotification('error', t('federation.teleport_failed'));
    return 'failed';
  }

  const graphLabel = target?.origin_graph_name || target?.origin_graph_id;

  switch (target?.status) {
    case 'ok': {
      if (
        target.cross_deployment &&
        !ask(t('federation.teleport_cross_deployment_confirm', { graph: graphLabel }))
      ) {
        return 'cancelled';
      }
      showNotification('info', t('federation.teleport_opening', { graph: graphLabel }));
      open(target.route);
      return 'ok';
    }
    case 'local':
      showNotification('info', t('federation.teleport_already_local'));
      return 'local';
    case 'permission_denied':
      showNotification('error', t('federation.teleport_permission_denied'));
      return 'permission_denied';
    case 'graph_unavailable':
      showNotification(
        'error',
        graphLabel
          ? t('federation.teleport_graph_unavailable', { graph: graphLabel })
          : t('federation.teleport_graph_unavailable_unnamed')
      );
      return 'graph_unavailable';
    case 'unknown_node':
      showNotification('error', t('federation.teleport_unknown_node'));
      return 'unknown_node';
    default:
      showNotification('error', t('federation.teleport_failed'));
      return 'failed';
  }
}
