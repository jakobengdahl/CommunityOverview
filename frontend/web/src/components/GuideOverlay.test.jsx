/**
 * @vitest-environment jsdom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import GuideOverlay from './GuideOverlay';
import useGraphStore from '../store/graphStore';
import * as api from '../services/api';
import en from '../i18n/en.json';
import { expectAllKeysResolved, missingKeys } from '../test/strictI18n';

vi.mock('../i18n', async () => (await import('../test/strictI18n')).strictI18nModule);
vi.mock('../services/api', () => ({ searchGraph: vi.fn() }));

const g = en.guide;
const initialState = useGraphStore.getState();

const start = (steps) => act(() => useGraphStore.getState().startGuide({ name: 'Tour', steps }));
const counter = (current, total) =>
  g.step_counter.replace('{current}', current).replace('{total}', total);

beforeEach(() => {
  missingKeys.length = 0;
});

afterEach(() => {
  vi.restoreAllMocks();
  useGraphStore.setState(initialState, true);
  expectAllKeysResolved();
});

describe('GuideOverlay', () => {
  it('renders nothing while no guide is active', () => {
    const { container } = render(<GuideOverlay />);
    expect(container).toBeEmptyDOMElement();
  });

  it('walks the steps with Next and ends the guide on the last step', () => {
    render(<GuideOverlay />);
    start([{ text: 'First' }, { text: 'Second' }]);

    expect(screen.getByRole('dialog', { name: 'Tour' })).toBeInTheDocument();
    expect(screen.getByText('First')).toBeInTheDocument();
    expect(screen.getByText(counter(1, 2))).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: g.next }));

    expect(screen.getByText('Second')).toBeInTheDocument();
    expect(screen.getByText(counter(2, 2))).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: g.close }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(useGraphStore.getState().guide.isActive).toBe(false);
  });

  // The input sits inside the dialog, whose own Enter handler also advances:
  // one Enter must move one step, not skip the step after the input.
  it('stores an input step answer on Enter and moves exactly one step', () => {
    render(<GuideOverlay />);
    start([
      { type: 'input', text: 'Your name?', store_as: 'who' },
      { text: 'Thanks' },
      { text: 'Last' },
    ]);

    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'Ada' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(screen.getByText('Thanks')).toBeInTheDocument();
    expect(useGraphStore.getState().guide.currentStepIndex).toBe(1);
    expect(useGraphStore.getState().guide.userInputs).toEqual({ who: 'Ada' });
  });

  it('stops the guide on Escape and on Cancel', () => {
    render(<GuideOverlay />);
    start([{ text: 'First' }, { text: 'Second' }]);
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(useGraphStore.getState().guide.isActive).toBe(false);

    start([{ text: 'First' }, { text: 'Second' }]);
    // The header's × and the footer button share the label; the footer one is last.
    fireEvent.click(screen.getAllByRole('button', { name: g.cancel }).at(-1));
    expect(useGraphStore.getState().guide.isActive).toBe(false);
  });

  it('adds search_nodes results to the canvas before showing the step', async () => {
    api.searchGraph.mockResolvedValueOnce({ nodes: [{ id: 'n1', name: 'Hit' }], edges: [] });
    render(<GuideOverlay />);
    start([{ type: 'search_nodes', query: 'ai', text: 'Found' }, { text: 'Next' }]);

    expect(await screen.findByText('Found')).toBeInTheDocument();
    expect(api.searchGraph).toHaveBeenCalledWith('ai', { nodeTypes: undefined, limit: 30 });
    expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(['n1']);
  });

  it('shows a failed action error and lets the visitor move on', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    api.searchGraph.mockRejectedValueOnce(new Error('search down'));
    render(<GuideOverlay />);
    start([{ type: 'search_nodes', query: 'x', text: 'Again' }, { text: 'Next' }]);

    expect(await screen.findByText('search down')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: g.next })).toBeEnabled();
  });
});
