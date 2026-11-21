import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { createRef, useState } from 'react';
import * as api from '../services/api';
import { CommandRegistryContext, createCommandRegistry } from '../commands/commandRegistry';
import { NODE_QUERY_DEBOUNCE_MS } from '../commands/commandSuggestions';
import ChatComposer, { formatNodeReference } from './ChatComposer';

vi.mock('../services/api', () => ({ searchGraph: vi.fn() }));
vi.mock('../i18n', () => ({ useI18n: () => ({ t: (key) => key, language: 'en' }) }));
vi.mock('../store/graphStore', () => ({
  default: (selector) => selector({ federationDepth: 1, schema: null }),
}));

const goalNode = { id: 'n1', name: 'Goals 2030', type: 'Goal' };
let registry;
let onSend;
const composer = createRef();

function Harness({ initial = '' }) {
  const [value, setValue] = useState(initial);
  return (
    <ChatComposer
      ref={composer}
      value={value}
      onChange={setValue}
      onSend={onSend}
      placeholder="p"
    />
  );
}

const renderComposer = () => {
  render(
    <CommandRegistryContext.Provider value={registry}>
      <Harness />
    </CommandRegistryContext.Provider>
  );
  const input = screen.getByRole('combobox');
  act(() => input.focus());
  return input;
};
const type = (input, value, caret = value.length) =>
  fireEvent.change(input, { target: { value, selectionStart: caret, selectionEnd: caret } });
const settle = () => act(async () => vi.advanceTimersByTimeAsync(NODE_QUERY_DEBOUNCE_MS));
const options = () => screen.queryAllByRole('option');
const key = (input, k, init = {}) => fireEvent.keyDown(input, { key: k, ...init });
const selected = () => options().findIndex((o) => o.getAttribute('aria-selected') === 'true');

function command(id, execute = vi.fn(async () => {})) {
  return { id, labelKey: id, isAvailable: () => true, execute };
}

