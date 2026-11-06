/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import AgentProposalsDialog from './AgentProposalsDialog';

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: (key) => key, language: 'en' }),
}));

const PENDING = {
  id: 'prop-1',
  agent_id: 'a1',
  agent_name: 'Agent One',
  tool: 'graph.add_nodes',
  input_args: { nodes: [1] },
  autonomy_level: 'act_after_approval',
  status: 'pending',
  created_at: '2026-08-10T10:00:00Z',
};

afterEach(() => vi.restoreAllMocks());

describe('AgentProposalsDialog', () => {
  beforeEach(() => {
    global.fetch = vi.fn();
  });

  it('lists proposals scoped by agent and shows approve/reject for pending', async () => {
    global.fetch.mockResolvedValue({ ok: true, json: async () => [PENDING] });

    render(<AgentProposalsDialog agentId="a1" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText('Agent One')).toBeInTheDocument());
    expect(global.fetch).toHaveBeenCalledWith('/agents/proposals?agent_id=a1');
    expect(screen.getByText('agent_proposals.approve')).toBeInTheDocument();
    expect(screen.getByText('agent_proposals.reject')).toBeInTheDocument();
  });

  it('shows a notice on every tainted proposal before expansion, but not clean proposals', async () => {
    global.fetch.mockResolvedValue({
      ok: true,
      json: async () => [
        { ...PENDING, tainted: false },
        { ...PENDING, id: 'prop-2', agent_name: 'Tainted One', tainted: true },
        { ...PENDING, id: 'prop-3', agent_name: 'Tainted Two', tainted: true },
      ],
    });
    render(<AgentProposalsDialog onClose={() => {}} />);
    await screen.findByText('Agent One');

    expect(
      within(screen.getByText('Agent One').closest('tr')).queryByText(
        'agent_proposals.tainted_notice'
      )
    ).not.toBeInTheDocument();
    for (const name of ['Tainted One', 'Tainted Two']) {
      expect(
        within(screen.getByText(name).closest('tr')).getByText('agent_proposals.tainted_notice')
      ).toBeInTheDocument();
    }
  });

  it('approves a proposal via POST and reloads', async () => {
    global.fetch
      .mockResolvedValueOnce({ ok: true, json: async () => [PENDING] }) // initial load
      .mockResolvedValueOnce({ ok: true, json: async () => ({}) }) // approve POST
      .mockResolvedValueOnce({
        ok: true,
        json: async () => [{ ...PENDING, status: 'applied' }],
      }); // reload

    render(<AgentProposalsDialog onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('Agent One')).toBeInTheDocument());

    fireEvent.click(screen.getByText('agent_proposals.approve'));

    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith(
        '/agents/proposals/prop-1/approve',
        expect.objectContaining({ method: 'POST' })
      )
    );
    await waitFor(() =>
      expect(screen.getByText('agent_proposals.status_applied')).toBeInTheDocument()
    );
  });

  it('shows the empty state', async () => {
    global.fetch.mockResolvedValue({ ok: true, json: async () => [] });
    render(<AgentProposalsDialog onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('agent_proposals.empty')).toBeInTheDocument());
    expect(global.fetch).toHaveBeenCalledWith('/agents/proposals');
  });

  it('shows an error when the request fails', async () => {
    global.fetch.mockResolvedValue({ ok: false, status: 500 });
    render(<AgentProposalsDialog onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('agent_proposals.load_error')).toBeInTheDocument());
  });
});
