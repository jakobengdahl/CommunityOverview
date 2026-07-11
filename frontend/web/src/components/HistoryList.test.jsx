/**
 * @vitest-environment jsdom
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import HistoryList from './HistoryList';

describe('HistoryList', () => {
  it('renders an empty state when there are no entries', () => {
    render(<HistoryList entries={[]} />);
    expect(screen.getByText('No activity yet')).toBeDefined();
  });

  it('shows the AI badge for AI-driven actions', () => {
    render(
      <HistoryList
        entries={[
          {
            event_id: 'e1',
            event_type: 'node.create',
            occurred_at: '2026-07-11T10:00:00Z',
            entity_kind: 'node',
            entity_id: 'n1',
            entity_type: 'Actor',
            after: { name: 'Eurostat' },
            event_origin: 'mcp',
            is_ai_action: true,
          },
        ]}
      />
    );
    expect(screen.getByText('AI')).toBeDefined();
    expect(screen.getByText('Node created')).toBeDefined();
    expect(screen.getByText('Eurostat')).toBeDefined();
  });

  it('renders a before→after diff for updates', () => {
    render(
      <HistoryList
        entries={[
          {
            event_id: 'e2',
            event_type: 'node.update',
            occurred_at: '2026-07-11T10:05:00Z',
            entity_kind: 'node',
            entity_id: 'n1',
            entity_type: 'Actor',
            before: { name: 'Eurostat', summary: 'Old summary' },
            after: { name: 'Eurostat', summary: 'New summary' },
            patch: { summary: 'New summary' },
            event_origin: 'web-ui',
            is_ai_action: false,
          },
        ]}
      />
    );
    expect(screen.getByText('Old summary')).toBeDefined();
    expect(screen.getByText('New summary')).toBeDefined();
    // Not an AI action → no badge
    expect(screen.queryByText('AI')).toBeNull();
  });
});
