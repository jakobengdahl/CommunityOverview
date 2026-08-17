/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render } from '@testing-library/react';
import ExpertAgentSelector from './ExpertAgentSelector';

const mockUseGraphStore = vi.fn();

vi.mock('../store/graphStore', () => ({
  default: () => mockUseGraphStore(),
}));

vi.mock('../i18n', () => ({
  useI18n: () => ({ t: (key) => key, language: 'en' }),
}));

function renderWithExperts(availableExperts) {
  mockUseGraphStore.mockImplementation(() => ({
    availableExperts,
    activeExperts: [],
    toggleExpertAgent: vi.fn(),
  }));

  const view = render(<ExpertAgentSelector />);
  fireEvent.click(view.getByTitle('experts.toggle_panel'));
  return view;
}

describe('ExpertAgentSelector icon resolution', () => {
  it('falls back to the default agent glyph when the icon name is an Object prototype member', () => {
    const { container } = renderWithExperts([
      { id: 'no-icon', name: 'No icon', specialty: 'none', color: '#000000' },
      {
        id: 'prototype-key',
        name: 'Prototype key',
        specialty: 'none',
        icon: 'constructor',
        color: '#000000',
      },
    ]);

    const icons = container.querySelectorAll('.expert-selector-icon');
    expect(icons).toHaveLength(2);
    expect(icons[1].innerHTML).toBe(icons[0].innerHTML);
  });

  it('still renders a registered icon name', () => {
    const { container } = renderWithExperts([
      { id: 'no-icon', name: 'No icon', specialty: 'none', color: '#000000' },
      { id: 'starred', name: 'Starred', specialty: 'none', icon: 'StarFill', color: '#000000' },
    ]);

    const icons = container.querySelectorAll('.expert-selector-icon');
    expect(icons).toHaveLength(2);
    expect(icons[1].innerHTML).not.toBe(icons[0].innerHTML);
  });
});
