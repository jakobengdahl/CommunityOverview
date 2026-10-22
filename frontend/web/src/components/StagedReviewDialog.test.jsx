import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: (key) => key, language: 'en' }),
}));
vi.mock('../services/api', () => ({
  mergeStagedChanges: vi.fn(),
  discardStagedChanges: vi.fn(),
}));

import * as api from '../services/api';
import StagedReviewDialog from './StagedReviewDialog';

const CREATE = {
  kind: 'node',
  entity_id: 'n1',
  action: 'create',
  status: 'clean',
  fields: { name: { base: null, staged: 'New node', main: null, conflict: false } },
  depends_on: [],
  session_seq: 2,
};
const EDGE = {
  kind: 'edge',
  entity_id: 'e1',
  action: 'create',
  status: 'clean',
  fields: {},
  depends_on: [{ kind: 'node', entity_id: 'n1' }],
  session_seq: 3,
};
const CONFLICT = {
  kind: 'node',
  entity_id: 'n2',
  action: 'update',
  status: 'conflict',
  fields: { description: { base: 'a', staged: 'mine', main: 'theirs', conflict: true } },
  depends_on: [],
  session_seq: 4,
};
const ITEMS = [CREATE, EDGE, CONFLICT];

function renderDialog(props = {}) {
  const onRefresh = props.onRefresh || vi.fn(async () => ITEMS);
  const onMerged = vi.fn();
  const onDiscarded = vi.fn();
  render(
    <StagedReviewDialog
      sessionId="1111-2222"
      items={props.items || ITEMS}
      diffError={null}
      onRefresh={onRefresh}
      onMerged={onMerged}
      onDiscarded={onDiscarded}
      onClose={vi.fn()}
      canvasName={() => null}
    />
  );
  return { onRefresh, onMerged, onDiscarded };
}

function checkbox(key) {
  return screen.getByTestId(`staged-item-${key}`).querySelector('input[type="checkbox"]');
}

beforeEach(() => {
  vi.clearAllMocks();
  let n = 0;
  vi.spyOn(crypto, 'randomUUID').mockImplementation(() => `uuid-${++n}`);
});

