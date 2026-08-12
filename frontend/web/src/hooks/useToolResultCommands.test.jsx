import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';

import * as api from '../services/api';
import { positionNewNodes } from '@community-graph/ui-graph-canvas';
import useGraphStore from '../store/graphStore';
import { useToolResultCommands } from './useToolResultCommands';

vi.mock('../services/api', () => ({
  getVisualizationStreamUrl: vi.fn((id) => `/viz/${id}`),
}));

vi.mock('@community-graph/ui-graph-canvas', () => ({
  // Identity-ish: tag nodes so a test can assert positioning ran on them.
  positionNewNodes: vi.fn((nodes) => nodes.map((n) => ({ ...n, positioned: true }))),
}));

vi.mock('../store/graphStore', () => {
  const state = {
    nodes: [{ id: 'existing' }],
    edges: [{ id: 'e-existing', source: 'existing', target: 'existing' }],
    addNodesToVisualization: vi.fn(),
    updateVisualization: vi.fn(),
    clearVisualization: vi.fn(),
    pulseNode: vi.fn(),
  };
  return { default: { getState: vi.fn(() => state) } };
});

// Observable EventSource stub so the legacy-stream effect is testable.
class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.onmessage = null;
    this.onerror = null;
    this.closed = false;
    FakeEventSource.instances.push(this);
  }
  close() {
    this.closed = true;
  }
}
FakeEventSource.instances = [];

const store = () => useGraphStore.getState();

beforeEach(() => {
  vi.clearAllMocks();
  FakeEventSource.instances = [];
  global.EventSource = FakeEventSource;
  const s = store();
  s.nodes = [{ id: 'existing' }];
  s.edges = [{ id: 'e-existing', source: 'existing', target: 'existing' }];
});

const render = (opts = {}) =>
  renderHook(() =>
    useToolResultCommands({
      sessionId: '1111-2222',
      opStreamReady: false,
      latestViewport: { current: null },
      ...opts,
    })
  );

describe('useToolResultCommands.applyToolResultCommand', () => {
  it('ignores a null tool result', () => {
    const { result } = render();
    act(() => result.current.applyToolResultCommand(null, 'cmd-1'));
    expect(store().updateVisualization).not.toHaveBeenCalled();
    expect(store().addNodesToVisualization).not.toHaveBeenCalled();
    expect(store().clearVisualization).not.toHaveBeenCalled();
  });

  it('dedupes by command_id — the same id is applied only once', () => {
    const { result } = render();
    const toolResult = { action: 'load_visualization', nodes: [{ id: 'n1', type: 'Goal' }] };
    act(() => result.current.applyToolResultCommand(toolResult, 'cmd-1'));
    act(() => result.current.applyToolResultCommand(toolResult, 'cmd-1'));
    expect(store().clearVisualization).toHaveBeenCalledTimes(1);
  });

  it('applies a distinct command_id even with identical payload', () => {
    const { result } = render();
    const toolResult = { action: 'load_visualization', nodes: [{ id: 'n1', type: 'Goal' }] };
    act(() => result.current.applyToolResultCommand(toolResult, 'cmd-1'));
    act(() => result.current.applyToolResultCommand(toolResult, 'cmd-2'));
    expect(store().clearVisualization).toHaveBeenCalledTimes(2);
  });

  it('add_to_visualization positions new nodes and adds them, dropping Community nodes', () => {
    const { result } = render();
    act(() =>
      result.current.applyToolResultCommand(
        {
          action: 'add_to_visualization',
          nodes: [
            { id: 'n1', type: 'Goal' },
            { id: 'c1', type: 'Community' },
          ],
          edges: [{ id: 'e1' }],
        },
        'cmd-1'
      )
    );
    expect(positionNewNodes).toHaveBeenCalledTimes(1);
    const [filtered] = positionNewNodes.mock.calls[0];
    expect(filtered.map((n) => n.id)).toEqual(['n1']);
    expect(store().addNodesToVisualization).toHaveBeenCalledWith(
      [{ id: 'n1', type: 'Goal', positioned: true }],
      [{ id: 'e1' }]
    );
  });

  it('add_to_visualization uses the latest viewport centre when available', () => {
    const latestViewport = { current: { x: 10, y: 20, zoom: 2 } };
    const { result } = render({ latestViewport });
    act(() =>
      result.current.applyToolResultCommand(
        { action: 'add_to_visualization', nodes: [{ id: 'n1', type: 'Goal' }] },
        'cmd-1'
      )
    );
    const opts = positionNewNodes.mock.calls[0][3];
    expect(opts.viewportCenter).toEqual({
      x: (window.innerWidth / 2 - 10) / 2,
      y: (window.innerHeight / 2 - 20) / 2,
    });
  });

  it('load_visualization clears then replaces the canvas', () => {
    const { result } = render();
    act(() =>
      result.current.applyToolResultCommand(
        { action: 'load_visualization', nodes: [{ id: 'n1', type: 'Goal' }], edges: [] },
        'cmd-1'
      )
    );
    expect(store().clearVisualization).toHaveBeenCalledTimes(1);
    expect(store().updateVisualization).toHaveBeenCalledWith([{ id: 'n1', type: 'Goal' }], []);
  });

  it('clear_visualization with no nodes clears without a replace', () => {
    const { result } = render();
    act(() =>
      result.current.applyToolResultCommand({ action: 'clear_visualization', nodes: [] }, 'cmd-1')
    );
    expect(store().clearVisualization).toHaveBeenCalledTimes(1);
    expect(store().updateVisualization).not.toHaveBeenCalled();
  });

  it('replace_visualization clears then replaces the canvas', () => {
    const { result } = render();
    act(() =>
      result.current.applyToolResultCommand(
        { action: 'replace_visualization', nodes: [{ id: 'n1', type: 'Goal' }], edges: [] },
        'cmd-1'
      )
    );
    expect(store().clearVisualization).toHaveBeenCalledTimes(1);
    expect(store().updateVisualization).toHaveBeenCalledWith([{ id: 'n1', type: 'Goal' }], []);
    expect(store().addNodesToVisualization).not.toHaveBeenCalled();
  });

  it('no explicit action with nodes defaults to additive, not a full replace', () => {
    const { result } = render();
    act(() =>
      result.current.applyToolResultCommand({ nodes: [{ id: 'n1', type: 'Goal' }] }, 'cmd-1')
    );
    // Additive default: nodes are merged in, the existing view is never cleared
    // or replaced. This is the guard against a plain "add X" wiping the canvas.
    expect(positionNewNodes).toHaveBeenCalledTimes(1);
    expect(store().addNodesToVisualization).toHaveBeenCalledWith(
      [{ id: 'n1', type: 'Goal', positioned: true }],
      []
    );
    expect(store().updateVisualization).not.toHaveBeenCalled();
    expect(store().clearVisualization).not.toHaveBeenCalled();
  });

  it('an unrecognised action with nodes defaults to additive (never clears)', () => {
    const { result } = render();
    act(() =>
      result.current.applyToolResultCommand(
        { action: 'something_else', nodes: [{ id: 'n1', type: 'Goal' }] },
        'cmd-1'
      )
    );
    expect(store().addNodesToVisualization).toHaveBeenCalledWith(
      [{ id: 'n1', type: 'Goal', positioned: true }],
      []
    );
    expect(store().updateVisualization).not.toHaveBeenCalled();
    expect(store().clearVisualization).not.toHaveBeenCalled();
  });
});

