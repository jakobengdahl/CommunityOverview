import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

import ActivityDrawer from '../src/components/ActivityDrawer';
import { I18nProvider } from '../src/i18n';
import useGraphStore from '../src/store/graphStore';
import * as api from '../src/services/api';

vi.mock('../src/services/api', () => ({
  getGraphHistory: vi.fn(async () => ({ entries: [] })),
  getSessionActivity: vi.fn(async () => ({ activity: [] })),
  undoSessionAction: vi.fn(async () => ({
    undone_activity_id: 'act-1',
    undone_op: 'annotation_created',
  })),
}));

const ROSTER = [
  { client_id: 'client-me', display_name: 'Ada', color: '#f00' },
  { client_id: 'client-other', display_name: 'Grace', color: '#0f0' },
];

function record(overrides = {}) {
  return {
    id: 'act-1',
    op: 'annotation_created',
    actor: 'client-me',
    occurred_at: new Date().toISOString(),
    affected: { kind: 'annotation', id: 'note-1', fields: null },
    before: null,
    after: { id: 'note-1', type: 'note' },
    inverse_op: { op: 'annotation_deleted', annotation_id: 'note-1' },
    undone: false,
    ...overrides,
  };
}

function renderDrawer(overrides = {}) {
  const props = {
    open: true,
    onClose: vi.fn(),
    sessionId: '1111-2222-3333-4444',
    currentClientId: 'client-me',
    roster: ROSTER,
    ...overrides,
  };
  render(
    <I18nProvider>
      <ActivityDrawer {...props} />
    </I18nProvider>
  );
  return props;
}

