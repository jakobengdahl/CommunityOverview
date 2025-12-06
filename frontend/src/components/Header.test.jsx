/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import Header from './Header';
import useGraphStore from '../store/graphStore';

describe('Header', () => {
  beforeEach(() => {
    // Mock window.history.replaceState
    window.history.replaceState = vi.fn();

    useGraphStore.setState({
      selectedCommunities: [],
      setSelectedCommunities: vi.fn((communities) => {
        useGraphStore.setState({ selectedCommunities: communities });
      })
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('renders app title', () => {
    render(<Header />);
    expect(screen.getByText('Community Knowledge Graph')).toBeDefined();
  });

  it('displays community selector button', () => {
    render(<Header />);
    expect(screen.getByRole('button', { name: /Select Communities/i })).toBeDefined();
  });

  it('shows dropdown when button is clicked', () => {
    render(<Header />);

    const dropdownButton = screen.getByRole('button', { name: /Select Communities/i });
    fireEvent.click(dropdownButton);

    expect(screen.getByText('eSam')).toBeDefined();
    expect(screen.getByText('Myndigheter')).toBeDefined();
    expect(screen.getByText('Officiell Statistik')).toBeDefined();
  });

  it('selects a community when checkbox is clicked', () => {
    render(<Header />);

    // Open dropdown
    const dropdownButton = screen.getByRole('button', { name: /Select Communities/i });
    fireEvent.click(dropdownButton);

    // Click eSam checkbox
    const eSamCheckbox = screen.getByRole('checkbox', { name: /eSam/i });
    fireEvent.click(eSamCheckbox);

    // Verify store was updated
    const state = useGraphStore.getState();
    expect(state.selectedCommunities).toContain('eSam');
  });

  it('deselects a community when checked checkbox is clicked', () => {
    useGraphStore.setState({
      selectedCommunities: ['eSam']
    });

    render(<Header />);

    // Open dropdown
    const dropdownButton = screen.getByRole('button');
    fireEvent.click(dropdownButton);

    // Click eSam checkbox to deselect
    const eSamCheckbox = screen.getByRole('checkbox', { name: /eSam/i });
    fireEvent.click(eSamCheckbox);

    // Verify store was updated
    const state = useGraphStore.getState();
    expect(state.selectedCommunities).not.toContain('eSam');
  });

  it('updates URL when communities are selected', () => {
    render(<Header />);

    // Open dropdown
    const dropdownButton = screen.getByRole('button', { name: /Select Communities/i });
    fireEvent.click(dropdownButton);

    // Select eSam
    const eSamCheckbox = screen.getByRole('checkbox', { name: /eSam/i });
    fireEvent.click(eSamCheckbox);

    // Verify URL was updated
    expect(window.history.replaceState).toHaveBeenCalled();
  });

  it('displays selected communities in button text', () => {
    useGraphStore.setState({
      selectedCommunities: ['eSam', 'Myndigheter']
    });

    render(<Header />);

    expect(screen.getByText(/eSam, Myndigheter/i)).toBeDefined();
  });

  it('allows selecting multiple communities', () => {
    render(<Header />);

    // Open dropdown
    const dropdownButton = screen.getByRole('button', { name: /Select Communities/i });
    fireEvent.click(dropdownButton);

    // Select multiple communities
    const eSamCheckbox = screen.getByRole('checkbox', { name: /eSam/i });
    const myndigCheckbox = screen.getByRole('checkbox', { name: /Myndigheter/i });

    fireEvent.click(eSamCheckbox);
    fireEvent.click(myndigCheckbox);

    // Verify both are selected
    const state = useGraphStore.getState();
    expect(state.selectedCommunities).toContain('eSam');
    expect(state.selectedCommunities).toContain('Myndigheter');
  });
});
