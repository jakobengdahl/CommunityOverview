import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: (key, params) => (params ? `${key}:${JSON.stringify(params)}` : key) }),
}));

import WriteModeBar from './WriteModeBar';

describe('WriteModeBar', () => {
  it('marks a staged session unmistakably, with its pending count and the review', () => {
    const onReview = vi.fn();
    render(
      <WriteModeBar writeMode="staged" stagedAvailable pendingCount={3} onReview={onReview} />
    );
    expect(screen.getByTestId('write-mode-staged')).toBeTruthy();
    expect(screen.getByTestId('staged-pending-count').textContent).toContain('"count":3');
    expect(screen.queryByTestId('write-mode-direct')).toBeNull();
    fireEvent.click(screen.getByText('staged.review'));
    expect(onReview).toHaveBeenCalled();
  });

  it('stays staged even where the capability is not reported', () => {
    render(<WriteModeBar writeMode="staged" stagedAvailable={false} pendingCount={0} />);
    expect(screen.getByTestId('write-mode-staged')).toBeTruthy();
  });

  it('labels a direct session, and offers the switch, only where staging exists', () => {
    const { rerender } = render(<WriteModeBar writeMode="direct" stagedAvailable={false} />);
    expect(screen.queryByTestId('write-mode-direct')).toBeNull();
    const onSwitch = vi.fn();
    rerender(<WriteModeBar writeMode="direct" stagedAvailable onSwitchToStaged={onSwitch} />);
    fireEvent.click(screen.getByText('staged.switch_to_staged'));
    expect(onSwitch).toHaveBeenCalled();
  });

  it('says the count is unknown when the diff could not be read', () => {
    render(<WriteModeBar writeMode="staged" pendingCount={0} diffUnavailable />);
    expect(screen.getByTestId('staged-pending-count').textContent).toBe('staged.pending_unknown');
  });

  it('says a linked session is still loading, with a retry, wherever writes are held', () => {
    const onRetry = vi.fn();
    render(<WriteModeBar writeMode="unknown" stagedAvailable={false} onRetryMode={onRetry} />);
    expect(screen.getByTestId('write-mode-unknown')).toBeTruthy();
    fireEvent.click(screen.getByText('staged.mode_retry'));
    expect(onRetry).toHaveBeenCalled();
  });
});
