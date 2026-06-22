/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import ConfirmDialog from './ConfirmDialog';

describe('ConfirmDialog', () => {
  let onConfirm;
  let onCancel;

  beforeEach(() => {
    onConfirm = vi.fn();
    onCancel = vi.fn();
  });

  it('renders title and message', () => {
    render(
      <ConfirmDialog
        title="Delete node"
        message="Are you sure you want to delete this node?"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    expect(screen.getByText('Delete node')).toBeDefined();
    expect(screen.getByText('Are you sure you want to delete this node?')).toBeDefined();
  });

  it('renders custom confirm and cancel text', () => {
    render(
      <ConfirmDialog
        title="Test"
        message="Message"
        confirmText="Delete"
        cancelText="Keep"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    expect(screen.getByText('Delete')).toBeDefined();
    expect(screen.getByText('Keep')).toBeDefined();
  });

  it('calls onConfirm when confirm button clicked', () => {
    render(
      <ConfirmDialog
        title="Test"
        message="Message"
        confirmText="OK"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    fireEvent.click(screen.getByText('OK'));
    expect(onConfirm).toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('calls onCancel when cancel button clicked', () => {
    render(
      <ConfirmDialog
        title="Test"
        message="Message"
        cancelText="No"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    fireEvent.click(screen.getByText('No'));
    expect(onCancel).toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('calls onCancel when overlay is clicked', () => {
    render(
      <ConfirmDialog
        title="Test"
        message="Message"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    const overlay = document.querySelector('.confirm-dialog-overlay');
    fireEvent.click(overlay);
    expect(onCancel).toHaveBeenCalled();
  });

  it('calls onConfirm on Enter key', () => {
    render(
      <ConfirmDialog
        title="Test"
        message="Message"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    const dialog = document.querySelector('.confirm-dialog');
    fireEvent.keyDown(dialog, { key: 'Enter' });
    expect(onConfirm).toHaveBeenCalled();
  });

  it('calls onCancel on Escape key', () => {
    render(
      <ConfirmDialog
        title="Test"
        message="Message"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    const dialog = document.querySelector('.confirm-dialog');
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalled();
  });

  it('applies danger style to confirm button when confirmStyle is danger', () => {
    render(
      <ConfirmDialog
        title="Test"
        message="Message"
        confirmText="Delete"
        confirmStyle="danger"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    );
    const btn = screen.getByText('Delete');
    expect(btn.className).toContain('danger');
  });
});
