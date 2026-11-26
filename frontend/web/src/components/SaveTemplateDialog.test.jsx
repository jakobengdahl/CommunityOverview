// covers: frontend/web/src/components/SaveTemplateDialog.jsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import SaveTemplateDialog, { SYSTEM_NODE_TYPES, templateSummary } from './SaveTemplateDialog';
import * as api from '../services/api';

vi.mock('../i18n', () => ({ useI18n: () => ({ t: (key) => key }) }));
vi.mock('../services/api', () => ({ saveTemplate: vi.fn() }));
const source = {
  seq: 7,
  resolved: {
    nodes: [
      { id: 'a', type: 'Actor' },
      { id: 'b', type: 'Actor' },
      { id: 's', type: 'Agent' },
    ],
    edges: [
      { source: 'a', target: 'b' },
      { source: 'a', target: 's' },
    ],
  },
  state: {
    annotations: [
      { id: 'note', type: 'note' },
      { id: 'ref', type: 'reference', content: { target_kind: 'resource', target: 'b' } },
    ],
  },
};
const schema = { node_types: { Agent: { category: 'system' } } };
const selection = [
  { id: 'a', annotation: false },
  { id: 'note', annotation: true },
  { id: 'ref', annotation: true },
];

describe('saving templates', () => {
  it('summarizes the selected or whole session including only inner connections and retained annotations', () => {
    expect(templateSummary(source, [], null)).toBeNull();
    expect(templateSummary(source, selection, schema)).toEqual({
      nodes: 1,
      edges: 0,
      annotations: 1,
    });
    expect(templateSummary(source, [], schema)).toEqual({ nodes: 2, edges: 1, annotations: 2 });
    expect(
      templateSummary({ ...source, resolved: { ...source.resolved, edges: [] } }, [], schema)
    ).toEqual({ nodes: 2, edges: 0, annotations: 2 });
  });

  it('uses the capture route system type set, independent of schema categories', () => {
    const python = readFileSync(
      resolve(process.cwd(), '../../backend/config/config_loader.py'),
      'utf8'
    );
    const block = python.match(/^SYSTEM_NODE_TYPES = \{$(.*?)^\}$/ms)[1];
    const types = [...block.matchAll(/^ {4}"(\w+)":/gm)].map((match) => match[1]);
    expect([...SYSTEM_NODE_TYPES].sort()).toEqual(types.sort());
    const nodes = types.map((type) => ({ id: type, type }));
    nodes.push({ id: 'domain', type: 'Custom' });
    expect(
      templateSummary({ resolved: { nodes } }, [], {
        node_types: { Custom: { category: 'system' } },
      })
    ).toEqual({ nodes: 1, edges: 0, annotations: 0 });
  });

  it('waits for schema before displaying counts or enabling save', async () => {
    const props = {
      sessionId: 'session',
      selected: [],
      prepare: () => Promise.resolve(source),
      onClose: vi.fn(),
      onSaved: vi.fn(),
    };
    const { rerender } = render(<SaveTemplateDialog {...props} schema={null} />);
    fireEvent.change(screen.getByLabelText('template.name'), { target: { value: 'Name' } });
    await screen.findByText('template.loading');
    expect(screen.getByRole('button', { name: 'template.save_button' }).disabled).toBe(true);
    rerender(<SaveTemplateDialog {...props} schema={schema} />);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'template.save_button' }).disabled).toBe(false)
    );
  });

  it.each([[selection], [[]]])('validates and sends the chosen scope once', async (selected) => {
    api.saveTemplate.mockReset().mockResolvedValue({
      template: { nodes: [{}], edges: [{}, {}], annotation_document: { annotations: [] } },
    });
    const saved = vi.fn();
    render(
      <SaveTemplateDialog
        sessionId="session"
        selected={selected}
        schema={schema}
        prepare={() => Promise.resolve(source)}
        onClose={vi.fn()}
        onSaved={saved}
      />
    );
    const button = screen.getByRole('button', { name: 'template.save_button' });
    await screen.findByLabelText('template.name');
    fireEvent.change(screen.getByLabelText('template.name'), { target: { value: '   ' } });
    expect(button.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('template.name'), { target: { value: ' Example ' } });
    fireEvent.change(screen.getByLabelText('template.description'), {
      target: { value: 'Description' },
    });
    fireEvent.change(screen.getByLabelText('template.tags'), { target: { value: 'one, two' } });
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);
    await waitFor(() =>
      expect(saved).toHaveBeenCalledExactlyOnceWith({ nodes: 1, edges: 2, annotations: 0 })
    );
    const body = {
      name: 'Example',
      description: 'Description',
      tags: ['one', 'two'],
      expected_revision: 7,
    };
    if (selected.length) Object.assign(body, { node_ids: ['a'], annotation_ids: ['note', 'ref'] });
    expect(api.saveTemplate).toHaveBeenCalledExactlyOnceWith('session', body);
  });

  it.each([
    'access_denied',
    'staged_changes',
    'nodes_limit',
    'edges_limit',
    'annotations_limit',
    'unknown',
  ])('keeps input on a structured failure: %s', async (code) => {
    api.saveTemplate.mockReset().mockRejectedValue({ code });
    render(
      <SaveTemplateDialog
        sessionId="session"
        selected={[]}
        schema={schema}
        prepare={() => Promise.resolve(source)}
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />
    );
    fireEvent.change(screen.getByLabelText('template.name'), { target: { value: 'Kept' } });
    const button = screen.getByRole('button', { name: 'template.save_button' });
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);
    await screen.findByRole('alert');
    expect(screen.getByLabelText('template.name').value).toBe('Kept');
    expect(screen.getByRole('alert').textContent).toBe(
      `template.errors.${code === 'unknown' ? 'other' : code}`
    );
  });
});
