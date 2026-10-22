import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: (key) => key }),
}));

import useGraphStore from '../store/graphStore';
import StagedEditNotice from './StagedEditNotice';

afterEach(() => useGraphStore.getState().setSessionWriteMode('direct'));

describe('StagedEditNotice', () => {
  it('tells the editor an edit in a staged session is held for review', () => {
    useGraphStore.getState().setSessionWriteMode('staged');
    render(<StagedEditNotice />);
    expect(screen.getByTestId('staged-edit-notice').textContent).toBe('staged.edit_notice');
  });

  it('says nothing in a direct session', () => {
    render(<StagedEditNotice />);
    expect(screen.queryByTestId('staged-edit-notice')).toBeNull();
  });
});
