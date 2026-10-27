/**
 * @vitest-environment jsdom
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import CreateSkillDialog from './CreateSkillDialog';

const typeInto = (label, value) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });

describe('CreateSkillDialog', () => {
  it('builds a node of the requested skill type with SKILL.md metadata from the form', () => {
    const onSave = vi.fn();
    const onClose = vi.fn();
    render(<CreateSkillDialog nodeType="Playbook" onClose={onClose} onSave={onSave} />);

    const whenToUse = `When ${'x'.repeat(130)}`;
    typeInto('Name *', ' Analyse ');
    typeInto('Description', ' Finds gaps ');
    typeInto('Aliases / synonyms (comma-separated)', 'gap, ,audit');
    typeInto('When to use', ` ${whenToUse} `);
    typeInto('Content (SKILL.md)', '\n## Steps\n1. Search\n');
    typeInto('Source URL', ' https://example.com/SKILL.md ');
    typeInto('Allowed tools (comma-separated)', 'search_graph, , add_nodes');
    typeInto('Version', '1.2.0');
    fireEvent.change(screen.getByLabelText('Effort'), { target: { value: 'high' } });
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
    typeInto('Name *', '   ');
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
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));

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
});
