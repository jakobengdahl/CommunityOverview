/**
 * @vitest-environment jsdom
 */
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import EditEdgeDialog from '../src/components/EditEdgeDialog';

vi.mock('../src/store/graphStore', () => ({
  default: () => ({ getRelationshipTypes: () => [{ type: 'KNOWS', description: 'knows' }] }),
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
  { id: 'n1', name: 'Alice' },
  { id: 'n2', name: 'Bob' },
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
    expect(screen.getByLabelText('Label')).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText('Optional label for this connection...')
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
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
