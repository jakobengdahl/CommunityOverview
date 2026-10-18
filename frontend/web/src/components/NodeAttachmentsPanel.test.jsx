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
    expect(screen.queryByText('attachments.loading')).toBeNull();
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

describe('NodeAttachmentsPanel upload failures without a usable body', () => {
  it('shows the generic message when the error body is not JSON', async () => {
    fetchMock.mockResolvedValueOnce(listing([])).mockResolvedValueOnce({
      ok: false,
      status: 413,
      json: async () => {
        throw new SyntaxError('not json');
      },
    });
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('attachments.empty');
    chooseFile(fileOfSize(5));
    expect((await screen.findByRole('alert')).textContent).toBe('attachments.errors.generic');
  });

  it('shows the generic message for a validation error whose detail is a list', async () => {
    fetchMock
      .mockResolvedValueOnce(listing([]))
      .mockResolvedValueOnce(jsonResponse(422, { detail: [{ loc: ['query'], msg: 'too long' }] }));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('attachments.empty');
    chooseFile(fileOfSize(5));
    expect((await screen.findByRole('alert')).textContent).toBe('attachments.errors.generic');
  });

  it('refuses any file once the node holds its maximum count, without sending it', async () => {
    fetchMock.mockResolvedValueOnce(listing([record('a', 1), record('b', 1), record('c', 1)]));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('a.pdf');
    chooseFile(fileOfSize(1));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/^attachments\.errors\.node_attachment_limit /);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('NodeAttachmentsPanel coverage of each guarantee', () => {
  it('encodes the file name in the upload URL', async () => {
    fetchMock
      .mockResolvedValueOnce(listing([]))
      .mockResolvedValueOnce(jsonResponse(201, record('x', 1, 'a b&c#d.txt')));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('attachments.empty');
    chooseFile(fileOfSize(1, 'a b&c#d.txt'));
    await screen.findByText('a b&c#d.txt');
    expect(fetchMock.mock.calls[1][0]).toBe('/api/nodes/n1/attachments?filename=a%20b%26c%23d.txt');
  });

  it.each([
    'attachment_too_large',
    'node_attachment_limit',
    'unsupported_attachment_type',
    'attachments_not_allowed',
    'node_archived',
    'federated_node',
    'attachments_unavailable',
  ])('maps server code %s to its own message key', async (code) => {
    fetchMock
      .mockResolvedValueOnce(listing([]))
      .mockResolvedValueOnce(jsonResponse(409, { detail: { error: code, message: 'x' } }));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('attachments.empty');
    chooseFile(fileOfSize(1));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent.startsWith(`attachments.errors.${code} `)).toBe(true);
  });

  it('shows the plain generic message for an unknown code with no message', async () => {
    fetchMock
      .mockResolvedValueOnce(listing([]))
      .mockResolvedValueOnce(jsonResponse(500, { detail: { error: 'other', message: '' } }));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('attachments.empty');
    chooseFile(fileOfSize(1));
    expect((await screen.findByRole('alert')).textContent).toBe('attachments.errors.generic');
  });

  it("shows each row's size and upload time", async () => {
    const at = '2026-10-18T08:00:00Z';
    fetchMock.mockResolvedValueOnce(listing([record('a1', 2048)]));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    const row = (await screen.findByText('a1.pdf')).closest('li');
    const size = new Intl.NumberFormat('en', {
      style: 'unit',
      unit: 'kilobyte',
      unitDisplay: 'short',
      maximumFractionDigits: 1,
    }).format(2);
    expect(row.textContent).toContain(size);
    expect(row.textContent).toContain(new Date(at).toLocaleString('en'));
  });

  it('opens the confirmation on the chosen row only', async () => {
    fetchMock.mockResolvedValueOnce(listing([record('a1'), record('a2')]));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('a1.pdf');
    fireEvent.click(screen.getByLabelText('attachments.delete_named {"name":"a1.pdf"}'));
    expect(screen.queryByText('attachments.confirm_delete {"name":"a2.pdf"}')).toBeNull();
    expect(screen.getByLabelText('attachments.delete_named {"name":"a2.pdf"}')).toBeTruthy();
  });

  it('ignores a list response that arrives after the node changed', async () => {
    let resolveA;
    fetchMock
      .mockReturnValueOnce(new Promise((resolve) => (resolveA = resolve)))
      .mockResolvedValueOnce(listing([record('b1')]));
    const { rerender } = render(<NodeAttachmentsPanel nodeId="A" typeAllowsAttachments />);
    rerender(<NodeAttachmentsPanel nodeId="B" typeAllowsAttachments />);
    await screen.findByText('b1.pdf');
    resolveA(listing([record('a1')]));
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByText('a1.pdf')).toBeNull();
    expect(screen.getByText('b1.pdf')).toBeTruthy();
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

  it('a slow delete on one row neither re-enables it nor closes another row', async () => {
    let finishFirst;
    fetchMock.mockResolvedValueOnce(listing([record('a1'), record('a2')])).mockReturnValueOnce(
      new Promise((resolve) => {
        finishFirst = () => resolve(jsonResponse(200, { success: true }));
      })
    );
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('a1.pdf');
    fireEvent.click(screen.getByLabelText('attachments.delete_named {"name":"a1.pdf"}'));
    const firstPrompt = screen.getByText('attachments.confirm_delete {"name":"a1.pdf"}');
    fireEvent.click(within(firstPrompt.parentElement).getByText('attachments.delete'));

    fireEvent.click(screen.getByLabelText('attachments.delete_named {"name":"a2.pdf"}'));
    // a1's in-flight delete keeps its prompt, disabled, so it cannot be sent twice.
    const stillPending = screen.getByText('attachments.confirm_delete {"name":"a1.pdf"}');
    expect(within(stillPending.parentElement).getByText('attachments.delete').disabled).toBe(true);

    finishFirst();
    await waitFor(() => expect(screen.queryByText('a1.pdf')).toBeNull());
    const secondPrompt = screen.getByText('attachments.confirm_delete {"name":"a2.pdf"}');
    expect(within(secondPrompt.parentElement).getByText('attachments.delete').disabled).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('cancel closes the prompt and restores the Delete button', async () => {
    fetchMock.mockResolvedValueOnce(listing([record('a1')]));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('a1.pdf');
    fireEvent.click(screen.getByLabelText('attachments.delete_named {"name":"a1.pdf"}'));
    fireEvent.click(screen.getByText('attachments.cancel'));
    expect(screen.queryByText('attachments.confirm_delete {"name":"a1.pdf"}')).toBeNull();
    expect(screen.getByLabelText('attachments.delete_named {"name":"a1.pdf"}')).toBeTruthy();
  });

  it('a failed delete closes its prompt and re-enables the row', async () => {
    fetchMock
      .mockResolvedValueOnce(listing([record('a1')]))
      .mockResolvedValueOnce(jsonResponse(500, {}));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('a1.pdf');
    fireEvent.click(screen.getByLabelText('attachments.delete_named {"name":"a1.pdf"}'));
    fireEvent.click(screen.getByText('attachments.delete'));
    await screen.findByRole('alert');
    expect(screen.queryByText('attachments.confirm_delete {"name":"a1.pdf"}')).toBeNull();
    expect(screen.getByLabelText('attachments.delete_named {"name":"a1.pdf"}').disabled).toBe(
      false
    );
  });

  it('two deletes in flight keep each other pending until each finishes', async () => {
    const finish = {};
    fetchMock
      .mockResolvedValueOnce(listing([record('a1'), record('a2')]))
      .mockReturnValueOnce(new Promise((r) => (finish.a1 = r)))
      .mockReturnValueOnce(new Promise((r) => (finish.a2 = r)));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('a1.pdf');
    const confirm = (name) => {
      fireEvent.click(screen.getByLabelText(`attachments.delete_named {"name":"${name}"}`));
      const prompt = screen.getByText(`attachments.confirm_delete {"name":"${name}"}`);
      fireEvent.click(within(prompt.parentElement).getByText('attachments.delete'));
    };
    const buttonsOf = (name) =>
      within(
        screen.getByText(`attachments.confirm_delete {"name":"${name}"}`).parentElement
      ).getAllByRole('button');
    confirm('a1.pdf');
    confirm('a2.pdf');
    expect(buttonsOf('a1.pdf').every((b) => b.disabled)).toBe(true);
    expect(buttonsOf('a2.pdf').every((b) => b.disabled)).toBe(true);

    finish.a1(jsonResponse(200, { success: true }));
    await waitFor(() => expect(screen.queryByText('a1.pdf')).toBeNull());
    expect(buttonsOf('a2.pdf').every((b) => b.disabled)).toBe(true);
    finish.a2(jsonResponse(200, { success: true }));
    await waitFor(() => expect(screen.queryByText('a2.pdf')).toBeNull());
  });
});

describe('NodeAttachmentsPanel list state across uploads and node changes', () => {
  it('an upload keeps the rows already listed', async () => {
    fetchMock
      .mockResolvedValueOnce(listing([record('old')]))
      .mockResolvedValueOnce(jsonResponse(201, record('new', 1, 'new.pdf')));
    render(<NodeAttachmentsPanel nodeId="n1" typeAllowsAttachments />);
    await screen.findByText('old.pdf');
    chooseFile(fileOfSize(1, 'new.pdf'));
    await screen.findByText('new.pdf');
    expect(screen.getByText('old.pdf')).toBeTruthy();
  });

  it('ignores a list failure that arrives after the node changed', async () => {
    let rejectA;
    fetchMock
      .mockReturnValueOnce(new Promise((_, reject) => (rejectA = reject)))
      .mockResolvedValueOnce(listing([record('b1')]));
    const { rerender } = render(<NodeAttachmentsPanel nodeId="A" typeAllowsAttachments />);
    rerender(<NodeAttachmentsPanel nodeId="B" typeAllowsAttachments />);
    await screen.findByText('b1.pdf');
    rejectA(new Error('late'));
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByText('attachments.load_error')).toBeNull();
  });
});
