/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import NodeDetailDialog from './NodeDetailDialog';

// The real panel, unlike NodeDetailDialog.test.jsx: this pins that switching
// node starts the panel afresh rather than showing the previous node's files.
vi.mock('../store/graphStore', () => ({
  default: () => ({
    getNodeColor: () => '#123456',
    schema: { node_types: { Document: { allows_attachments: true } } },
  }),
}));

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: (key) => key, language: 'en' }),
}));

vi.mock('./EntityHistoryView', () => ({ default: () => null }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('NodeDetailDialog attachments on node switch', () => {
  it("drops the previous node's files while the next node's list loads", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          attachments: [{ id: 'a1', filename: 'first.pdf', size: 1, uploaded_at: '' }],
          limits: { max_file_bytes: 10, max_files_per_node: 3, max_bytes_per_node: 30 },
          uploads_allowed: true,
        }),
      })
      .mockReturnValueOnce(new Promise(() => {}));
    vi.stubGlobal('fetch', fetchMock);

    const docA = { id: 'A', data: { type: 'Document', name: 'A' } };
    const docB = { id: 'B', data: { type: 'Document', name: 'B' } };
    const { rerender } = render(<NodeDetailDialog node={docA} onClose={vi.fn()} />);
    expect(await screen.findByText('first.pdf')).toBeTruthy();

    rerender(<NodeDetailDialog node={docB} onClose={vi.fn()} />);
    expect(screen.queryByText('first.pdf')).toBeNull();
    expect(screen.getByText('attachments.loading')).toBeTruthy();
    expect(fetchMock.mock.calls[1][0]).toBe('/api/nodes/B/attachments');
  });
});
