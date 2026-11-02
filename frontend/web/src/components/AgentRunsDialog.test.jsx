/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import AgentRunsDialog from './AgentRunsDialog';

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: (key) => key, language: 'en' }),
}));

const RUNS = [
  {
    id: 'job-1',
    agent_id: 'a1',
    agent_name: 'Agent One',
    trigger: 'event',
    event_type: 'node.create',
    status: 'succeeded',
    attempts: 1,
    correlation_id: 'corr-1',
    error: null,
    result: { handled: true },
    started_at: '2026-08-10T10:00:00Z',
    finished_at: '2026-08-10T10:00:02Z',
  },
];

afterEach(() => {
  vi.restoreAllMocks();
});

describe('AgentRunsDialog', () => {
  beforeEach(() => {
    global.fetch = vi.fn();
  });

  it('fetches and renders runs, scoping by agentId', async () => {
    global.fetch.mockResolvedValue({ ok: true, json: async () => RUNS });

    render(<AgentRunsDialog agentId="a1" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText('Agent One')).toBeInTheDocument());
    expect(global.fetch).toHaveBeenCalledWith('/agents/runs?agent_id=a1');
  });

  it('expands a row to show run detail', async () => {
    global.fetch.mockResolvedValue({ ok: true, json: async () => RUNS });

    render(<AgentRunsDialog onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('Agent One')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Agent One'));
    // Detail row surfaces the event type value.
    expect(screen.getByText('node.create')).toBeInTheDocument();
    expect(screen.getByText('corr-1')).toBeInTheDocument();
  });

  it('shows the empty state when there are no runs', async () => {
    global.fetch.mockResolvedValue({ ok: true, json: async () => [] });

    render(<AgentRunsDialog onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('agent_runs.empty')).toBeInTheDocument());
    // No agent_id filter → plain endpoint.
    expect(global.fetch).toHaveBeenCalledWith('/agents/runs');
  });

  it('exposes each mounted instance as a modal dialog named by its own title', async () => {
    global.fetch.mockResolvedValue({ ok: true, json: async () => [] });

    render(
      <>
        <AgentRunsDialog onClose={() => {}} />
        <AgentRunsDialog onClose={() => {}} />
      </>
    );
    const dialogs = screen.getAllByRole('dialog', { name: 'agent_runs.title' });
    expect(dialogs).toHaveLength(2);
    expect(dialogs[0]).toHaveAttribute('aria-modal', 'true');
    expect(dialogs[0].getAttribute('aria-labelledby')).not.toBe(
      dialogs[1].getAttribute('aria-labelledby')
    );
    await waitFor(() => expect(screen.getAllByText('agent_runs.empty')).toHaveLength(2));
  });

  it('shows an error when the request fails', async () => {
    global.fetch.mockResolvedValue({ ok: false, status: 500 });

    render(<AgentRunsDialog onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('agent_runs.load_error')).toBeInTheDocument());
  });
});
