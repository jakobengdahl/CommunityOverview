// covers: frontend/web/src/components/CommandBoxHint.jsx
import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import CommandBoxHint from './CommandBoxHint';
import useUserPreferencesStore from '../store/userPreferencesStore';
import { I18nProvider } from '../i18n';

const renderHint = () =>
  render(
    <I18nProvider>
      <CommandBoxHint />
    </I18nProvider>
  );

describe('CommandBoxHint', () => {
  beforeEach(() => window.localStorage.clear());

  it('shows only in the command-box-only layout, and stays dismissed once dismissed', () => {
    useUserPreferencesStore.setState({ preferences: { hideClassicSurfaces: false } });
    const { unmount } = renderHint();
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
    unmount();

    useUserPreferencesStore.setState({ preferences: { hideClassicSurfaces: true } });
    const second = renderHint();
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }));
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
    second.unmount();

    renderHint();
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
  });
});
