import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

import AppInstallPrompt from '../src/components/AppInstallPrompt';
import { I18nProvider } from '../src/i18n';

function renderPrompt() {
  return render(
    <I18nProvider>
      <AppInstallPrompt />
    </I18nProvider>
  );
}

function dispatchInstallPrompt(prompt = vi.fn().mockResolvedValue({ outcome: 'accepted' })) {
  const event = new Event('beforeinstallprompt');
  event.preventDefault = vi.fn();
  event.prompt = prompt;
  event.userChoice = Promise.resolve({ outcome: 'accepted' });

  window.dispatchEvent(event);

  return { event, prompt };
}

describe('AppInstallPrompt', () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.resetModules();
  });

  it('stays hidden until the browser exposes an install prompt', () => {
    renderPrompt();

    expect(screen.queryByRole('button', { name: /install app/i })).not.toBeInTheDocument();
  });

  it('shows an install button and invokes the captured browser prompt', async () => {
    renderPrompt();

    let event;
    let prompt;
    act(() => {
      ({ event, prompt } = dispatchInstallPrompt());
    });

    expect(event.preventDefault).toHaveBeenCalledTimes(1);
    const button = await screen.findByRole('button', { name: /install app/i });

    fireEvent.click(button);

    await waitFor(() => expect(prompt).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /install app/i })).not.toBeInTheDocument()
    );
  });

  it('uses translated install copy instead of hardcoded visible strings', async () => {
    renderPrompt();

    const prompt = vi.fn().mockResolvedValue({ outcome: 'accepted' });
    act(() => {
      dispatchInstallPrompt(prompt);
    });

    const button = await screen.findByRole('button', { name: 'Install app' });
    expect(button).toHaveTextContent('Install app');
    fireEvent.click(button);
    await waitFor(() => expect(prompt).toHaveBeenCalledTimes(1));
  });
});
