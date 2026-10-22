/**
 * Which requests carry the session header while the active session is staged
 * (docs/SESSION_OVERLAY_CONTRACT.md §16.1).
 *
 * In a staged session the backend stages a graph write, and composes a graph
 * read, only when the request names the session in
 * `x-communityoverview-session-id`. A write that leaves it out lands in the
 * main graph — exactly the mistake a staged session exists to prevent. So the
 * header goes on:
 *
 * - every route the backend stages or composes on (the list mirrors
 *   backend/api_host/session_binding.py), and
 * - every other graph-changing request, so a route that cannot stage refuses
 *   it with `session_not_supported` instead of writing to the graph.
 *
 * Session routes are left out: they name their session in the path, and
 * creating a session is not a write in one. A few POSTs that never change the
 * graph (teleport resolution, document upload and analysis) are left out too,
 * or the backend would refuse them in a staged session for no reason.
 */

export const SESSION_ID_HEADER = 'x-communityoverview-session-id';

// [method, path below the deployment root]. Kept in step with
// backend/api_host/session_binding.py's _API_SESSION_AWARE/_HOST_SESSION_AWARE.
const SESSION_AWARE = [
  ['POST', /^\/api\/search$/],
  ['GET', /^\/api\/nodes\/[^/]+$/],
  ['POST', /^\/api\/nodes\/[^/]+\/related$/],
  ['POST', /^\/api\/similar$/],
  ['POST', /^\/api\/similar\/batch$/],
  ['POST', /^\/api\/federation\/adopt$/],
  ['POST', /^\/api\/nodes$/],
  ['PATCH', /^\/api\/nodes\/[^/]+$/],
  ['DELETE', /^\/api\/nodes$/],
  ['POST', /^\/api\/nodes\/archive$/],
  ['POST', /^\/api\/edges$/],
  ['PATCH', /^\/api\/edges\/[^/]+$/],
  ['DELETE', /^\/api\/edges\/[^/]+$/],
  ['POST', /^\/api\/edges\/archive$/],
  ['GET', /^\/api\/stats$/],
  ['GET', /^\/api\/meta\/subtypes$/],
  ['GET', /^\/api\/meta\/relationship-applicability\/audit$/],
  ['GET', /^\/api\/views$/],
  ['GET', /^\/api\/views\/[^/]+$/],
  ['GET', /^\/api\/export$/],
  ['POST', /^\/execute_tool$/],
  ['GET', /^\/export_graph$/],
  ['POST', /^\/ui\/chat$/],
  ['POST', /^\/ui\/chat\/simple$/],
];

// POSTs that change nothing in the graph, so they stay usable in a staged
// session without a header the backend would refuse.
const NON_GRAPH_POSTS = [
  /^\/api\/federation\/teleport$/,
  /^\/ui\/propose-nodes$/,
  /^\/ui\/upload(\/.*)?$/,
];

const SESSION_ROUTES = /^\/(api\/)?sessions(\/.*)?$/;

// Session-aware POSTs that only read the graph.
const READ_POSTS = [
  /^\/api\/search$/,
  /^\/api\/nodes\/[^/]+\/related$/,
  /^\/api\/similar$/,
  /^\/api\/similar\/batch$/,
];

const MUTATING = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

/**
 * Whether a request to `path` (below the deployment root, without the query)
 * must carry the staged session's header.
 * @param {string} method
 * @param {string} path
 * @returns {boolean}
 */
export function needsSessionHeader(method, path) {
  const verb = (method || 'GET').toUpperCase();
  if (SESSION_ROUTES.test(path)) return false;
  if (isSessionAware(verb, path)) return true;
  if (!MUTATING.has(verb)) return false;
  return !(verb === 'POST' && NON_GRAPH_POSTS.some((re) => re.test(path)));
}

/**
 * Whether a request that needs the session header may change the graph:
 * everything but plain reads. Chat and tool calls count as writes, since
 * the assistant's tools can write.
 * @param {string} method
 * @param {string} path
 */
export function mayWriteGraph(method, path) {
  const verb = (method || 'GET').toUpperCase();
  if (verb === 'GET') return false;
  if (verb === 'POST' && READ_POSTS.some((re) => re.test(path))) return false;
  return needsSessionHeader(verb, path);
}

/**
 * Whether the backend stages or composes on this route: the header is safe
 * on it in a direct session too, where it changes nothing.
 * @param {string} method
 * @param {string} path
 */
export function isSessionAware(method, path) {
  const verb = (method || 'GET').toUpperCase();
  return SESSION_AWARE.some(([m, re]) => m === verb && re.test(path));
}

/**
 * The path of `url` below `root`, without query or hash.
 * @param {string} url
 * @param {string} root - the deployment path prefix ('' at the origin root)
 * @returns {string}
 */
export function pathBelowRoot(url, root) {
  const { pathname } = new URL(url, window.location.origin);
  return root && pathname.startsWith(root) ? pathname.slice(root.length) : pathname;
}
