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
  const onRefresh = props.onRefresh || vi.fn(async () => props.items || ITEMS);
  const onMerged = vi.fn();
  const onDiscarded = vi.fn();
  const element = (items) => (
    <StagedReviewDialog
      sessionId="1111-2222"
      items={items}
      diffError={null}
      onRefresh={onRefresh}
      onMerged={onMerged}
      onDiscarded={onDiscarded}
      onClose={vi.fn()}
      canvasName={() => null}
    />
  );
  const { rerender } = render(element(props.items || ITEMS));
  return { onRefresh, onMerged, onDiscarded, rerender: (items) => rerender(element(items)) };
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

  it('does not merge when the fresh read fails', async () => {
    renderDialog({ onRefresh: vi.fn(async () => null) });
    fireEvent.click(checkbox('node:n1'));
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(screen.getByText('staged.refresh_failed')).toBeTruthy());
    expect(api.mergeStagedChanges).not.toHaveBeenCalled();
  });

  it('does not discard an entry that changed since it was ticked', async () => {
    renderDialog({ onRefresh: vi.fn(async () => [{ ...CREATE, session_seq: 9 }, EDGE, CONFLICT]) });
    fireEvent.click(checkbox('node:n1'));
    fireEvent.click(screen.getByText('staged.discard_selected'));
    fireEvent.click(screen.getByText('staged.discard_confirm'));
    await waitFor(() => expect(screen.getByText('staged.review_drift')).toBeTruthy());
    expect(api.discardStagedChanges).not.toHaveBeenCalled();
  });

  it('retries a raced merge at most once', async () => {
    const raced = () =>
      Object.assign(new Error('raced'), { status: 409, code: 'merge_raced', data: {} });
    api.mergeStagedChanges.mockRejectedValue(raced());
    renderDialog();
    fireEvent.click(checkbox('node:n1'));
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(screen.getByText('staged.merge_raced')).toBeTruthy());
    expect(api.mergeStagedChanges).toHaveBeenCalledTimes(2);
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

  it('never merges a manual value for a field that came to conflict after it was chosen', async () => {
    const twoConflicts = {
      ...CONFLICT,
      fields: {
        ...CONFLICT.fields,
        name: { base: 'n', staged: 'mine', main: 'theirs', conflict: true },
      },
      session_seq: 5,
    };
    const { rerender } = renderDialog({
      onRefresh: vi.fn(async () => [CREATE, EDGE, twoConflicts]),
    });
    fireEvent.click(checkbox('node:n2'));
    fireEvent.change(screen.getByTestId('resolution-node:n2'), { target: { value: 'manual' } });
    rerender([CREATE, EDGE, twoConflicts]);
    // The tick dropped when the entry changed; the user ticks it again.
    expect(checkbox('node:n2').checked).toBe(false);
    fireEvent.click(checkbox('node:n2'));
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(screen.getByText('staged.needs_decision')).toBeTruthy());
    expect(api.mergeStagedChanges).not.toHaveBeenCalled();
  });

  it('names a known refusal in the UI language', async () => {
    api.mergeStagedChanges.mockRejectedValue(
      Object.assign(new Error('invalid merge request (English)'), {
        status: 422,
        code: 'invalid_merge_request',
        data: {},
      })
    );
    renderDialog();
    fireEvent.click(checkbox('node:n1'));
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() =>
      expect(screen.getByText('staged.refusal.invalid_merge_request')).toBeTruthy()
    );
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

  it('names the entries a merge_incomplete answer lists, without ticking them', async () => {
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
    await waitFor(() =>
      expect(screen.getByTestId('staged-item-edge:e1').className).toContain('is-blocked')
    );
    expect(checkbox('edge:e1').checked).toBe(false);
    expect(api.mergeStagedChanges).toHaveBeenCalledTimes(1);
  });

  it('unticks an entry restaged after it was ticked, so it never merges unseen', async () => {
    const { rerender } = renderDialog();
    fireEvent.click(checkbox('node:n1'));
    expect(checkbox('node:n1').checked).toBe(true);
    const restaged = [
      { ...CREATE, session_seq: 7, fields: { name: { ...CREATE.fields.name, staged: 'Other' } } },
      EDGE,
      CONFLICT,
    ];
    rerender(restaged);
    expect(checkbox('node:n1').checked).toBe(false);
    expect(screen.getByText('staged.ticks_dropped')).toBeTruthy();
    expect(screen.getByTestId('staged-merge').disabled).toBe(true);
  });

  it('does not re-tick a restaged dependency on the way to ticking its dependant', () => {
    const { rerender } = renderDialog();
    fireEvent.click(checkbox('node:n1'));
    rerender([{ ...CREATE, session_seq: 9 }, EDGE, CONFLICT]);
    expect(checkbox('node:n1').checked).toBe(false);
    fireEvent.click(checkbox('edge:e1'));
    expect(checkbox('edge:e1').checked).toBe(false);
    expect(screen.getByText('staged.recheck_first')).toBeTruthy();
  });

  it('keeps a stale tick through an unrelated tick, then refuses to re-tick it on the way', () => {
    const { rerender } = renderDialog();
    fireEvent.click(checkbox('node:n1'));
    rerender([{ ...CREATE, session_seq: 9 }, EDGE, CONFLICT]);
    fireEvent.click(checkbox('node:n2'));
    fireEvent.click(checkbox('edge:e1'));
    expect(checkbox('edge:e1').checked).toBe(false);
    expect(screen.getByText('staged.recheck_first')).toBeTruthy();
  });

  it('will not merge a selection a stale dependency left open', () => {
    const { rerender } = renderDialog();
    fireEvent.click(checkbox('edge:e1'));
    rerender([{ ...CREATE, session_seq: 9 }, EDGE, CONFLICT]);
    expect(checkbox('edge:e1').checked).toBe(true);
    expect(screen.getByTestId('staged-merge').disabled).toBe(true);
  });

  it('drops a merge_blocked status once another entry is resolved differently', async () => {
    api.mergeStagedChanges.mockRejectedValueOnce(
      Object.assign(new Error('blocked'), {
        status: 409,
        code: 'merge_blocked',
        data: { items: [{ ...EDGE, status: 'dangling' }] },
      })
    );
    renderDialog({ items: [{ ...CREATE, status: 'id_taken' }, EDGE] });
    fireEvent.click(checkbox('edge:e1'));
    fireEvent.change(screen.getByTestId('resolution-node:n1'), { target: { value: 'keep_main' } });
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(screen.getByTestId('resolution-edge:e1')).toBeTruthy());
    fireEvent.change(screen.getByTestId('resolution-node:n1'), {
      target: { value: 'take_session' },
    });
    expect(screen.queryByTestId('resolution-edge:e1')).toBeNull();
    expect(screen.getByTestId('staged-item-edge:e1').textContent).toContain('staged.status.clean');
  });

  it('a merge_blocked status that exists only within the merge can be resolved', async () => {
    api.mergeStagedChanges
      .mockRejectedValueOnce(
        Object.assign(new Error('blocked'), {
          status: 409,
          code: 'merge_blocked',
          data: { items: [{ ...EDGE, status: 'dangling' }] },
        })
      )
      .mockResolvedValueOnce({ merged: [{}] });
    const idTaken = { ...CREATE, status: 'id_taken' };
    renderDialog({ items: [idTaken, EDGE] });
    fireEvent.click(checkbox('edge:e1'));
    fireEvent.change(screen.getByTestId('resolution-node:n1'), { target: { value: 'keep_main' } });
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(screen.getByTestId('resolution-edge:e1')).toBeTruthy());
    fireEvent.change(screen.getByTestId('resolution-edge:e1'), {
      target: { value: 'take_session' },
    });
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(api.mergeStagedChanges).toHaveBeenCalledTimes(2));
    expect(api.mergeStagedChanges.mock.calls[1][1].resolutions).toEqual([
      { kind: 'node', entity_id: 'n1', choice: 'keep_main' },
      { kind: 'edge', entity_id: 'e1', choice: 'take_session' },
    ]);
  });

  it('does not replay an unknown-outcome merge id once its entries have changed', async () => {
    api.mergeStagedChanges.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const onRefresh = vi
      .fn()
      .mockResolvedValueOnce(ITEMS)
      .mockResolvedValueOnce([{ ...CREATE, session_seq: 8 }, EDGE, CONFLICT])
      .mockResolvedValue([{ ...CREATE, session_seq: 8 }, EDGE, CONFLICT]);
    const { rerender } = renderDialog({ onRefresh });
    fireEvent.click(checkbox('node:n1'));
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(screen.getByText('staged.merge_unknown')).toBeTruthy());
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(screen.getByText('staged.review_drift')).toBeTruthy());
    api.mergeStagedChanges.mockResolvedValueOnce({ merged: [{}] });
    const fresh = [{ ...CREATE, session_seq: 8 }, EDGE, CONFLICT];
    rerender(fresh);
    fireEvent.click(checkbox('node:n1'));
    fireEvent.click(screen.getByTestId('staged-merge'));
    await waitFor(() => expect(api.mergeStagedChanges).toHaveBeenCalledTimes(2));
    const ids = api.mergeStagedChanges.mock.calls.map((call) => call[1].merge_id);
    expect(ids[0]).not.toBe(ids[1]);
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