describe('StagedReviewDialog', () => {
  it('shows base, session and graph values and the status of each entry', () => {
    renderDialog();
    const row = screen.getByTestId('staged-item-node:n2');
    expect(row.textContent).toContain('mine');
    expect(row.textContent).toContain('theirs');
    expect(row.textContent).toContain('staged.status.conflict');
  });

  it('ticking an edge ticks the node it needs', () => {
    renderDialog();
    fireEvent.click(checkbox('edge:e1'));
    expect(checkbox('node:n1').checked).toBe(true);
  });

  it('merges exactly the ticked entries under a fresh merge id', async () => {
    api.mergeStagedChanges.mockResolvedValue({ merged: [{}, {}], replayed: false });
    const { onMerged } = renderDialog();
    fireEvent.click(checkbox('edge:e1'));
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(onMerged).toHaveBeenCalled());
    expect(api.mergeStagedChanges).toHaveBeenCalledWith('1111-2222', {
      merge_id: 'uuid-1',
      entries: [
        { kind: 'node', entity_id: 'n1' },
        { kind: 'edge', entity_id: 'e1' },
      ],
    });
  });

  it('does not merge an entry that changed since it was shown', async () => {
    const restaged = [CREATE, EDGE, { ...CONFLICT, session_seq: 9 }];
    renderDialog({ onRefresh: vi.fn(async () => restaged) });
    fireEvent.click(checkbox('node:n2'));
    fireEvent.change(screen.getByTestId('resolution-node:n2'), {
      target: { value: 'take_session' },
    });
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(screen.getByText('staged.review_drift')).toBeTruthy());
    expect(api.mergeStagedChanges).not.toHaveBeenCalled();
  });

  it('asks for a decision on a conflict before merging', async () => {
    renderDialog();
    fireEvent.click(checkbox('node:n2'));
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(screen.getByText('staged.needs_decision')).toBeTruthy());
    expect(api.mergeStagedChanges).not.toHaveBeenCalled();
  });

  it('sends the values typed for a manual resolution', async () => {
    api.mergeStagedChanges.mockResolvedValue({ merged: [{}] });
    renderDialog();
    fireEvent.click(checkbox('node:n2'));
    fireEvent.change(screen.getByTestId('resolution-node:n2'), { target: { value: 'manual' } });
    fireEvent.change(screen.getByTestId('manual-node:n2-description'), {
      target: { value: 'both of us' },
    });
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(api.mergeStagedChanges).toHaveBeenCalled());
    expect(api.mergeStagedChanges.mock.calls[0][1].resolutions).toEqual([
      { kind: 'node', entity_id: 'n2', choice: 'manual', values: { description: 'both of us' } },
    ]);
  });

  it('retries a merge whose outcome is unknown under the same merge id', async () => {
    api.mergeStagedChanges
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce({ merged: [{}], replayed: true });
    const { onMerged } = renderDialog();
    fireEvent.click(checkbox('node:n1'));
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(screen.getByText('staged.merge_unknown')).toBeTruthy());
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(onMerged).toHaveBeenCalled());
    const ids = api.mergeStagedChanges.mock.calls.map((call) => call[1].merge_id);
    expect(ids).toEqual(['uuid-1', 'uuid-1']);
  });

  it('a changed selection after an unknown outcome goes out under a new merge id', async () => {
    api.mergeStagedChanges
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce({ merged: [{}] });
    renderDialog();
    fireEvent.click(checkbox('node:n1'));
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(screen.getByText('staged.merge_unknown')).toBeTruthy());
    fireEvent.click(checkbox('edge:e1'));
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(api.mergeStagedChanges).toHaveBeenCalledTimes(2));
    const ids = api.mergeStagedChanges.mock.calls.map((call) => call[1].merge_id);
    expect(ids[0]).not.toBe(ids[1]);
  });

  it('on merge_raced reads again and retries once with a fresh id when nothing shown changed', async () => {
    api.mergeStagedChanges
      .mockRejectedValueOnce(
        Object.assign(new Error('raced'), { status: 409, code: 'merge_raced', data: {} })
      )
      .mockResolvedValueOnce({ merged: [{}] });
    const { onMerged, onRefresh } = renderDialog();
    fireEvent.click(checkbox('node:n1'));
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(onMerged).toHaveBeenCalled());
    expect(onRefresh).toHaveBeenCalledTimes(2);
    const ids = api.mergeStagedChanges.mock.calls.map((call) => call[1].merge_id);
    expect(ids).toEqual(['uuid-1', 'uuid-2']);
  });

  it('marks the entries a merge_blocked answer names', async () => {
    api.mergeStagedChanges.mockRejectedValueOnce(
      Object.assign(new Error('blocked'), {
        status: 409,
        code: 'merge_blocked',
        data: { items: [{ kind: 'node', entity_id: 'n1' }] },
      })
    );
    renderDialog();
    fireEvent.click(checkbox('node:n1'));
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() =>
      expect(screen.getByTestId('staged-item-node:n1').className).toContain('is-blocked')
    );
  });

  it('adds the entries a merge_incomplete answer names to the selection, without merging', async () => {
    api.mergeStagedChanges.mockRejectedValueOnce(
      Object.assign(new Error('incomplete'), {
        status: 422,
        code: 'merge_incomplete',
        data: { missing: [{ kind: 'edge', entity_id: 'e1' }] },
      })
    );
    renderDialog();
    fireEvent.click(checkbox('node:n1'));
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(checkbox('edge:e1').checked).toBe(true));
    expect(api.mergeStagedChanges).toHaveBeenCalledTimes(1);
  });

  it('discards only the ticked entries, after a confirm', async () => {
    api.discardStagedChanges.mockResolvedValue({ removed_entries: [['node', 'n2']] });
    const { onDiscarded } = renderDialog();
    fireEvent.click(checkbox('node:n2'));
    fireEvent.click(screen.getByText('staged.discard_selected'));
    expect(api.discardStagedChanges).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('staged.discard_confirm'));
    await waitFor(() => expect(onDiscarded).toHaveBeenCalled());
    expect(api.discardStagedChanges).toHaveBeenCalledWith('1111-2222', [
      { kind: 'node', entity_id: 'n2' },
    ]);
  });
});
