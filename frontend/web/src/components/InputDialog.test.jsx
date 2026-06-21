/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import InputDialog from './InputDialog';

describe('InputDialog', () => {
  let onConfirm;
  let onCancel;

  beforeEach(() => {
    onConfirm = vi.fn();
    onCancel = vi.fn();
  });

  it('renders title, label, and placeholder', () => {
    render(
      <InputDialog
        title="Save View"
        label="View name"
        placeholder="Enter a name..."
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    expect(screen.getByText('Save View')).toBeDefined();
    expect(screen.getByText('View name')).toBeDefined();
    expect(screen.getByPlaceholderText('Enter a name...')).toBeDefined();
  });

  it('confirm button is disabled when input is empty', () => {
    render(
      <InputDialog
        title="Test"
        confirmText="Save"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    const btn = screen.getByText('Save');
    expect(btn.disabled).toBe(true);
  });

  it('confirm button enables after typing', () => {
    render(
      <InputDialog
        title="Test"
        confirmText="Save"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'my view' } });
    const btn = screen.getByText('Save');
    expect(btn.disabled).toBe(false);
  });

  it('calls onConfirm with trimmed value when confirm button clicked', () => {
    render(
      <InputDialog
        title="Test"
        confirmText="Save"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: '  hello  ' } });
    fireEvent.click(screen.getByText('Save'));
    expect(onConfirm).toHaveBeenCalledWith('hello');
  });

  it('calls onConfirm on Enter key', () => {
    render(
      <InputDialog
        title="Test"
        confirmText="Save"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'my name' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onConfirm).toHaveBeenCalledWith('my name');
  });

  it('calls onCancel on Escape key', () => {
    render(
      <InputDialog
        title="Test"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    const input = screen.getByRole('textbox');
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalled();
  });

  it('calls onCancel when cancel button clicked', () => {
    render(
      <InputDialog
        title="Test"
        cancelText="Cancel"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    fireEvent.click(screen.getByText('Cancel'));
    expect(onCancel).toHaveBeenCalled();
  });

  it('calls onCancel when overlay is clicked', () => {
    render(
      <InputDialog
        title="Test"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    const overlay = document.querySelector('.input-dialog-overlay');
    fireEvent.click(overlay);
    expect(onCancel).toHaveBeenCalled();
  });

  it('disables buttons and input while loading', () => {
    render(
      <InputDialog
        title="Test"
        confirmText="Save"
        cancelText="Cancel"
        isLoading={true}
        defaultValue="existing"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    expect(screen.getByRole('textbox').disabled).toBe(true);
    expect(screen.getByText('Cancel').disabled).toBe(true);
  });
});
