/**
 * @vitest-environment jsdom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import CreateAgentDialog from './CreateAgentDialog';
import en from '../i18n/en.json';
import { missingKeys } from '../test/strictI18n';

vi.mock('../i18n', async () => (await import('../test/strictI18n')).strictI18nModule);

vi.mock('../store/graphStore', () => ({
  default: (selector) =>
    selector({
      schema: { node_types: { Actor: {}, Initiative: {}, EventSubscription: {}, Agent: {} } },
    }),
}));

const NOW = 1_700_000_000_000;
const PREFIX = `agent-${NOW.toString(36)}`;
const d = en.agent_dialog;

beforeEach(() => {
  missingKeys.length = 0;
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
  // Every t() call, including placeholders and alert messages, resolved.
  expect(missingKeys).toEqual([]);
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

    typeInto(d.name_label, ' Enricher ');
    typeInto(d.aliases_label, 'enr, , bot');
    typeInto(d.task_prompt_label, ' Enrich new initiatives ');
    expect(screen.queryByRole('checkbox', { name: 'Agent' })).toBeNull();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Initiative' }));
    fireEvent.click(screen.getByRole('checkbox', { name: d.op_update }));
    fireEvent.click(screen.getByRole('checkbox', { name: d.op_delete }));
    typeInto(d.keywords_label, 'ai, data ,');
    fireEvent.click(screen.getByRole('button', { name: d.submit }));

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

    typeInto(d.name_label, '   ');
    typeInto(d.task_prompt_label, 'Do it');
    fireEvent.submit(form);
    expect(window.alert).toHaveBeenLastCalledWith(d.name_required);

    typeInto(d.name_label, 'Agent');
    typeInto(d.task_prompt_label, '  ');
    fireEvent.submit(form);
    expect(window.alert).toHaveBeenLastCalledWith(d.task_prompt_required);
    expect(onSave).not.toHaveBeenCalled();
  });

  // The backend keeps 185 characters so "<name> - Subscription" stays within
  // the 200-character Node.name limit.
  it('accepts a 185-character name, whose subscription name fits in 200 characters', async () => {
    const onSave = vi.fn();
    render(<CreateAgentDialog onClose={vi.fn()} onSave={onSave} />);
    await screen.findByRole('checkbox', { name: /GRAPH/ });
    expect(screen.getByLabelText(d.name_label)).toHaveAttribute('maxLength', '185');

    typeInto(d.name_label, 'n'.repeat(185));
    typeInto(d.task_prompt_label, 'Do it');
    fireEvent.click(screen.getByRole('button', { name: d.submit }));

    expect(window.alert).not.toHaveBeenCalled();
    const [subscription, agent] = onSave.mock.calls[0][0].nodes;
    expect(agent.name).toHaveLength(185);
    expect(subscription.name).toHaveLength(200);
  });

  it('measures the cap on the trimmed name', async () => {
    const onSave = vi.fn();
    render(<CreateAgentDialog onClose={vi.fn()} onSave={onSave} />);
    await screen.findByRole('checkbox', { name: /GRAPH/ });

    typeInto(d.name_label, `  ${'n'.repeat(185)}  `);
    typeInto(d.task_prompt_label, 'Do it');
    fireEvent.click(screen.getByRole('button', { name: d.submit }));

    expect(window.alert).not.toHaveBeenCalled();
    expect(onSave.mock.calls[0][0].nodes[1].name).toHaveLength(185);
  });

  it('refuses a 186-character name instead of sending a create the backend rejects', async () => {
    const onSave = vi.fn();
    const onClose = vi.fn();
    render(<CreateAgentDialog onClose={onClose} onSave={onSave} />);
    await screen.findByRole('checkbox', { name: /GRAPH/ });

    // jsdom does not apply maxLength to a scripted value, as a browser would
    // not to an initial or pasted-over value; the submit check still holds.
    typeInto(d.name_label, 'n'.repeat(186));
    typeInto(d.task_prompt_label, 'Do it');
    fireEvent.click(screen.getByRole('button', { name: d.submit }));

    expect(window.alert).toHaveBeenCalledWith(d.name_too_long.replace('{max}', '185'));
    expect(onSave).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('refuses a 186-character name when editing too', async () => {
    const onSave = vi.fn();
    render(
      <CreateAgentDialog
        onClose={vi.fn()}
        onSave={onSave}
        initialData={{
          agent: { id: 'a1', name: 'Old', aliases: [], metadata: { subscription_id: 's1' } },
          subscription: { id: 's1', metadata: { filters: {}, delivery: {} } },
        }}
      />
    );
    await screen.findByRole('checkbox', { name: /GRAPH/ });

    typeInto(d.name_label, 'n'.repeat(186));
    typeInto(d.task_prompt_label, 'Do it');
    fireEvent.click(screen.getByRole('button', { name: d.submit_edit }));

    expect(window.alert).toHaveBeenCalledWith(d.name_too_long.replace('{max}', '185'));
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
    expect(screen.getByLabelText(d.task_prompt_label)).toHaveValue('legacy prompt');
    fireEvent.click(screen.getByRole('button', { name: d.submit_edit }));

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
