import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('reactflow', () => ({
  Handle: ({ type }) => <div data-testid={`handle-${type}`} />,
  Position: { Top: 'top', Bottom: 'bottom' },
}));

import CustomNode from '../src/components/CustomNode';

describe('CustomNode tooltip', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders tooltip below the node in a top-level layer on hover', () => {
    const rect = {
      top: 100,
      left: 50,
      width: 120,
      height: 40,
      right: 170,
      bottom: 140,
    };

    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue(rect);

    render(
      <CustomNode
        id="node-1"
        selected={false}
        data={{
          label: 'Node 1',
          nodeType: 'Actor',
          color: '#3B82F6',
          description: 'Tooltip body',
          communities: ['A'],
        }}
      />
    );

    fireEvent.mouseEnter(screen.getByText('Node 1').closest('.graph-custom-node'));

    const tooltip = screen.getByText('Tooltip body').closest('.graph-node-tooltip');
    expect(tooltip).toBeInTheDocument();
    expect(tooltip.parentElement).toBe(document.body);
    expect(tooltip.style.top).toBe('148px');
    expect(tooltip.style.left).toBe('110px');
    expect(tooltip.style.zIndex).toBe('99999');
  });

  it('renders a remote-selection outline and name badge in the collaborator colour', () => {
    render(
      <CustomNode
        id="node-1"
        selected={false}
        data={{
          label: 'Node 1',
          nodeType: 'Actor',
          color: '#3B82F6',
          remoteSelection: { color: '#e6194b', displayName: 'Ada' },
        }}
      />
    );
    const node = screen.getByText('Node 1').closest('.graph-custom-node');
    expect(node.className).toContain('remote-selected');
    expect(node.style.outline).toBe('2px solid #e6194b');
    const badge = screen.getByText('Ada');
    expect(badge.className).toContain('graph-node-remote-badge');
    expect(badge.style.backgroundColor).toBe('rgb(230, 25, 75)');
  });

  it('has no remote marker when remoteSelection is absent', () => {
    render(
      <CustomNode id="node-1" selected={false} data={{ label: 'Node 1', nodeType: 'Actor', color: '#3B82F6' }} />
    );
    const node = screen.getByText('Node 1').closest('.graph-custom-node');
    expect(node.className).not.toContain('remote-selected');
    expect(node.style.outline).toBe('');
  });
});
