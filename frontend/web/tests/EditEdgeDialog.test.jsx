/**
 * @vitest-environment jsdom
 */
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import EditEdgeDialog from '../src/components/EditEdgeDialog';

vi.mock('../src/store/graphStore', () => ({
  default: () => ({
    getRelationshipTypes: () => [
      { type: 'KNOWS', description: 'knows' },
      { type: 'WORKS_FOR', description: 'employment' },
    ],
    getRelationshipTypesForNodes: (sourceType, targetType) =>
      sourceType === 'Person' && targetType === 'Person'
        ? [{ type: 'KNOWS', description: 'knows' }]
        : [],
  }),
}));

vi.mock('../src/components/EntityHistoryView', () => ({
  default: () => <div data-testid="history-view" />,
}));

const translations = {
  en: {
    'detail.tab_details': 'Details',
    'history.view_history': 'History',
    'edit_edge.title': 'Edit Connection',
    'edit_edge.connection': 'Connection',
    'edit_edge.type': 'Type',
    'edit_edge.no_type': 'No specific type',
    'edit_edge.label_field': 'Label',
    'edit_edge.label_placeholder': 'Optional label for this connection...',
    'context_menu.delete': 'Delete',
    'common.cancel': 'Cancel',
    'common.save': 'Save',
  },
  sv: {
    'detail.tab_details': 'Detaljer',
    'history.view_history': 'Historik',
    'edit_edge.title': 'Redigera koppling',
    'edit_edge.connection': 'Koppling',
    'edit_edge.type': 'Typ',
    'edit_edge.no_type': 'Ingen specifik typ',
    'edit_edge.label_field': 'Etikett',
    'edit_edge.label_placeholder': 'Valfri etikett för den här kopplingen...',
    'context_menu.delete': 'Ta bort',
    'common.cancel': 'Avbryt',
    'common.save': 'Spara',
  },
};

let currentLanguage = 'en';

vi.mock('../src/i18n', () => ({
  useI18n: () => ({
    t: (key) => translations[currentLanguage][key] ?? key,
  }),
}));

const edge = { id: 'e1', source: 'n1', target: 'n2', type: 'KNOWS', label: 'knows' };
const nodes = [
  { id: 'n1', name: 'Alice', type: 'Person' },
  { id: 'n2', name: 'Bob', type: 'Person' },
];

function renderDialog() {
  return render(
    <EditEdgeDialog
      edge={edge}
      nodes={nodes}
      onClose={vi.fn()}
      onSave={vi.fn()}
      onDelete={vi.fn()}
    />
  );
}

describe('EditEdgeDialog', () => {
  beforeEach(() => {
    currentLanguage = 'en';
  });

  it('renders the translated English labels by default', () => {
    renderDialog();

    expect(screen.getByRole('heading', { level: 2, name: 'Edit Connection' })).toBeInTheDocument();
    expect(screen.getByText('Connection')).toBeInTheDocument();
    expect(screen.getByLabelText('Type')).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'No specific type' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'KNOWS - knows' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'WORKS_FOR - employment' })).toBeNull();
    expect(screen.getByLabelText('Label')).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText('Optional label for this connection...')
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
  });

  it('preserves non-visual metadata and does not stamp appearance defaults on save', () => {
    const onSave = vi.fn();
    const plainEdge = {
      id: 'e1',
      source: 'n1',
      target: 'n2',
      type: 'KNOWS',
      label: 'knows',
      metadata: { note: 'keep me' },
    };
    render(
      <EditEdgeDialog
        edge={plainEdge}
        nodes={nodes}
        onClose={vi.fn()}
        onSave={onSave}
        onDelete={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(onSave).toHaveBeenCalledTimes(1);
    const payload = onSave.mock.calls[0][0];
    // Non-visual key survives; no default visual keys are added.
    expect(payload.metadata).toEqual({ note: 'keep me' });
  });

  it('clears both animated and pulse when Animate is unchecked', () => {
    const onSave = vi.fn();
    const pulsedEdge = {
      id: 'e1',
      source: 'n1',
      target: 'n2',
      type: 'KNOWS',
      label: 'knows',
      metadata: { pulse: true, animated: true, color: '#ff0000' },
    };
    const { container } = render(
      <EditEdgeDialog
        edge={pulsedEdge}
        nodes={nodes}
        onClose={vi.fn()}
        onSave={onSave}
        onDelete={vi.fn()}
      />
    );

    const animate = container.querySelector('input[name="animated"]');
    expect(animate.checked).toBe(true);
    fireEvent.click(animate); // uncheck

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    const payload = onSave.mock.calls[0][0];
    expect(payload.metadata).not.toHaveProperty('animated');
    expect(payload.metadata).not.toHaveProperty('pulse');
    // The custom colour is unrelated and must be preserved.
    expect(payload.metadata.color).toBe('#ff0000');
  });

  it('reflects and preserves an externally-set direction regardless of case/whitespace', () => {
    const onSave = vi.fn();
    const dirEdge = {
      id: 'e1',
      source: 'n1',
      target: 'n2',
      type: 'KNOWS',
      label: 'knows',
      metadata: { direction: '  Forward ' },
    };
    const { container } = render(
      <EditEdgeDialog
        edge={dirEdge}
        nodes={nodes}
        onClose={vi.fn()}
        onSave={onSave}
        onDelete={vi.fn()}
      />
    );

    // The dialog normalizes and shows 'forward' rather than defaulting to 'none'.
    const direction = container.querySelector('select[name="direction"]');
    expect(direction.value).toBe('forward');

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave.mock.calls[0][0].metadata.direction).toBe('forward');
  });

  it('clamps an out-of-range thickness on save', () => {
    const onSave = vi.fn();
    const thickEdge = {
      id: 'e1',
      source: 'n1',
      target: 'n2',
      type: 'KNOWS',
      label: 'knows',
      metadata: { thickness: 999 },
    };
    render(
      <EditEdgeDialog
        edge={thickEdge}
        nodes={nodes}
        onClose={vi.fn()}
        onSave={onSave}
        onDelete={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSave.mock.calls[0][0].metadata.thickness).toBe(12);
  });

  it('switches the dialog labels when translations resolve to Swedish', () => {
    currentLanguage = 'sv';

    renderDialog();

    expect(
      screen.getByRole('heading', { level: 2, name: 'Redigera koppling' })
    ).toBeInTheDocument();
    expect(screen.getByText('Koppling')).toBeInTheDocument();
    expect(screen.getByLabelText('Typ')).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Ingen specifik typ' })).toBeInTheDocument();
    expect(screen.getByLabelText('Etikett')).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText('Valfri etikett för den här kopplingen...')
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Ta bort' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Avbryt' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Spara' })).toBeInTheDocument();
  });
});
