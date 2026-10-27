/**
 * @vitest-environment jsdom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import CreateSkillDialog from './CreateSkillDialog';
import en from '../i18n/en.json';
import { expectAllKeysResolved, missingKeys } from '../test/strictI18n';

vi.mock('../i18n', async () => (await import('../test/strictI18n')).strictI18nModule);

const d = en.skill_dialog;

beforeEach(() => {
  missingKeys.length = 0;
});

afterEach(() => {
  // Every t() call, including placeholders, resolved.
  expectAllKeysResolved();
});

const typeInto = (label, value) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

describe('CreateSkillDialog', () => {
  it('builds a node of the requested skill type with SKILL.md metadata from the form', () => {
    const onSave = vi.fn();
    const onClose = vi.fn();
    render(<CreateSkillDialog nodeType="Playbook" onClose={onClose} onSave={onSave} />);

    const whenToUse = `When ${'x'.repeat(130)}`;
    typeInto(d.name_label, ' Analyse ');
    typeInto(d.description_label, ' Finds gaps ');
    typeInto(d.aliases_label, 'gap, ,audit');
    typeInto(d.when_label, ` ${whenToUse} `);
    typeInto(d.content_label, '\n## Steps\n1. Search\n');
    typeInto(d.source_url_label, ' https://example.com/SKILL.md ');
    typeInto(d.allowed_tools_label, 'search_graph, , add_nodes');
    typeInto(d.version_label, '1.2.0');
    fireEvent.change(screen.getByLabelText(d.effort_label), { target: { value: 'high' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create Playbook' }));

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0]).toEqual({
      name: 'Analyse',
      type: 'Playbook',
      description: 'Finds gaps',
      summary: whenToUse.slice(0, 120),
      aliases: ['gap', 'audit'],
      metadata: {
        when_to_use: whenToUse,
        content: '## Steps\n1. Search',
        source_url: 'https://example.com/SKILL.md',
        allowed_tools: ['search_graph', 'add_nodes'],
        version: '1.2.0',
        effort: 'high',
      },
      communities: [],
    });
    expect(onClose).toHaveBeenCalled();
  });

  it('does not save a skill with a blank name', () => {
    const onSave = vi.fn();
    const onClose = vi.fn();
    const { container } = render(<CreateSkillDialog onClose={onClose} onSave={onSave} />);
    typeInto(d.name_label, '   ');
    fireEvent.submit(container.querySelector('form'));
    expect(onSave).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('sends id and updates when editing, falling back to the name for the summary', () => {
    const onSave = vi.fn();
    render(
      <CreateSkillDialog
        onClose={vi.fn()}
        onSave={onSave}
        initialData={{
          id: 'sk1',
          name: 'Analyse',
          aliases: ['gap'],
          metadata: { custom: 1, content: 'Body', allowed_tools: ['search_graph'] },
        }}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: d.submit_edit }));

    expect(onSave.mock.calls[0][0]).toEqual({
      id: 'sk1',
      updates: {
        name: 'Analyse',
        description: '',
        summary: 'Analyse',
        aliases: ['gap'],
        metadata: {
          custom: 1,
          when_to_use: '',
          content: 'Body',
          source_url: '',
          allowed_tools: ['search_graph'],
          version: '',
          effort: '',
        },
      },
    });
  });

  it('titles the dialog with the node type when creating and when editing', () => {
    const { unmount } = render(
      <CreateSkillDialog nodeType="Playbook" onClose={vi.fn()} onSave={vi.fn()} />
    );
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe(
      d.title.replace('{type}', 'Playbook')
    );
    unmount();

    render(
      <CreateSkillDialog
        nodeType="Playbook"
        onClose={vi.fn()}
        onSave={vi.fn()}
        initialData={{ id: 'sk1', name: 'Analyse' }}
      />
    );
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe(
      d.title_edit.replace('{type}', 'Playbook')
    );
  });

  it('shows the translated placeholder on every prose field', () => {
    render(<CreateSkillDialog onClose={vi.fn()} onSave={vi.fn()} />);
    for (const field of ['name', 'description', 'aliases', 'when', 'content']) {
      expect(screen.getByLabelText(d[`${field}_label`])).toHaveAttribute(
        'placeholder',
        d[`${field}_placeholder`]
      );
    }
  });

  it('labels each effort option with its translation', () => {
    render(<CreateSkillDialog onClose={vi.fn()} onSave={vi.fn()} />);
    const options = screen.getAllByRole('option');
    expect(options.map((o) => [o.value, o.textContent])).toEqual([
      ['', d.effort_unset],
      ['low', d.effort_low],
      ['medium', d.effort_medium],
      ['high', d.effort_high],
    ]);
  });

  it('closes on Cancel without saving', () => {
    const onSave = vi.fn();
    const onClose = vi.fn();
    render(<CreateSkillDialog onClose={onClose} onSave={onSave} />);
    typeInto(d.name_label, 'Analyse');
    fireEvent.click(screen.getByRole('button', { name: d.cancel }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onSave).not.toHaveBeenCalled();
  });
});