describe('ChatComposer', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    registry = createCommandRegistry();
    ['alpha', 'beta', 'gamma'].forEach((id) => registry.register(command(id)));
    onSend = vi.fn();
    api.searchGraph.mockReset().mockResolvedValue({ nodes: [goalNode] });
  });
  afterEach(() => vi.useRealTimers());

  it('sends slash-prefixed text as chat on Enter or the send control unless an item is selected', () => {
    const execute = vi.fn();
    registry.register(command('settings', execute));
    const input = renderComposer();
    type(input, '/settings');
    expect(options()).toHaveLength(1);

    expect(key(input, 'Enter', { shiftKey: true })).toBe(true);
    expect(onSend).not.toHaveBeenCalled();
    expect(key(input, 'Enter')).toBe(false);
    act(() => composer.current.submit());
    expect(onSend).toHaveBeenCalledTimes(2);
    expect(execute).not.toHaveBeenCalled();
  });

  it('offers nodes and node types for plain text and only commands in slash mode', async () => {
    const input = renderComposer();
    type(input, 'g');
    await settle();
    expect(options()).toHaveLength(0);

    type(input, 'goal');
    await settle();
    expect(options().map((o) => o.textContent)).toEqual([
      'Goalcommand_box.kind_node_type',
      'Goals 2030command_box.kind_node',
    ]);

    type(input, '  /');
    expect(options().map((o) => o.textContent)).toEqual([
      'alphacommand_box.kind_command',
      'betacommand_box.kind_command',
      'gammacommand_box.kind_command',
    ]);
  });

  it('moves with arrows without wrapping, accepts the first item on Tab and keeps native keys otherwise', async () => {
    const input = renderComposer();
    type(input, '/');
    expect(input).toHaveAttribute('aria-expanded', 'true');
    expect(input).toHaveAttribute('aria-controls', screen.getByRole('listbox').id);
    expect(input).not.toHaveAttribute('aria-activedescendant');

    key(input, 'ArrowUp');
    expect(selected()).toBe(2);
    key(input, 'ArrowDown');
    expect(selected()).toBe(2);
    key(input, 'ArrowUp');
    key(input, 'ArrowUp');
    key(input, 'ArrowUp');
    expect(selected()).toBe(0);
    key(input, 'ArrowDown');
    expect(input).toHaveAttribute('aria-activedescendant', options()[1].id);

    // IME composition and Shift+Tab pass through untouched.
    expect(key(input, 'Enter', { isComposing: true })).toBe(true);
    expect(key(input, 'Tab', { shiftKey: true })).toBe(true);

    // An edit clears the active item; Esc closes and keeps the draft.
    type(input, '/a');
    expect(selected()).toBe(-1);
    expect(key(input, 'Escape')).toBe(false);
    expect(options()).toHaveLength(0);
    expect(input).toHaveValue('/a');
    expect(input).toHaveAttribute('aria-expanded', 'false');
    expect(input).not.toHaveAttribute('aria-controls');
    expect(key(input, 'Tab')).toBe(true);

    key(input, 'ArrowDown');
    expect(selected()).toBe(0);
    type(input, '/');
    key(input, 'ArrowDown');
    key(input, 'ArrowDown');
    await act(async () => expect(key(input, 'Tab')).toBe(false));
    expect(registry.get('alpha').execute).toHaveBeenCalledTimes(1);
    expect(registry.get('beta').execute).not.toHaveBeenCalled();
  });

  it('inserts a node reference at the caret or a type label without sending, and keeps focus', async () => {
    const input = renderComposer();
    // A slash-led word mid-sentence is ordinary text, not command mode.
    type(input, 'see /alpha');
    await settle();
    expect(options()).toHaveLength(0);

    const draft = 'tell me about goals please';
    type(input, draft, draft.indexOf(' please'));
    await settle();
    expect(api.searchGraph).toHaveBeenLastCalledWith('goals', expect.anything());
    key(input, 'ArrowDown');
    key(input, 'Enter');
    const reference = formatNodeReference(goalNode);
    expect(input).toHaveValue(`tell me about ${reference} please`);
    expect(input.selectionStart).toBe(`tell me about ${reference} `.length);

    type(input, 'goal');
    await settle();
    key(input, 'Tab');
    expect(input).toHaveValue('Goal ');
    expect(input).toHaveFocus();
    expect(onSend).not.toHaveBeenCalled();
  });

  it('clears a command draft after handoff and keeps it with an announced failure', async () => {
    registry.register(
      command(
        'broken',
        vi.fn(async () => Promise.reject(new Error('x')))
      )
    );
    const input = renderComposer();
    type(input, '/alpha');
    await act(async () => key(input, 'Tab'));
    expect(registry.get('alpha').execute).toHaveBeenCalledWith({
      view: 'interactive',
      locale: 'en',
    });
    expect(input).toHaveValue('');

    type(input, '/broken');
    await act(async () => key(input, 'Tab'));
    expect(input).toHaveValue('/broken');
    expect(screen.getByRole('status')).toHaveTextContent('command_box.command_failed');
  });

  it('bounds the popup by the room above the box in the visual viewport', () => {
    const viewport = Object.assign(new EventTarget(), { offsetTop: 0, height: 400 });
    vi.stubGlobal('visualViewport', viewport);
    const rect = vi
      .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockReturnValue({ top: 300 });
    try {
      const input = renderComposer();
      type(input, '/');
      expect(screen.getByRole('listbox').style.maxHeight).toBe('min(20rem, 292px)');
      // The keyboard opening scrolls the visual viewport down over the page.
      viewport.offsetTop = 200;
      act(() => viewport.dispatchEvent(new Event('scroll')));
      expect(screen.getByRole('listbox').style.maxHeight).toBe('min(20rem, 92px)');
    } finally {
      rect.mockRestore();
      vi.unstubAllGlobals();
    }
  });

  it('accepts a tapped row before blur can dismiss it; leaving the box closes the list', async () => {
    const input = renderComposer();
    type(input, '/');
    const row = options()[1];
    expect(fireEvent.mouseDown(row)).toBe(false);
    await act(async () => fireEvent.click(row));
    expect(registry.get('beta').execute).toHaveBeenCalledTimes(1);

    type(input, '/g');
    act(() => input.blur());
    expect(options()).toHaveLength(0);
    expect(input).toHaveValue('/g');
  });

  it('without a registry (kiosk) is a plain textarea that never intercepts keys', () => {
    render(<ChatComposer value="/al" onChange={() => {}} onSend={onSend} />);
    const input = screen.getByRole('textbox');
    expect(screen.queryByRole('combobox')).toBeNull();
    expect(key(input, 'ArrowDown')).toBe(true);
    expect(key(input, 'Tab')).toBe(true);
    key(input, 'Enter');
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(api.searchGraph).not.toHaveBeenCalled();
  });
});
