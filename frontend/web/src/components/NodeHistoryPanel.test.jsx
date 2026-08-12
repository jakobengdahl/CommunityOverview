/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import NodeHistoryPanel from './NodeHistoryPanel';
import useGraphStore from '../store/graphStore';

const nowIso = () => new Date().toISOString();

function seed(entries) {
  useGraphStore.setState({
    navHistory: entries,
    nodes: entries.map((e) => ({ id: e.id, name: e.name, type: e.type })),
    focusNodeId: null,
  });
}

describe('NodeHistoryPanel', () => {
  beforeEach(() => {
    useGraphStore.setState({ navHistory: [], nodes: [], focusNodeId: null });
  });
  afterEach(cleanup);

  it('renders nothing when the trail is empty', () => {
    const { container } = render(<NodeHistoryPanel />);
    expect(container.firstChild).toBeNull();
  });

  it('shows a toggle with the entry count once there is history', () => {
    seed([{ id: 'a', name: 'Alpha', type: 'Goal', action: 'added', at: nowIso() }]);
    render(<NodeHistoryPanel />);
    expect(screen.getByText('1')).toBeDefined();
    // Panel is collapsed by default: the row is not shown yet.
    expect(screen.queryByText('Alpha')).toBeNull();
  });

  it('opens the panel and lists entries with their action labels', () => {
    seed([
      { id: 'a', name: 'Alpha', type: 'Goal', action: 'added', at: nowIso() },
      { id: 'b', name: 'Beta', type: 'Actor', action: 'visited', at: nowIso() },
    ]);
    render(<NodeHistoryPanel />);
    fireEvent.click(screen.getByRole('button', { name: 'Recent nodes' }));
    expect(screen.getByText('Alpha')).toBeDefined();
    expect(screen.getByText('Beta')).toBeDefined();
    expect(screen.getByText('Added')).toBeDefined();
    expect(screen.getByText('Visited')).toBeDefined();
  });

  it('navigates to a node when its row is clicked', () => {
    seed([{ id: 'a', name: 'Alpha', type: 'Goal', action: 'added', at: nowIso() }]);
    render(<NodeHistoryPanel />);
    fireEvent.click(screen.getByRole('button', { name: 'Recent nodes' }));
    fireEvent.click(screen.getByText('Alpha'));
    expect(useGraphStore.getState().focusNodeId).toBe('a');
  });

  it('clears the trail via the clear button', () => {
    seed([{ id: 'a', name: 'Alpha', type: 'Goal', action: 'added', at: nowIso() }]);
    render(<NodeHistoryPanel />);
    fireEvent.click(screen.getByRole('button', { name: 'Recent nodes' }));
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    expect(useGraphStore.getState().navHistory).toHaveLength(0);
  });

  it('closes the panel on Escape', () => {
    seed([{ id: 'a', name: 'Alpha', type: 'Goal', action: 'added', at: nowIso() }]);
    render(<NodeHistoryPanel />);
    fireEvent.click(screen.getByRole('button', { name: 'Recent nodes' }));
    expect(screen.getByText('Alpha')).toBeDefined();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByText('Alpha')).toBeNull();
  });
});
