/**
 * @vitest-environment jsdom
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import NodeHistoryPanel from './NodeHistoryPanel';
import useGraphStore from '../store/graphStore';

const nowIso = () => new Date().toISOString();

function readStylesheet() {
  return readFileSync(join(process.cwd(), 'src/components/NodeHistoryPanel.css'), 'utf8');
}

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

  it('keeps the history toolbar above the bottom-center annotation toolbox lane', () => {
    const css = readStylesheet();
    const baseRule = css.match(/\.node-history \{([^}]*)\}/);
    expect(baseRule).toBeTruthy();
    expect(baseRule[1]).toMatch(/bottom:\s*calc\(86px \+ env\(safe-area-inset-bottom\)\)/);
    expect(baseRule[1]).toMatch(/left:\s*50%/);
    expect(baseRule[1]).toMatch(/transform:\s*translateX\(-50%\)/);
    expect(baseRule[1]).toMatch(/z-index:\s*6/);
    expect(baseRule[1]).not.toMatch(/bottom:\s*16px/);
    expect(baseRule[1]).not.toMatch(/top:\s*80px/);
    expect(baseRule[1]).not.toMatch(/right:\s*16px/);
    expect(css).not.toMatch(/\.app\.session-drawer-open:not\(\.is-mobile\) \.node-history/);

    const mobileBlock = css.match(/@media \(max-width: 768px\) \{((?:[^{}]|\{[^{}]*\})*)\}/);
    expect(mobileBlock).toBeTruthy();
    expect(mobileBlock[1]).toMatch(
      /\.node-history \{[^}]*top:\s*calc\(56px \+ env\(safe-area-inset-top\)\)/
    );
    expect(mobileBlock[1]).toMatch(/\.node-history \{[^}]*bottom:\s*auto/);
    expect(mobileBlock[1]).toMatch(/\.node-history \{[^}]*left:\s*auto/);
    expect(mobileBlock[1]).toMatch(/\.node-history \{[^}]*transform:\s*none/);
    expect(mobileBlock[1]).toMatch(
      /\.node-history \{[^}]*right:\s*calc\(12px \+ env\(safe-area-inset-right\)\)/
    );
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
