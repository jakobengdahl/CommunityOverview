// covers: frontend/web/src/components/SaveTemplateDialog.jsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import SaveTemplateDialog, { templateSummary } from './SaveTemplateDialog';
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
    expect(templateSummary(source, selection, schema)).toEqual({
      nodes: 1,
      edges: 0,
      annotations: 1,
    });
    expect(templateSummary(source, [], schema)).toEqual({ nodes: 2, edges: 1, annotations: 2 });
  });

  it.each([[selection], [[]]])('validates and sends the chosen scope once', async (selected) => {
    api.saveTemplate.mockReset().mockResolvedValue({ success: true });
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
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
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
