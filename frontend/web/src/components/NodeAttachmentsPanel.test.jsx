/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import NodeAttachmentsPanel, { precheckUpload } from './NodeAttachmentsPanel';

// t echoes the key plus its params so assertions can see which message and
// which values were chosen without depending on the English wording.
vi.mock('../i18n', () => ({
  useI18n: () => ({
    t: (key, params) => (params ? `${key} ${JSON.stringify(params)}` : key),
    language: 'en',
  }),
}));

const LIMITS = { max_file_bytes: 100, max_files_per_node: 3, max_bytes_per_node: 250 };

function record(id, size = 10, filename = `${id}.pdf`) {
  return {
    id,
    node_id: 'n1',
    filename,
    size,
    content_type: 'application/pdf',
    sha256: 'x',
    uploaded_at: '2026-10-18T08:00:00Z',
    uploaded_by: null,
  };
}

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function listing(attachments = [], uploadsAllowed = true) {
  return jsonResponse(200, { attachments, limits: LIMITS, uploads_allowed: uploadsAllowed });
}

function chooseFile(file) {
  fireEvent.change(screen.getByTestId('node-attachments-input'), { target: { files: [file] } });
}

function fileOfSize(size, name = 'doc.pdf') {
  return new File([new Uint8Array(size)], name, { type: 'application/pdf' });
}

let fetchMock;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('NodeAttachmentsPanel gating', () => {
  it('renders nothing for a type without the flag and no attachments', async () => {
    fetchMock.mockResolvedValueOnce(listing([], false));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments={false} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId('node-attachments')).toBeNull();
  });

  it('still lists existing files when the type no longer allows uploads', async () => {
    fetchMock.mockResolvedValueOnce(listing([record('a1')], false));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments={false} />);
    expect(await screen.findByText('a1.pdf')).toBeTruthy();
    expect(screen.queryByTestId('node-attachments-input')).toBeNull();
  });

  it('never renders or calls the API for a federated node', () => {
    render(<NodeAttachmentsPanel nodeId="federated::g::x" typeAllowsAttachments />);
    expect(screen.queryByTestId('node-attachments')).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('shows the upload control only when the list says uploads_allowed', async () => {
    fetchMock.mockResolvedValueOnce(listing([], false));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    expect(await screen.findByText('attachments.empty')).toBeTruthy();
    expect(screen.queryByTestId('node-attachments-input')).toBeNull();
  });

  it('shows a load error, not the empty state, when listing fails', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(503, { detail: { error: 'x' } }));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    expect(await screen.findByText('attachments.load_error')).toBeTruthy();
    expect(screen.queryByText('attachments.empty')).toBeNull();
  });
});

describe('NodeAttachmentsPanel list', () => {
  it('lists each file with a download link to the GET route', async () => {
    fetchMock.mockResolvedValueOnce(listing([record('a1'), record('b/2', 2048, 'q&a.txt')]));
    render(<NodeAttachmentsPanel nodeId="node 1" typeAllowsAttachments />);
    const link = await screen.findByText('q&a.txt');
    expect(link.tagName).toBe('A');
    expect(link.getAttribute('href')).toBe('/api/nodes/node%201/attachments/b%2F2');
    expect(fetchMock.mock.calls[0][0]).toBe('/api/nodes/node%201/attachments');
    expect(screen.getByText('a1.pdf').getAttribute('href')).toBe(
      '/api/nodes/node%201/attachments/a1'
    );
  });
});

