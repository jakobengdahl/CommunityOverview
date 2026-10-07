import { describe, it, expect, vi, beforeEach } from 'vitest';
import { teleportToSourceGraph } from '../src/utils/teleport';

/**
 * These pin the four defined behaviours of the navigation contract —
 * permission denial, unavailable graph, cross-deployment and backlink — at the
 * one place the UI maps a resolved status onto what the user sees.
 */

function harness(resolved, { confirmAnswer = true } = {}) {
  const notifications = [];
  const opened = [];
  const confirmed = [];
  return {
    notifications,
    opened,
    confirmed,
    run: (overrides = {}) =>
      teleportToSourceGraph({
        nodeId: 'federated::esam-main::remote-1',
        sessionId: '1111-2222',
        searchQuery: 'external',
        t: (key, vars) => (vars ? `${key}:${JSON.stringify(vars)}` : key),
        showNotification: (type, message) => notifications.push([type, message]),
        confirm: (message) => {
          confirmed.push(message);
          return confirmAnswer;
        },
        openUrl: (url) => opened.push(url),
        resolve: typeof resolved === 'function' ? resolved : vi.fn().mockResolvedValue(resolved),
        ...overrides,
      }),
  };
}

describe('teleportToSourceGraph — a resolved route', () => {
  it('opens the route and reports which graph it is opening', async () => {
    const h = harness({
      success: true,
      status: 'ok',
      route: 'https://esam.example/app?node=remote-1',
      origin_graph_id: 'esam-main',
      origin_graph_name: 'eSam',
      cross_deployment: false,
    });

    expect(await h.run()).toBe('ok');
    expect(h.opened).toEqual(['https://esam.example/app?node=remote-1']);
    expect(h.confirmed).toEqual([]);
    expect(h.notifications[0][0]).toBe('info');
    expect(h.notifications[0][1]).toContain('federation.teleport_opening');
    expect(h.notifications[0][1]).toContain('eSam');
  });

  it('passes the session and the search context to the resolver', async () => {
    const resolve = vi.fn().mockResolvedValue({ status: 'local' });
    await harness(resolve).run({ resolve });

    expect(resolve).toHaveBeenCalledWith('federated::esam-main::remote-1', {
      sessionId: '1111-2222',
      searchQuery: 'external',
    });
  });
});

describe('teleportToSourceGraph — behaviour 3: cross-deployment', () => {
  const crossTarget = {
    status: 'ok',
    route: 'https://other.example/app?node=remote-1',
    origin_graph_id: 'esam-main',
    origin_graph_name: 'eSam',
    cross_deployment: true,
  };

  it('confirms before leaving this deployment', async () => {
    const h = harness(crossTarget);

    expect(await h.run()).toBe('ok');
    expect(h.confirmed).toHaveLength(1);
    expect(h.confirmed[0]).toContain('federation.teleport_cross_deployment_confirm');
    expect(h.confirmed[0]).toContain('eSam');
    expect(h.opened).toEqual(['https://other.example/app?node=remote-1']);
  });

  it('opens nothing when the confirmation is declined', async () => {
    const h = harness(crossTarget, { confirmAnswer: false });

    expect(await h.run()).toBe('cancelled');
    expect(h.opened).toEqual([]);
    expect(h.notifications).toEqual([]);
  });

  it('names the graph by its id when no display name came back', async () => {
    const h = harness({ ...crossTarget, origin_graph_name: '' });
    await h.run();

    expect(h.confirmed[0]).toContain('esam-main');
  });
});

describe('teleportToSourceGraph — behaviour 1: permission denial', () => {
  it('reports the denial as an error and opens nothing', async () => {
    const h = harness({
      success: false,
      status: 'permission_denied',
      origin_graph_id: '',
      origin_graph_name: '',
      cross_deployment: false,
    });

    expect(await h.run()).toBe('permission_denied');
    expect(h.opened).toEqual([]);
    expect(h.notifications).toEqual([['error', 'federation.teleport_permission_denied']]);
  });
});

describe('teleportToSourceGraph — behaviour 2: unavailable graph', () => {
  it('names the graph that cannot be reached', async () => {
    const h = harness({
      success: false,
      status: 'graph_unavailable',
      origin_graph_id: 'esam-main',
      origin_graph_name: 'eSam',
      reason: 'graph_unreachable',
      cross_deployment: false,
    });

    expect(await h.run()).toBe('graph_unavailable');
    expect(h.opened).toEqual([]);
    expect(h.notifications[0][0]).toBe('error');
    expect(h.notifications[0][1]).toContain('federation.teleport_graph_unavailable');
    expect(h.notifications[0][1]).toContain('eSam');
  });

  it('falls back to the unnamed message when the graph has no name', async () => {
    const h = harness({
      status: 'graph_unavailable',
      origin_graph_id: '',
      origin_graph_name: '',
    });

    expect(await h.run()).toBe('graph_unavailable');
    expect(h.notifications).toEqual([['error', 'federation.teleport_graph_unavailable_unnamed']]);
  });
});

describe('teleportToSourceGraph — the remaining outcomes', () => {
  it('tells the user a local node is already here', async () => {
    const h = harness({ success: true, status: 'local' });

    expect(await h.run()).toBe('local');
    expect(h.opened).toEqual([]);
    expect(h.notifications).toEqual([['info', 'federation.teleport_already_local']]);
  });

  it('reports an unknown node', async () => {
    const h = harness({ status: 'unknown_node' });

    expect(await h.run()).toBe('unknown_node');
    expect(h.notifications).toEqual([['error', 'federation.teleport_unknown_node']]);
  });

  it('reports a failure when the resolver rejects', async () => {
    const h = harness(() => Promise.reject(new Error('network down')));

    expect(await h.run()).toBe('failed');
    expect(h.opened).toEqual([]);
    expect(h.notifications).toEqual([['error', 'federation.teleport_failed']]);
  });

  it('reports a failure on a status it does not recognise', async () => {
    const h = harness({ status: 'something_new' });

    expect(await h.run()).toBe('failed');
    expect(h.notifications).toEqual([['error', 'federation.teleport_failed']]);
  });

  it('reports a failure rather than throwing on an empty response', async () => {
    const h = harness(undefined);

    expect(await h.run()).toBe('failed');
    expect(h.opened).toEqual([]);
  });

  it('never opens a route for any outcome other than ok', async () => {
    for (const status of [
      'local',
      'permission_denied',
      'graph_unavailable',
      'unknown_node',
      'bogus',
    ]) {
      const h = harness({ status, route: 'https://leak.example/app?node=x' });
      await h.run();
      expect(h.opened, status).toEqual([]);
    }
  });
});

describe('teleportToSourceGraph — defaults', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('opens in a new tab with noopener when no navigator is supplied', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);

    await teleportToSourceGraph({
      nodeId: 'n1',
      t: (k) => k,
      showNotification: () => {},
      resolve: vi.fn().mockResolvedValue({
        status: 'ok',
        route: 'https://esam.example/app',
        cross_deployment: false,
      }),
    });

    expect(open).toHaveBeenCalledWith('https://esam.example/app', '_blank', 'noopener,noreferrer');
  });

  it('gates a cross-deployment hop on window.confirm by default', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);

    const status = await teleportToSourceGraph({
      nodeId: 'n1',
      t: (k) => k,
      showNotification: () => {},
      resolve: vi.fn().mockResolvedValue({
        status: 'ok',
        route: 'https://other.example/app',
        cross_deployment: true,
      }),
    });

    expect(confirmSpy).toHaveBeenCalled();
    expect(status).toBe('cancelled');
    expect(open).not.toHaveBeenCalled();
  });
});
