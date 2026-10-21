/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import MetamodelExplorerBoundary from './MetamodelExplorerBoundary';

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: (key) => key, language: 'en' }),
}));

let shouldThrow = true;
function Bomb() {
  if (shouldThrow) throw new Error('boom');
  return <div>explorer content</div>;
}

describe('MetamodelExplorerBoundary', () => {
  afterEach(() => {
    shouldThrow = true;
    vi.restoreAllMocks();
  });

  it('shows a readable fallback instead of unmounting when the explorer throws', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    render(
      <div>
        <p>rest of app</p>
        <MetamodelExplorerBoundary onClose={() => {}}>
          <Bomb />
        </MetamodelExplorerBoundary>
      </div>
    );
    expect(screen.getByText('rest of app')).not.toBeNull();
    expect(screen.getByRole('alertdialog')).not.toBeNull();
    expect(screen.getByText('metamodel.error_title')).not.toBeNull();
    expect(screen.getByText('metamodel.error_message')).not.toBeNull();
  });

  it('offers close and retry from the fallback', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const onClose = vi.fn();
    render(
      <MetamodelExplorerBoundary onClose={onClose}>
        <Bomb />
      </MetamodelExplorerBoundary>
    );
    shouldThrow = false;
    fireEvent.click(screen.getByRole('button', { name: 'metamodel.error_retry' }));
    expect(screen.getByText('explorer content')).not.toBeNull();

    shouldThrow = true;
    const { unmount } = render(
      <MetamodelExplorerBoundary onClose={onClose}>
        <Bomb />
      </MetamodelExplorerBoundary>
    );
    fireEvent.click(screen.getAllByRole('button', { name: 'metamodel.error_close' })[0]);
    expect(onClose).toHaveBeenCalled();
    unmount();
  });
});