describe('ActivityDrawer', () => {
  beforeEach(() => {
    useGraphStore.setState({ nodes: [] });
    api.getSessionActivity.mockReset().mockResolvedValue({ activity: [] });
    api.getGraphHistory.mockReset().mockResolvedValue({ entries: [] });
    api.undoSessionAction.mockReset().mockResolvedValue({
      undone_activity_id: 'act-1',
      undone_op: 'annotation_created',
    });
  });
  afterEach(cleanup);

  it('opens on the Session tab and fetches session activity for the given session', async () => {
    renderDrawer();
    await waitFor(() =>
      expect(api.getSessionActivity).toHaveBeenCalledWith(
        '1111-2222-3333-4444',
        expect.objectContaining({ limit: expect.any(Number) })
      )
    );
    expect(screen.getByRole('tab', { name: 'Session' })).toHaveAttribute('aria-selected', 'true');
  });

  it('shows the empty state when there is no session activity', async () => {
    renderDrawer();
    expect(await screen.findByText('No session activity yet')).toBeInTheDocument();
  });

  it('renders a readable description and actor attribution for a record', async () => {
    api.getSessionActivity.mockResolvedValue({ activity: [record()] });
    renderDrawer();
    expect(await screen.findByText('Created a sticky note')).toBeInTheDocument();
    expect(screen.getByText('Ada (You)')).toBeInTheDocument();
  });

  it('still attributes your own action as "You" when the roster has not echoed your entry back yet', async () => {
    api.getSessionActivity.mockResolvedValue({ activity: [record({ actor: 'client-me' })] });
    renderDrawer({ roster: [] });
    await screen.findByText('Created a sticky note');
    expect(screen.getByText('You')).toBeInTheDocument();
  });

  it('falls back to the raw client id for a collaborator who is no longer in the roster', async () => {
    api.getSessionActivity.mockResolvedValue({
      activity: [record({ actor: 'client-gone' })],
    });
    renderDrawer({ roster: [] });
    await screen.findByText('Created a sticky note');
    expect(screen.getByText('client-gone')).toBeInTheDocument();
  });

  it("shows Undo only on the current actor's own latest undoable record, not on others'", async () => {
    api.getSessionActivity.mockResolvedValue({
      activity: [
        record({ id: 'mine', actor: 'client-me' }),
        record({ id: 'theirs', actor: 'client-other' }),
      ],
    });
    renderDrawer();
    await screen.findAllByText('Created a sticky note');
    // One inline row Undo button (for "mine") + the toolbar "Undo my last action" button.
    const undoButtons = screen.getAllByRole('button', { name: /undo/i });
    expect(undoButtons.length).toBe(2);
  });

  it('does not show an inline Undo button when the actor has nothing undoable', async () => {
    api.getSessionActivity.mockResolvedValue({
      activity: [record({ actor: 'client-other' })],
    });
    renderDrawer();
    await screen.findByText('Created a sticky note');
    // Only the disabled toolbar button remains; no inline row Undo.
    const undoButtons = screen.getAllByRole('button', { name: /undo/i });
    expect(undoButtons.length).toBe(1);
    expect(undoButtons[0]).toBeDisabled();
  });

  it('invokes undo for the current actor and shows success, then refreshes the list', async () => {
    api.getSessionActivity
      .mockResolvedValueOnce({ activity: [record()] })
      .mockResolvedValueOnce({ activity: [record({ undone: true })] });
    renderDrawer();
    await screen.findByText('Created a sticky note');

    fireEvent.click(screen.getByRole('button', { name: 'Undo my last action' }));

    await waitFor(() =>
      expect(api.undoSessionAction).toHaveBeenCalledWith('1111-2222-3333-4444', 'client-me')
    );
    expect(await screen.findByText('Action undone')).toBeInTheDocument();
    expect(await screen.findByText('Undone')).toBeInTheDocument();
    expect(api.getSessionActivity).toHaveBeenCalledTimes(2);
  });

  it('shows conflict feedback distinctly from a generic failure', async () => {
    api.getSessionActivity.mockResolvedValue({ activity: [record()] });
    const conflictError = new Error('affected state changed since this action');
    conflictError.status = 409;
    api.undoSessionAction.mockRejectedValue(conflictError);
    renderDrawer();
    await screen.findByText('Created a sticky note');

    fireEvent.click(screen.getByRole('button', { name: 'Undo my last action' }));

    expect(
      await screen.findByText('That action can no longer be undone — it was changed since')
    ).toBeInTheDocument();
  });

  it('shows a distinct message for a rate-limited undo', async () => {
    api.getSessionActivity.mockResolvedValue({ activity: [record()] });
    const rateLimitError = new Error('rate limit exceeded');
    rateLimitError.status = 429;
    api.undoSessionAction.mockRejectedValue(rateLimitError);
    renderDrawer();
    await screen.findByText('Created a sticky note');

    fireEvent.click(screen.getByRole('button', { name: 'Undo my last action' }));

    expect(
      await screen.findByText('Too many actions — wait a moment and try again')
    ).toBeInTheDocument();
  });

  it('switches to the Graph tab and loads graph history there, independent of the Session tab', async () => {
    api.getGraphHistory.mockResolvedValue({
      entries: [
        {
          event_id: 'e1',
          event_type: 'node.create',
          entity_kind: 'node',
          entity_id: 'n1',
          occurred_at: new Date().toISOString(),
          after: { name: 'Alpha' },
        },
      ],
    });
    renderDrawer();
    fireEvent.click(screen.getByRole('tab', { name: 'Graph' }));
    await waitFor(() => expect(api.getGraphHistory).toHaveBeenCalled());
    expect(await screen.findByText('Alpha')).toBeInTheDocument();
    // Session tab's undo toolbar is not part of the Graph tab's DOM.
    expect(screen.queryByRole('button', { name: 'Undo my last action' })).toBeNull();
  });

  it('resolves a node_moved description through the node store when available', async () => {
    useGraphStore.setState({ nodes: [{ id: 'node-1', name: 'Acme Corp', type: 'Customer' }] });
    api.getSessionActivity.mockResolvedValue({
      activity: [
        record({
          op: 'node_moved',
          affected: { kind: 'node_position', id: 'node-1' },
          inverse_op: { op: 'node_moved', node_id: 'node-1', position: { x: 0, y: 0 } },
        }),
      ],
    });
    renderDrawer();
    expect(await screen.findByText('Moved "Acme Corp"')).toBeInTheDocument();
  });

  it('closes on Escape', async () => {
    const props = renderDrawer();
    await screen.findByText('No session activity yet');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(props.onClose).toHaveBeenCalled();
  });
});