describe('useToolResultCommands.applyPulseCommand', () => {
  it('pulses the targeted node with mapped options', () => {
    const { result } = render();
    act(() =>
      result.current.applyPulseCommand(
        { node_id: 'n1', pulse: { style: 'grow', color: '#ff0000', duration_ms: 2000 } },
        'cmd-1'
      )
    );
    expect(store().pulseNode).toHaveBeenCalledWith('n1', {
      style: 'grow',
      color: '#ff0000',
      durationMs: 2000,
    });
  });

  it('ignores a pulse without a node_id', () => {
    const { result } = render();
    act(() => result.current.applyPulseCommand({ pulse: { style: 'glow' } }, 'cmd-1'));
    expect(store().pulseNode).not.toHaveBeenCalled();
  });

  it('dedupes a pulse by command_id', () => {
    const { result } = render();
    const cmd = { node_id: 'n1', pulse: {} };
    act(() => result.current.applyPulseCommand(cmd, 'cmd-1'));
    act(() => result.current.applyPulseCommand(cmd, 'cmd-1'));
    expect(store().pulseNode).toHaveBeenCalledTimes(1);
  });
});

describe('useToolResultCommands legacy SSE stream', () => {
  it('opens the push stream for the session when the op stream is not ready', () => {
    render({ opStreamReady: false });
    expect(api.getVisualizationStreamUrl).toHaveBeenCalledWith('1111-2222');
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe('/viz/1111-2222');
  });

  it('does not open the push stream once the op stream is ready', () => {
    render({ opStreamReady: true });
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it('closes the stream on unmount', () => {
    const { unmount } = render();
    const es = FakeEventSource.instances[0];
    expect(es.closed).toBe(false);
    unmount();
    expect(es.closed).toBe(true);
  });

  it('routes a tool_result message through applyToolResultCommand', () => {
    render();
    const es = FakeEventSource.instances[0];
    act(() =>
      es.onmessage({
        data: JSON.stringify({
          type: 'tool_result',
          command_id: 'cmd-1',
          result: { action: 'load_visualization', nodes: [{ id: 'n1', type: 'Goal' }] },
        }),
      })
    );
    expect(store().clearVisualization).toHaveBeenCalledTimes(1);
  });

  it('routes a node_pulse message through applyPulseCommand', () => {
    render();
    const es = FakeEventSource.instances[0];
    act(() =>
      es.onmessage({
        data: JSON.stringify({
          type: 'node_pulse',
          command_id: 'cmd-1',
          node_id: 'n1',
          pulse: { style: 'marker', color: null, duration_ms: 1500 },
        }),
      })
    );
    expect(store().pulseNode).toHaveBeenCalledWith('n1', {
      style: 'marker',
      color: null,
      durationMs: 1500,
    });
  });

  it('ignores ping, connected and non-tool_result messages', () => {
    render();
    const es = FakeEventSource.instances[0];
    act(() => {
      es.onmessage({ data: JSON.stringify({ type: 'ping' }) });
      es.onmessage({ data: JSON.stringify({ type: 'connected' }) });
      es.onmessage({ data: JSON.stringify({ type: 'other', result: { action: 'x' } }) });
    });
    expect(store().clearVisualization).not.toHaveBeenCalled();
    expect(store().updateVisualization).not.toHaveBeenCalled();
  });
});
