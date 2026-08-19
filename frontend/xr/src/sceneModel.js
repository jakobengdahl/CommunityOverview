// In-memory scene model for the XR client: the reduction of the shared-session
// op stream (ADR 0003) into the flat, renderer-friendly state the WebGL scene
// draws.
//
// This is the piece that proves the session protocol is renderer-agnostic. The
// ops arrive from `frontend/web/src/services/sessionSyncClient.js` completely
// unchanged — no protocol addition, no XR-specific field — and are folded here
// into node identities, 2D `{x, y}` positions, visibility, presence and remote
// selection claims. `domeLayout.js` then maps those same 2D positions onto the
// dome, so a headset never holds a coordinate a desktop client cannot represent.
//
// Deliberately pure and free of three.js and React, like `domeLayout.js`: it is
// the part of the client worth unit-testing.
//
// Scope: nodes only. Edges and annotations are carried by the protocol but the
// XR scaffold has no renderer for them yet (README "not yet wired"), so their
// ops are ignored rather than reduced into state nothing reads.

export const EMPTY_SCENE = Object.freeze({
  sessionId: null,
  name: null,
  // id -> { id, name, type, x, y, hydrated }
  nodes: Object.freeze({}),
  hiddenNodeIds: Object.freeze([]),
  // Presence members ({ client_id, display_name, color }) of the other clients.
  roster: Object.freeze([]),
  // element_id -> { clientId, color, displayName }, other clients' claims only.
  claims: Object.freeze({}),
});

function emptyNode(id) {
  // A `nodes_added` op carries ids only, so identity and position both arrive
  // later — the name/type from a REST hydration, the position from the
  // `node_moved` / `layout_applied` op the originator emits in the same batch.
  // Until then the node is known but not renderable (see `renderableNodes`).
  return { id, name: null, type: null, x: null, y: null, hydrated: false };
}

function coord(value) {
  return Number.isFinite(value) ? value : null;
}

function withPosition(node, position) {
  const x = coord(position?.x);
  const y = coord(position?.y);
  if (x === null || y === null) return node;
  return { ...node, x, y };
}

function addIds(list, ids) {
  return Array.from(new Set([...list, ...(ids || []).filter((id) => typeof id === 'string')]));
}

function removeIds(list, ids) {
  const drop = new Set(ids || []);
  return list.filter((id) => !drop.has(id));
}

/**
 * Build a scene from the resolved session payload of
 * `GET /api/sessions/{id}?resolve=true` — the same authoritative read the 2D
 * client uses on connect and on every resync.
 */
export function sceneFromSession(payload, { sessionId = null } = {}) {
  const state = payload?.state || {};
  const positions = state.positions || {};
  const nodes = {};
  for (const node of payload?.resolved?.nodes || []) {
    if (!node?.id) continue;
    nodes[node.id] = withPosition(
      { ...emptyNode(node.id), name: node.name ?? null, type: node.type ?? null, hydrated: true },
      positions[node.id]
    );
  }
  return {
    ...EMPTY_SCENE,
    sessionId: sessionId ?? payload?.id ?? null,
    name: payload?.name ?? null,
    nodes,
    hiddenNodeIds: addIds([], state.hidden_node_ids),
  };
}

/**
 * Fold one applied op into the scene. Pure — returns a new scene, and returns
 * the input unchanged when the op names nothing this scene models.
 */
export function applyOp(scene, op) {
  const type = op?.op;
  switch (type) {
    case 'nodes_added': {
      const added = (op.node_ids || []).filter((id) => typeof id === 'string' && !scene.nodes[id]);
      if (!added.length) return scene;
      const nodes = { ...scene.nodes };
      for (const id of added) nodes[id] = emptyNode(id);
      return { ...scene, nodes };
    }
    case 'nodes_removed': {
      const drop = (op.node_ids || []).filter((id) => scene.nodes[id]);
      if (!drop.length) return scene;
      const nodes = { ...scene.nodes };
      for (const id of drop) delete nodes[id];
      return {
        ...scene,
        nodes,
        // A removed node must not keep a hide entry: re-adding the same node
        // later would otherwise resurrect it invisible.
        hiddenNodeIds: removeIds(scene.hiddenNodeIds, drop),
      };
    }
    case 'node_moved': {
      const node = scene.nodes[op.node_id];
      if (!node) return scene;
      const moved = withPosition(node, op.position);
      if (moved === node) return scene;
      return { ...scene, nodes: { ...scene.nodes, [op.node_id]: moved } };
    }
    case 'layout_applied': {
      // Positions for ids this client has never seen are dropped rather than
      // materialising an anonymous node: identity only ever comes from
      // `nodes_added` + hydration, so a ghost entry here would render as an
      // unnamed box that no later op can explain.
      let changed = false;
      const nodes = { ...scene.nodes };
      for (const [id, position] of Object.entries(op.positions || {})) {
        const node = nodes[id];
        if (!node) continue;
        const moved = withPosition(node, position);
        if (moved === node) continue;
        nodes[id] = moved;
        changed = true;
      }
      return changed ? { ...scene, nodes } : scene;
    }
    case 'nodes_hidden':
      return { ...scene, hiddenNodeIds: addIds(scene.hiddenNodeIds, op.node_ids) };
    case 'nodes_shown':
      return { ...scene, hiddenNodeIds: removeIds(scene.hiddenNodeIds, op.node_ids) };
    case 'session_renamed':
      return { ...scene, name: op.name ?? null };
    default:
      // Edge, annotation and group ops: carried by the protocol, not rendered
      // by this client yet. Ignoring them keeps the scene to what is drawn.
      return scene;
  }
}

/** Fold a batch of ops in order. */
export function applyOps(scene, ops) {
  return (ops || []).reduce(applyOp, scene);
}

/**
 * Merge REST-hydrated node details (name, type) into nodes the op stream only
 * knows by id. Unknown ids are ignored: a node removed while its hydration was
 * in flight must not come back.
 */
export function hydrateNodes(scene, nodes) {
  let changed = false;
  const next = { ...scene.nodes };
  for (const node of nodes || []) {
    const current = next[node?.id];
    if (!current) continue;
    next[node.id] = {
      ...current,
      name: node.name ?? current.name,
      type: node.type ?? current.type,
      hydrated: true,
    };
    changed = true;
  }
  return changed ? { ...scene, nodes: next } : scene;
}

/** Ids the op stream introduced whose name/type have not been fetched yet. */
export function pendingNodeIds(scene) {
  return Object.values(scene.nodes)
    .filter((n) => !n.hydrated)
    .map((n) => n.id);
}

export function withRoster(scene, roster) {
  return { ...scene, roster: (roster || []).filter((m) => m && m.client_id) };
}

export function withClaims(scene, claims) {
  return { ...scene, claims: claims || {} };
}

/**
 * The nodes the renderer draws: visible, positioned, and annotated with the
 * remote claim (if any) so a collaborator's selection is visible in-headset.
 * Sorted by id so the scene graph keeps a stable order across re-renders.
 */
export function renderableNodes(scene) {
  const hidden = new Set(scene.hiddenNodeIds);
  return Object.values(scene.nodes)
    .filter((n) => !hidden.has(n.id) && n.x !== null && n.y !== null)
    .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
    .map((n) => ({ ...n, claim: scene.claims[n.id] || null }));
}
