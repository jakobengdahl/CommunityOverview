import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { createRef, useState } from 'react';
import * as api from '../services/api';
import { CommandRegistryContext, createCommandRegistry } from '../commands/commandRegistry';
import { NODE_QUERY_DEBOUNCE_MS } from '../commands/commandSuggestions';
import ChatComposer, { formatNodeReference } from './ChatComposer';

const scope = vi.hoisted(() => ({ language: 'en', federationDepth: 1 }));
vi.mock('../services/api', () => ({ searchGraph: vi.fn() }));
vi.mock('../i18n', () => ({ useI18n: () => ({ t: (key) => key, language: scope.language }) }));
vi.mock('../store/graphStore', () => ({
  default: (selector) => selector({ federationDepth: scope.federationDepth, schema: null }),
}));

const goalNode = { id: 'n1', name: 'Goals 2030', type: 'Goal' };
let registry;
let onSend;
let rerenderComposer;
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

const renderComposer = (wrap = (composerElement) => composerElement) => {
  const tree = () => (
    <CommandRegistryContext.Provider value={registry}>
      {wrap(<Harness />)}
    </CommandRegistryContext.Provider>
  );
  const { rerender } = render(tree());
  rerenderComposer = () => rerender(tree());
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
    Object.assign(scope, { language: 'en', federationDepth: 1 });
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

  it('offers nodes and node types only after an @ trigger and only commands in slash mode', async () => {
    const input = renderComposer();
    type(input, 'goals');
    await settle();
    expect(options()).toHaveLength(0);
    // An @ after a non-whitespace character is an e-mail address.
    type(input, 'mail bob@goals');
    await settle();
    expect(api.searchGraph).not.toHaveBeenCalled();
    type(input, '@g');
    await settle();
    expect(options()).toHaveLength(0);

    // The query runs to the caret, spaces included.
    type(input, 'about @goals 20');
    await settle();
    expect(api.searchGraph).toHaveBeenLastCalledWith('goals 20', expect.anything());

    type(input, '@goal');
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
    const sheetKeyDown = vi.fn();
    const input = renderComposer((composerElement) => (
      <div onKeyDown={sheetKeyDown}>{composerElement}</div>
    ));
    type(input, '/');
    expect(input).toHaveAttribute('aria-expanded', 'true');
    expect(input).toHaveAttribute('aria-controls', screen.getByRole('listbox').id);
    expect(input).not.toHaveAttribute('aria-activedescendant');
    // The scrolling popup is no Tab stop, so Shift+Tab leaves the box natively.
    expect(screen.getByRole('listbox')).toHaveAttribute('tabindex', '-1');

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
    // Esc while @ results load keeps them closed when they arrive and stays in the box.
    type(input, '@goal');
    expect(key(input, 'Escape')).toBe(false);
    await settle();
    expect(options()).toHaveLength(0);
    expect(sheetKeyDown.mock.calls.map(([e]) => e.key)).not.toContain('Escape');
    // With no list shown, Escape belongs to the hosting sheet.
    type(input, '/zzz');
    expect(key(input, 'Escape')).toBe(true);
    api.searchGraph.mockResolvedValueOnce({ nodes: [] });
    type(input, '@zz');
    await settle();
    expect(key(input, 'Escape')).toBe(true);
    expect(sheetKeyDown.mock.calls.filter(([e]) => e.key === 'Escape')).toHaveLength(2);

    type(input, '/a');
    key(input, 'ArrowDown');
    expect(selected()).toBe(0);
    type(input, '/');
    key(input, 'ArrowDown');
    key(input, 'ArrowDown');
    await act(async () => expect(key(input, 'Tab')).toBe(false));
    expect(registry.get('alpha').execute).toHaveBeenCalledTimes(1);
    expect(registry.get('beta').execute).not.toHaveBeenCalled();
  });

  it.each([
    ['language', { language: 'sv' }],
    ['graph scope', { federationDepth: 2 }],
  ])('a %s change clears the selection so Enter sends instead of accepting', (_, change) => {
    const input = renderComposer();
    type(input, '/');
    key(input, 'ArrowDown');
    expect(selected()).toBe(0);
    Object.assign(scope, change);
    rerenderComposer();
    expect(selected()).toBe(-1);
    key(input, 'Enter');
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(registry.get('alpha').execute).not.toHaveBeenCalled();
  });

  it('a caret move that changes the @ query clears the selection so Enter sends', async () => {
    const input = renderComposer();
    type(input, '@goals 2030');
    await settle();
    key(input, 'ArrowDown');
    expect(selected()).toBe(0);
    const moveCaret = (caret) =>
      act(() => {
        input.setSelectionRange(caret, caret);
        fireEvent.select(input);
      });
    moveCaret(6);
    expect(input).not.toHaveAttribute('aria-activedescendant');
    await settle();
    // Back at the end, the results reopen with nothing selected.
    moveCaret(11);
    await settle();
    expect(options().length).toBeGreaterThan(0);
    expect(selected()).toBe(-1);
    key(input, 'Enter');
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(input).toHaveValue('@goals 2030');
  });

  it('inserts a node reference at the caret or a type label without sending, and keeps focus', async () => {
    const input = renderComposer();
    // A slash-led word mid-sentence is ordinary text, not command mode.
    type(input, 'see /alpha');
    await settle();
    expect(options()).toHaveLength(0);

    const draft = 'tell me about @goals please';
    type(input, draft, draft.indexOf(' please'));
    await settle();
    expect(api.searchGraph).toHaveBeenLastCalledWith('goals', expect.anything());
    key(input, 'ArrowDown');
    key(input, 'Enter');
    const reference = formatNodeReference(goalNode);
    expect(input).toHaveValue(`tell me about ${reference} please`);
    expect(input.selectionStart).toBe(`tell me about ${reference} `.length);

    // An @ inside an inserted reference never becomes a trigger of its own.
    api.searchGraph.mockResolvedValueOnce({
      nodes: [{ id: 'n2', name: 'Team @alpha', type: 'Goal' }],
    });
    type(input, '@team');
    await settle();
    key(input, 'Tab');
    type(input, `${input.value}x`);
    await settle();
    expect(api.searchGraph).toHaveBeenLastCalledWith('team', expect.anything());
    expect(options()).toHaveLength(0);

    type(input, '@goal');
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

    // Text typed while the handoff is pending is a new draft and survives it.
    let finish;
    registry.register(
      command(
        'slow',
        vi.fn(() => new Promise((resolve) => (finish = resolve)))
      )
    );
    type(input, '/slow');
    await act(async () => key(input, 'Tab'));
    type(input, 'next question');
    await act(async () => finish());
    expect(input).toHaveValue('next question');

    // A command that became unavailable after it was listed is not run.
    const stale = command('stale');
    registry.register(stale);
    type(input, '/stale');
    stale.isAvailable = () => false;
    await act(async () => key(input, 'Tab'));
    expect(stale.execute).not.toHaveBeenCalled();
    expect(input).toHaveValue('/stale');
    expect(screen.getByRole('status')).toHaveTextContent('command_box.command_failed');

    type(input, '/broken');
    await act(async () => key(input, 'Tab'));
    expect(input).toHaveValue('/broken');
    const status = screen.getByRole('status');
    expect(status).toHaveTextContent('command_box.command_failed');

    // Reopening shows the count again; a repeated failure is a new announcement.
    const firstFailure = status.firstChild;
    key(input, 'ArrowDown');
    expect(status).toHaveTextContent('command_box.results');
    await act(async () => key(input, 'Enter'));
    expect(status).toHaveTextContent('command_box.command_failed');
    expect(status.firstChild).not.toBe(firstFailure);
  });

  it('gives every item a distinct option id that stays its own when the list reorders', async () => {
    api.searchGraph.mockResolvedValue({
      nodes: [
        { id: 'a b', name: 'Goal A', type: 'Goal' },
        { id: 'a_b', name: 'Goal B', type: 'Goal' },
      ],
    });
    const input = renderComposer();
    type(input, '@goal');
    await settle();
    const ids = options().map((o) => o.id);
    expect(ids).toHaveLength(3);
    expect(new Set(ids).size).toBe(3);
    key(input, 'ArrowDown');
    key(input, 'ArrowDown');
    expect(input).toHaveAttribute('aria-activedescendant', ids[1]);

    type(input, '/');
    const before = Object.fromEntries(options().map((o) => [o.textContent, o.id]));
    // A command sorting first shifts every row of the same query down.
    act(() => registry.register(command('aaa')));
    const after = Object.fromEntries(options().map((o) => [o.textContent, o.id]));
    expect(Object.keys(after)).toHaveLength(4);
    Object.keys(before).forEach((label) => expect(after[label]).toBe(before[label]));
    expect(Object.values(before)).not.toContain(Object.values(after)[0]);
  });

  it('bounds the popup by the room above the box in the visual viewport and its scroll container', () => {
    const viewport = Object.assign(new EventTarget(), { offsetTop: 0, height: 400 });
    vi.stubGlobal('visualViewport', viewport);
    let sheetTop = 0;
    let boxTop = 300;
    const rect = vi
      .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockImplementation(function () {
        return { top: this.classList.contains('sheet') ? sheetTop : boxTop };
      });
    const removeListener = vi.spyOn(window, 'removeEventListener');
    try {
      const input = renderComposer((composerElement) => (
        <div className="sheet" style={{ overflowY: 'auto' }}>
          {composerElement}
        </div>
      ));
      type(input, '/');
      expect(screen.getByRole('listbox').style.maxHeight).toBe('min(20rem, 292px)');
      // The keyboard opening scrolls the visual viewport down over the page.
      viewport.offsetTop = 200;
      act(() => viewport.dispatchEvent(new Event('scroll')));
      expect(screen.getByRole('listbox').style.maxHeight).toBe('min(20rem, 92px)');
      // A bottom sheet's scrolling content starting lower clips the popup sooner.
      sheetTop = 250;
      act(() => viewport.dispatchEvent(new Event('resize')));
      expect(screen.getByRole('listbox').style.maxHeight).toBe('min(20rem, 42px)');
      // Scrolling the sheet moves the box within it without any viewport event.
      boxTop = 350;
      act(() => document.querySelector('.sheet').dispatchEvent(new Event('scroll')));
      expect(screen.getByRole('listbox').style.maxHeight).toBe('min(20rem, 92px)');
      // Closing the list stops listening for ancestor scrolls.
      key(input, 'Escape');
      expect(screen.queryByRole('listbox')).toBeNull();
      expect(removeListener).toHaveBeenCalledWith('scroll', expect.any(Function), true);
    } finally {
      removeListener.mockRestore();
      rect.mockRestore();
      vi.unstubAllGlobals();
    }
  });

  it('accepts a tapped row before blur can dismiss it; leaving the box closes the list', async () => {
    const input = renderComposer();
    type(input, '/');
    const row = options()[1];
    // A tap during IME composition does not accept.
    fireEvent.compositionStart(input);
    await act(async () => fireEvent.click(row));
    expect(registry.get('beta').execute).not.toHaveBeenCalled();
    fireEvent.compositionEnd(input);
    expect(fireEvent.mouseDown(row)).toBe(false);
    // Pressing the popup's padding or scrollbar keeps focus too.
    expect(fireEvent.mouseDown(screen.getByRole('listbox'))).toBe(false);
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
