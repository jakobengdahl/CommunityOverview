import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import SummonNotice from '../src/components/SummonNotice';
import { I18nProvider } from '../src/i18n';

function renderNotice(notice, handlers = {}) {
  return render(
    <I18nProvider>
      <SummonNotice notice={notice} onReturn={handlers.onReturn} onDismiss={handlers.onDismiss} />
    </I18nProvider>
  );
}

describe('SummonNotice', () => {
  it('names the summoner and offers Return', () => {
    const onReturn = vi.fn();
    renderNotice({ summonedBy: 'Ada', returnTo: { x: 0, y: 0, zoom: 1 } }, { onReturn });
    expect(screen.getByText('Ada brought you here')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Return'));
    expect(onReturn).toHaveBeenCalledTimes(1);
  });

  it('falls back to an anonymous notice and hides Return without a camera to go back to', () => {
    renderNotice({ summonedBy: null, returnTo: null });
    expect(screen.getByText('Someone brought you here')).toBeInTheDocument();
    expect(screen.queryByText('Return')).toBeNull();
  });

  it('renders nothing without a notice', () => {
    const { container } = renderNotice(null);
    expect(container).toBeEmptyDOMElement();
  });
});
