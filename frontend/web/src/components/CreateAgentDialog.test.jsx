/**
 * @vitest-environment jsdom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import CreateAgentDialog from './CreateAgentDialog';
import en from '../i18n/en.json';

vi.mock('../store/graphStore', () => ({
  default: (selector) =>
    selector({
      schema: { node_types: { Actor: {}, Initiative: {}, EventSubscription: {}, Agent: {} } },
    }),
}));

const NOW = 1_700_000_000_000;
const PREFIX = `agent-${NOW.toString(36)}`;

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(NOW);
  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => [
      { id: 'GRAPH', description: 'Graph tools' },
      { id: 'WEB', description: 'Web search' },
    ],
  });
  vi.spyOn(window, 'alert').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

const typeInto = (label, value) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

describe('CreateAgentDialog', () => {
  it('builds an Agent and its EventSubscription whose loop guard matches the agent origin', async () => {
    const onSave = vi.fn();
    const onClose = vi.fn();
    render(<CreateAgentDialog onClose={onClose} onSave={onSave} />);

    // GRAPH is preselected once integrations load; WEB is added on top.
    expect(await screen.findByRole('checkbox', { name: /GRAPH/ })).toBeChecked();
    fireEvent.click(screen.getByRole('checkbox', { name: /WEB/ }));

    typeInto('Name *', ' Enricher ');
    typeInto('Aliases / synonyms (comma-separated)', 'enr, , bot');
    typeInto('Task Prompt *', ' Enrich new initiatives ');
    expect(screen.queryByRole('checkbox', { name: 'Agent' })).toBeNull();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Initiative' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Update' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Delete' }));
    typeInto('Keywords (comma-separated, optional)', 'ai, data ,');
    fireEvent.click(screen.getByRole('button', { name: 'Create Agent' }));

    const agentId = `${PREFIX}-node`;
    const subscriptionId = `${PREFIX}-subscription`;
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0]).toEqual({
      nodes: [
        {
          id: subscriptionId,
          name: 'Enricher - Subscription',
          type: 'EventSubscription',
          description: 'Event subscription for agent: Enricher',
          summary: 'Listens to create, delete events',
          metadata: {
            filters: {
              target: { entity_kind: 'node', node_types: ['Initiative'] },
              operations: ['create', 'delete'],
              keywords: { any: ['ai', 'data'] },
            },
            delivery: {
              webhook_url: `internal://agent/${agentId}`,
              // The runtime writes as `agent:<node id>` (EventOrigin.agent) and
              // the dispatcher compares origins exactly.
              ignore_origins: [`agent:${agentId}`],
              ignore_session_ids: [],
            },
          },
        },
        {
          id: agentId,
          name: 'Enricher',
          type: 'Agent',
          description: 'AI agent: Enricher',
          summary: 'MCP agent with 2 integration(s)',
          aliases: ['enr', 'bot'],
          metadata: {
            subscription_id: subscriptionId,
            enabled: true,
            prompts: { task_prompt: 'Enrich new initiatives' },
            mcp_integration_ids: ['GRAPH', 'WEB'],
          },
        },
      ],
      edges: [{ source: agentId, target: subscriptionId, type: 'RELATES_TO' }],
    });
    expect(onClose).toHaveBeenCalled();
  });

  it('refuses a blank name or task prompt without saving', async () => {
    const onSave = vi.fn();
    const { container } = render(<CreateAgentDialog onClose={vi.fn()} onSave={onSave} />);
    await screen.findByRole('checkbox', { name: /GRAPH/ });
    const form = container.querySelector('form');

    typeInto('Name *', '   ');
    typeInto('Task Prompt *', 'Do it');
    fireEvent.submit(form);
    expect(window.alert).toHaveBeenLastCalledWith('Agent name is required');

    typeInto('Name *', 'Agent');
    typeInto('Task Prompt *', '  ');
    fireEvent.submit(form);
    expect(window.alert).toHaveBeenLastCalledWith('Task prompt is required');
    expect(onSave).not.toHaveBeenCalled();
  });

  it('keeps the subscription link and delivery when editing, and drops the legacy agent key', async () => {
    const onSave = vi.fn();
    render(
      <CreateAgentDialog
        onClose={vi.fn()}
        onSave={onSave}
        onViewRuns={vi.fn()}
        onViewProposals={vi.fn()}
        initialData={{
          agent: {
            id: 'a1',
            name: ' Old ',
            aliases: [],
            metadata: {
              subscription_id: 's1',
              agent: { enabled: false, task_prompt: 'legacy prompt', mcp_integrations: ['WEB'] },
            },
          },
          subscription: {
            id: 's1',
            metadata: {
              filters: { target: { node_types: ['Actor'] }, operations: ['update'] },
              delivery: {
                webhook_url: 'internal://agent/a1',
                ignore_origins: ['agent:a1'],
                ignore_session_ids: ['sess'],
              },
            },
          },
        }}
      />
    );
    await screen.findByRole('checkbox', { name: /GRAPH/ });
    // The agent's action buttons are translated, not raw keys.
    expect(screen.getByRole('button', { name: en.agent_runs.view_runs })).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: en.agent_proposals.view_proposals })
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Task Prompt *')).toHaveValue('legacy prompt');
    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));

    const saved = onSave.mock.calls[0][0];
    expect(saved.agentId).toBe('a1');
    expect(saved.subscriptionId).toBe('s1');
    expect(saved.agentUpdates.name).toBe('Old');
    expect(saved.subscriptionUpdates.name).toBe('Old - Subscription');
    expect(saved.subscriptionUpdates.description).toBe('Event subscription for agent: Old');
    expect(saved.agentUpdates.metadata).toEqual({
      subscription_id: 's1',
      enabled: false,
      prompts: { task_prompt: 'legacy prompt' },
      mcp_integration_ids: ['WEB'],
      agent: undefined,
    });
    expect(saved.subscriptionUpdates.metadata).toEqual({
      filters: {
        target: { entity_kind: 'node', node_types: ['Actor'] },
        operations: ['update'],
        keywords: { any: [] },
      },
      delivery: {
        webhook_url: 'internal://agent/a1',
        ignore_origins: ['agent:a1'],
        ignore_session_ids: ['sess'],
      },
    });
  });
});
