/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import NodeDetailDialog from './NodeDetailDialog';

vi.mock('../store/graphStore', () => ({
  default: () => ({ getNodeColor: () => '#123456', schema: { node_types: {} } }),
}));

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: (key) => key }),
}));

// Stub the history view so the tab-selection invariant is tested without any
// API calls; its identity is enough to assert which tab is active.
vi.mock('./EntityHistoryView', () => ({
  default: ({ entityKind, entityId }) => (
    <div data-testid="entity-history" data-kind={entityKind} data-id={entityId} />
  ),
}));

const node = { id: 'n1', data: { type: 'Actor', name: 'Alice' } };

describe('NodeDetailDialog', () => {
  it('opens on the details tab by default (no history view rendered)', () => {
    render(<NodeDetailDialog node={node} onClose={vi.fn()} />);
    expect(screen.queryByTestId('entity-history')).toBeNull();
    expect(
      screen.getByRole('tab', { name: 'detail.tab_details' }).getAttribute('aria-selected')
    ).toBe('true');
  });

  it('opens directly on the history tab when initialView is "history"', () => {
    render(<NodeDetailDialog node={node} onClose={vi.fn()} initialView="history" />);
    const history = screen.getByTestId('entity-history');
    expect(history.getAttribute('data-kind')).toBe('node');
    expect(history.getAttribute('data-id')).toBe('n1');
    expect(
      screen.getByRole('tab', { name: 'history.view_history' }).getAttribute('aria-selected')
    ).toBe('true');
  });
});
