import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import FloatingHeader from '../src/components/FloatingHeader';
import { I18nProvider } from '../src/i18n';

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
