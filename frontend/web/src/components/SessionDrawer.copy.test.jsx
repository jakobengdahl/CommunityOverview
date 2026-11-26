// covers: frontend/web/src/components/SessionDrawer.jsx
import { fireEvent, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { renderDrawer } from './SessionDrawer.menuTestUtils';

it('copies the chosen session and closes its context menu', () => {
  const onCopySession = vi.fn();
  renderDrawer({ onCopySession });
  fireEvent.contextMenu(screen.getByText('Test session'));
  fireEvent.click(screen.getByRole('menuitem', { name: 'Create a copy' }));
  expect(onCopySession).toHaveBeenCalledWith('abc-123');
  expect(screen.queryByRole('menu')).toBeNull();
});
