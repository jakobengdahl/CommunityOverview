import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import CollectionForm from '../src/components/CollectionForm';

describe('CollectionForm', () => {
  const onSubmit = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
  });

  const form = {
    title: 'Feedback',
    submit_label: 'Send it',
    fields: [
      {
        id: 'role',
        label: 'Your role',
        type: 'radio',
        options: ['Manager', 'Analyst'],
        required: true,
      },
      {
        id: 'topics',
        label: 'Topics',
        type: 'checkbox',
        options: [
          { value: 'a', label: 'AI' },
          { value: 'b', label: 'Data' },
        ],
      },
      { id: 'score', label: 'Satisfaction', type: 'slider', min: 1, max: 5, step: 1 },
      { id: 'comment', label: 'Comment', type: 'text' },
    ],
  };

  it('renders title, fields and options', () => {
    render(<CollectionForm form={form} onSubmit={onSubmit} />);
    expect(screen.getByText('Feedback')).toBeInTheDocument();
    expect(screen.getByText('Your role')).toBeInTheDocument();
    expect(screen.getByText('Manager')).toBeInTheDocument();
    expect(screen.getByText('AI')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send it' })).toBeInTheDocument();
  });

  it('blocks submit and shows hint when a required field is unanswered', () => {
    render(<CollectionForm form={form} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByRole('button', { name: 'Send it' }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText('Please answer all required questions.')).toBeInTheDocument();
  });

  it('submits structured answers with field_id, type and value', () => {
    render(<CollectionForm form={form} onSubmit={onSubmit} />);

    fireEvent.click(screen.getByLabelText('Manager'));
    fireEvent.click(screen.getByLabelText('Data'));
    fireEvent.click(screen.getByRole('button', { name: 'Send it' }));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    const answers = onSubmit.mock.calls[0][0];
    const byId = Object.fromEntries(answers.map((a) => [a.field_id, a]));
    expect(byId.role.value).toBe('Manager');
    expect(byId.role.type).toBe('radio');
    expect(byId.topics.value).toEqual(['b']);
    expect(byId.score.value).toBe(3); // slider defaults to midpoint
  });

  it('locks the form and shows submitted label when submitted', () => {
    render(
      <CollectionForm form={form} onSubmit={onSubmit} submitted labels={{ submitted: 'Done' }} />
    );
    const button = screen.getByRole('button', { name: 'Done' });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
