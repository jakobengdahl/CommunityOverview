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
});
