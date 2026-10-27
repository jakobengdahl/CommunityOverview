/**
 * @vitest-environment jsdom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import CreateSubscriptionDialog from './CreateSubscriptionDialog';
import en from '../i18n/en.json';

vi.mock('../store/graphStore', () => ({
  default: (selector) =>
    selector({
      schema: { node_types: { Actor: {}, Initiative: {}, EventSubscription: {}, Agent: {} } },
    }),
}));

const d = en.subscription_dialog;

beforeEach(() => {
  vi.spyOn(crypto, 'randomUUID').mockReturnValue('sub-uuid');
  vi.spyOn(window, 'alert').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

const typeInto = (label, value) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

describe('CreateSubscriptionDialog', () => {
  it('builds an EventSubscription node with filters and delivery from the form', () => {
    const onSave = vi.fn();
    const onClose = vi.fn();
    const { container } = render(<CreateSubscriptionDialog onClose={onClose} onSave={onSave} />);

    // Every label comes from the translation table; an unknown key would
    // render as the raw key.
    expect(screen.getByRole('heading', { name: d.title })).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/subscription_dialog\./);

    typeInto(d.name_label, ' Hook ');
    typeInto(d.aliases_label, 'a, b,');
    expect(screen.queryByRole('checkbox', { name: 'EventSubscription' })).toBeNull();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Actor' }));
    fireEvent.click(screen.getByRole('checkbox', { name: d.op_update }));
    typeInto(d.keywords_label, 'x, y');
    typeInto(d.webhook_label, 'https://example.com/hook');
    typeInto(d.ignore_origins_label, 'mcp, web-ui');
    fireEvent.click(screen.getByRole('button', { name: d.submit }));

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0]).toEqual({
      id: 'sub-uuid',
      name: 'Hook',
      type: 'EventSubscription',
      description: 'Webhook subscription: Hook',
      summary: 'Listening on create, delete events',
      aliases: ['a', 'b'],
      metadata: {
        filters: {
          target: { entity_kind: 'node', node_types: ['Actor'] },
          operations: ['create', 'delete'],
          keywords: { any: ['x', 'y'] },
        },
        delivery: {
          webhook_url: 'https://example.com/hook',
          ignore_origins: ['mcp', 'web-ui'],
          ignore_session_ids: [],
        },
      },
      communities: [],
    });
    expect(onClose).toHaveBeenCalled();
  });

  it('refuses a blank name or webhook URL with the translated message', () => {
    const onSave = vi.fn();
    const { container } = render(<CreateSubscriptionDialog onClose={vi.fn()} onSave={onSave} />);
    typeInto(d.name_label, '  ');
    typeInto(d.webhook_label, 'https://example.com/hook');
    fireEvent.submit(container.querySelector('form'));

    typeInto(d.name_label, 'Hook');
    typeInto(d.webhook_label, '');
    fireEvent.submit(container.querySelector('form'));

    expect(window.alert).toHaveBeenCalledTimes(2);
    expect(window.alert).toHaveBeenLastCalledWith(d.validation_error);
    expect(onSave).not.toHaveBeenCalled();
  });

  it('keeps unknown metadata and ignored sessions when editing', () => {
    const onSave = vi.fn();
    render(
      <CreateSubscriptionDialog
        onClose={vi.fn()}
        onSave={onSave}
        initialData={{
          id: 's1',
          name: ' Hook ',
          aliases: [],
          metadata: {
            owner: 'x',
            filters: { target: { node_types: ['Actor'] }, operations: ['delete'] },
            delivery: {
              webhook_url: 'https://example.com/old',
              ignore_origins: ['mcp'],
              ignore_session_ids: ['sess'],
              secret_ref: 'ref',
            },
          },
        }}
      />
    );
    expect(screen.getByRole('heading', { name: d.title_edit })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: d.submit_edit }));

    expect(onSave.mock.calls[0][0]).toEqual({
      id: 's1',
      updates: {
        name: 'Hook',
        description: 'Webhook subscription: Hook',
        summary: 'Listening on delete events',
        aliases: [],
        metadata: {
          owner: 'x',
          filters: {
            target: { entity_kind: 'node', node_types: ['Actor'] },
            operations: ['delete'],
            keywords: { any: [] },
          },
          delivery: {
            webhook_url: 'https://example.com/old',
            ignore_origins: ['mcp'],
            ignore_session_ids: ['sess'],
            secret_ref: 'ref',
          },
        },
      },
    });
  });
});
