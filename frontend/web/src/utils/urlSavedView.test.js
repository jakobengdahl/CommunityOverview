import { describe, it, expect, beforeEach, vi } from 'vitest';

import useGraphStore from '../store/graphStore';
import { loadUrlSavedView } from './urlSavedView';

const t = (key) => key;
const switchSession = () => useGraphStore.getState().resetSessionScopedState(t, 'en');

function deferred() {
  let release;
  let fail;
  const promise = new Promise((resolve, reject) => {
    release = resolve;
    fail = reject;
  });
  return { promise, release: (v) => release(v), reject: (e) => fail(e) };
}

let currentSession = 's1';
const view = { success: true, nodes: [{ id: 'v1' }], edges: [] };

// Models App: the ?session= load paints the canvas when it settles, the view is
// applied on top. `canvas` records the order in which each lands.
function harness() {
  const canvas = [];
  const getSavedView = vi.fn(async () => view);
  const applyView = vi.fn(() => canvas.push('view'));
  return {
    canvas,
    getSavedView,
    applyView,
    sessionId: 's1',
    getCurrentSessionId: () => currentSession,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  useGraphStore.getState().clearVisualization();
  currentSession = 's1';
});

describe('loadUrlSavedView', () => {
  it('view-only: applies the view without waiting for anything', async () => {
    const h = harness();
    const applied = await loadUrlSavedView({ name: 'v', sessionBootstrap: null, ...h });
    expect(applied).toBe(true);
    expect(h.canvas).toEqual(['view']);
  });

  it('both params, session reply first: the view still ends up on top', async () => {
    const h = harness();
    const session = Promise.resolve().then(() => h.canvas.push('session'));
    await loadUrlSavedView({ name: 'v', sessionBootstrap: session, ...h });
    expect(h.canvas).toEqual(['session', 'view']);
  });

  it('both params, session reply last: the view is not fetched until it lands, then wins', async () => {
    const h = harness();
    const gate = deferred();
    const session = gate.promise.then(() => h.canvas.push('session'));
    const done = loadUrlSavedView({ name: 'v', sessionBootstrap: session, ...h });
    await Promise.resolve();
    expect(h.getSavedView).not.toHaveBeenCalled();
    gate.release();
    await done;
    expect(h.canvas).toEqual(['session', 'view']);
  });

  it('failed session load: the view is still applied', async () => {
    const h = harness();
    const session = Promise.reject(new Error('boom'));
    const applied = await loadUrlSavedView({ name: 'v', sessionBootstrap: session, ...h });
    expect(applied).toBe(true);
    expect(h.canvas).toEqual(['view']);
  });

  it('drops the view when the user switched session while it was loading', async () => {
    const h = harness();
    const reply = deferred();
    h.getSavedView.mockReturnValue(reply.promise);
    const done = loadUrlSavedView({ name: 'v', sessionBootstrap: null, ...h });
    await vi.waitFor(() => expect(h.getSavedView).toHaveBeenCalled());
    switchSession();
    reply.release(view);
    expect(await done).toBe(false);
    expect(h.applyView).not.toHaveBeenCalled();
  });

  it('applies nothing for an empty or unsuccessful view', async () => {
    const h = harness();
    h.getSavedView.mockResolvedValue({ success: true, nodes: [] });
    expect(await loadUrlSavedView({ name: 'v', sessionBootstrap: null, ...h })).toBe(false);
    h.getSavedView.mockResolvedValue({ success: false });
    expect(await loadUrlSavedView({ name: 'v', sessionBootstrap: null, ...h })).toBe(false);
    expect(h.applyView).not.toHaveBeenCalled();
  });

  it('drops the view when the user left the URL session while its bootstrap was pending', async () => {
    const h = harness();
    const gate = deferred();
    const done = loadUrlSavedView({ name: 'v', sessionBootstrap: gate.promise, ...h });
    currentSession = 's2';
    gate.release();
    expect(await done).toBe(false);
    expect(h.getSavedView).not.toHaveBeenCalled();
  });

  it('still applies the view when the bootstrap itself resets session-scoped state', async () => {
    const h = harness();
    const session = Promise.resolve().then(() => switchSession());
    expect(await loadUrlSavedView({ name: 'v', sessionBootstrap: session, ...h })).toBe(true);
  });

  it.each([
    ['canvas clear', () => useGraphStore.getState().clearVisualization()],
    ['node addition', () => useGraphStore.getState().addNodesToVisualization([{ id: 'edit' }])],
    ['visibility edit', () => useGraphStore.getState().setHiddenNodeIds(['edit'])],
    ['dimming edit', () => useGraphStore.getState().setDimmedNodeIds(['edit'])],
    ['edge intensity edit', () => useGraphStore.getState().setEdgeIntensity(0.3)],
    ['annotation restore', () => useGraphStore.getState().setPendingAnnotations([{ id: 'note' }])],
  ])('preserves a %s made while fetching the view', async (_label, edit) => {
    const h = harness();
    const reply = deferred();
    h.getSavedView.mockReturnValue(reply.promise);
    const done = loadUrlSavedView({ name: 'v', sessionBootstrap: null, ...h });
    await vi.waitFor(() => expect(h.getSavedView).toHaveBeenCalled());
    edit();
    reply.release(view);
    expect(await done).toBe(false);
    expect(h.applyView).not.toHaveBeenCalled();
  });

  it('allows selection and consumption of bootstrap annotations while fetching the view', async () => {
    const s = useGraphStore.getState();
    s.setPendingAnnotations([{ id: 'bootstrap-note' }]);
    s.setPendingGroups({ groups: [{ id: 'bootstrap-group' }], parentIds: {} });
    const h = harness();
    const reply = deferred();
    h.getSavedView.mockReturnValue(reply.promise);
    const done = loadUrlSavedView({ name: 'v', sessionBootstrap: null, ...h });
    await vi.waitFor(() => expect(h.getSavedView).toHaveBeenCalled());
    s.setSelectedNodeId('v1');
    s.setPendingAnnotations(null);
    s.setPendingGroups(null);
    reply.release(view);
    expect(await done).toBe(true);
    expect(h.applyView).toHaveBeenCalledWith(view);
  });
});
