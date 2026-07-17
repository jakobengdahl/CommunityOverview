import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import FloatingHeader from '../src/components/FloatingHeader';
import { I18nProvider, useI18n } from '../src/i18n';

describe('FloatingHeader', () => {
  it('toggles the session drawer from the hamburger button', () => {
    const onToggleDrawer = vi.fn();
    render(
      <I18nProvider>
        <FloatingHeader title="Test Graph" sessionId="1234-5678" onToggleDrawer={onToggleDrawer} />
      </I18nProvider>
    );

    expect(screen.getByText('1234-5678')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Menu'));
    expect(onToggleDrawer).toHaveBeenCalledTimes(1);
  });

  it('falls back to the i18n default title when no title prop is given', () => {
    render(
      <I18nProvider>
        <FloatingHeader sessionId="1234-5678" />
      </I18nProvider>
    );
    expect(screen.getByText('Community Graph View')).toBeInTheDocument();
  });

  it('hides the presence roster when the local user is alone', () => {
    const { container } = render(
      <I18nProvider>
        <FloatingHeader
          sessionId="1234-5678"
          currentClientId="me"
          roster={[{ client_id: 'me', display_name: 'Me', color: '#111' }]}
        />
      </I18nProvider>
    );
    expect(container.querySelector('.floating-header-presence')).toBeNull();
  });

  it('returns explicit fallback text when a translation key is missing (not the key name)', () => {
    // Regression for the t('key') || 'fallback' anti-pattern: t() used to return the key
    // name (truthy) on a miss, so the || branch never fired. The fix adds a 3rd fallback
    // parameter: t(key, params, fallback) returns fallback when the key is absent.
    function ProbeComponent() {
      const { t } = useI18n();
      const withFallback = t('nonexistent.key.xyz', undefined, 'Expected Fallback');
      const withoutFallback = t('nonexistent.key.xyz');
      return (
        <>
          <span data-testid="with-fallback">{withFallback}</span>
          <span data-testid="without-fallback">{withoutFallback}</span>
        </>
      );
    }
    render(
      <I18nProvider>
        <ProbeComponent />
      </I18nProvider>
    );

    // With explicit fallback: the fallback string is returned, not the key name.
    expect(screen.getByTestId('with-fallback')).toHaveTextContent('Expected Fallback');
    // Without fallback: key name is still returned (backward-compatible behaviour).
    expect(screen.getByTestId('without-fallback')).toHaveTextContent('nonexistent.key.xyz');
  });

  it('renders a presence dot per member once another user is connected', () => {
    const { container } = render(
      <I18nProvider>
        <FloatingHeader
          sessionId="1234-5678"
          currentClientId="me"
          roster={[
            { client_id: 'me', display_name: 'Me', color: '#111' },
            { client_id: 'other', display_name: 'Ada', color: '#e6194b' },
          ]}
        />
      </I18nProvider>
    );
    const dots = container.querySelectorAll('.floating-header-presence-dot');
    expect(dots).toHaveLength(2);
    // Self dot is flagged so it can be visually distinguished.
    expect(container.querySelector('.floating-header-presence-dot.is-self')).not.toBeNull();
  });
});