describe('NodeAttachmentsPanel upload', () => {
  it('posts the raw file with its name and adds the stored record to the list', async () => {
    fetchMock
      .mockResolvedValueOnce(listing([]))
      .mockResolvedValueOnce(jsonResponse(201, record('new', 5, 'doc.pdf')));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('attachments.empty');
    const file = fileOfSize(5, 'doc.pdf');
    chooseFile(file);
    expect(await screen.findByText('doc.pdf')).toBeTruthy();
    const [url, options] = fetchMock.mock.calls[1];
    expect(url).toBe('/api/nodes/n1/attachments?filename=doc.pdf');
    expect(options.method).toBe('POST');
    expect(options.body).toBe(file);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it("maps the server's error code to its message key", async () => {
    fetchMock.mockResolvedValueOnce(listing([])).mockResolvedValueOnce(
      jsonResponse(415, {
        detail: { error: 'unsupported_attachment_type', message: 'This file type...' },
      })
    );
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('attachments.empty');
    chooseFile(fileOfSize(5));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/^attachments\.errors\.unsupported_attachment_type/);
    expect(screen.getByText('attachments.empty')).toBeTruthy();
  });

  it("shows the server's message for an error code without its own key", async () => {
    fetchMock
      .mockResolvedValueOnce(listing([]))
      .mockResolvedValueOnce(
        jsonResponse(500, { detail: { error: 'attachment_index_corrupt', message: 'Index bad' } })
      );
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('attachments.empty');
    chooseFile(fileOfSize(5));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe('attachments.errors.generic_with_detail {"detail":"Index bad"}');
  });

  it('refuses an oversized file before sending, with the per-file message', async () => {
    fetchMock.mockResolvedValueOnce(listing([]));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('attachments.empty');
    chooseFile(fileOfSize(101));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/^attachments\.errors\.attachment_too_large /);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('refuses a file that fits the per-file limit but not the node budget', async () => {
    fetchMock.mockResolvedValueOnce(listing([record('a', 100), record('b', 100)]));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('a.pdf');
    chooseFile(fileOfSize(51));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/^attachments\.errors\.node_attachment_limit /);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('precheckUpload', () => {
  it('refuses by count before size once the node is full', () => {
    const full = [record('a', 1), record('b', 1), record('c', 1)];
    expect(precheckUpload({ size: 1 }, full, LIMITS)).toBe('node_attachment_limit');
  });

  it('reports the per-file limit when it ties with the remaining budget', () => {
    const used = [record('a', 150)];
    expect(precheckUpload({ size: 101 }, used, LIMITS)).toBe('attachment_too_large');
    expect(precheckUpload({ size: 100 }, used, LIMITS)).toBeNull();
  });

  it('lets a file of exactly the cap through', () => {
    expect(precheckUpload({ size: 100 }, [], LIMITS)).toBeNull();
  });
});

describe('NodeAttachmentsPanel delete', () => {
  it('asks for confirmation, then deletes and removes the row', async () => {
    fetchMock
      .mockResolvedValueOnce(listing([record('a1'), record('a2')]))
      .mockResolvedValueOnce(jsonResponse(200, { success: true }));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('a1.pdf');
    fireEvent.click(screen.getByLabelText('attachments.delete_named {"name":"a1.pdf"}'));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const prompt = screen.getByText('attachments.confirm_delete {"name":"a1.pdf"}');
    fireEvent.click(within(prompt.parentElement).getByText('attachments.delete'));
    await waitFor(() => expect(screen.queryByText('a1.pdf')).toBeNull());
    expect(screen.getByText('a2.pdf')).toBeTruthy();
    const [url, options] = fetchMock.mock.calls[1];
    expect(url).toBe('/api/nodes/n1/attachments/a1');
    expect(options.method).toBe('DELETE');
  });

  it('cancelling the confirmation sends nothing', async () => {
    fetchMock.mockResolvedValueOnce(listing([record('a1')]));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('a1.pdf');
    fireEvent.click(screen.getByLabelText('attachments.delete_named {"name":"a1.pdf"}'));
    fireEvent.click(screen.getByText('attachments.cancel'));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.getByText('a1.pdf')).toBeTruthy();
  });

  it('keeps the row and shows the error when the delete is refused', async () => {
    fetchMock
      .mockResolvedValueOnce(listing([record('a1')]))
      .mockResolvedValueOnce(jsonResponse(403, { detail: 'Forbidden' }));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('a1.pdf');
    fireEvent.click(screen.getByLabelText('attachments.delete_named {"name":"a1.pdf"}'));
    fireEvent.click(screen.getByText('attachments.delete'));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe('attachments.errors.generic_with_detail {"detail":"Forbidden"}');
    expect(screen.getByText('a1.pdf')).toBeTruthy();
  });
});
