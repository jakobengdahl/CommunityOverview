import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useEditableText } from '../src/hooks/useEditableText';
import { AnnotationContext } from '../src/components/AnnotationContext';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

vi.mock('reactflow', () => ({
  useReactFlow: () => ({ setNodes: hoisted.setNodes }),
}));

function applyUpdate(node) {
  const call = hoisted.setNodes.mock.calls.at(-1);
  return call[0]([node])[0];
}

function makeWrapper(contextValue) {
  return function Wrapper({ children }) {
    return <AnnotationContext.Provider value={contextValue}>{children}</AnnotationContext.Provider>;
  };
}

// task-shared-editable-text-hook: unit coverage for the extracted
// commit/debounce/remote-lock contract itself, independent of any one of the
// three components (NoteNode, LabelNode, GenericAnnotationNode) that wire it
// onto an actual input element — those components' own tests
// (NoteNode.test.jsx, LabelNode.test.jsx, GenericAnnotationNode.test.jsx)
// pin that each wiring still behaves identically.
describe('useEditableText', () => {
  beforeEach(() => hoisted.setNodes.mockClear());

  it('seeds text from data.text and starts out not editing', () => {
    const { result } = renderHook(() => useEditableText('n1', { text: 'Hello' }), {
      wrapper: makeWrapper({ notifyChange: vi.fn(), notifyRemoteLockedAttempt: vi.fn() }),
    });
    expect(result.current.isEditing).toBe(false);
    expect(result.current.text).toBe('Hello');
  });

  it('startEditing enters edit mode and stops the triggering event propagating', () => {
    const { result } = renderHook(() => useEditableText('n1', { text: 'Hello' }), {
      wrapper: makeWrapper({ notifyChange: vi.fn(), notifyRemoteLockedAttempt: vi.fn() }),
    });
    const stopPropagation = vi.fn();
    act(() => result.current.startEditing({ stopPropagation }));
    expect(result.current.isEditing).toBe(true);
    expect(stopPropagation).toHaveBeenCalledTimes(1);
  });

  it('handleTextChange live-syncs the raw value and notifies "text"', () => {
    const notifyChange = vi.fn();
    const { result } = renderHook(() => useEditableText('n1', { text: 'Hello' }), {
      wrapper: makeWrapper({ notifyChange, notifyRemoteLockedAttempt: vi.fn() }),
    });
    act(() => result.current.startEditing({ stopPropagation: () => {} }));
    act(() => result.current.handleTextChange({ target: { value: '  typing  ' } }));
    expect(result.current.text).toBe('  typing  ');
    expect(applyUpdate({ id: 'n1', data: { text: 'Hello' } }).data.text).toBe('  typing  ');
    expect(notifyChange).toHaveBeenCalledWith('text');
  });

  it('commitText trims and writes the value, then leaves edit mode', () => {
    const notifyChange = vi.fn();
    const { result } = renderHook(() => useEditableText('n1', { text: 'Hello' }), {
      wrapper: makeWrapper({ notifyChange, notifyRemoteLockedAttempt: vi.fn() }),
    });
    act(() => result.current.startEditing({ stopPropagation: () => {} }));
    act(() => result.current.handleTextChange({ target: { value: '  world  ' } }));
    act(() => result.current.commitText());
    expect(result.current.isEditing).toBe(false);
    expect(applyUpdate({ id: 'n1', data: { text: 'Hello' } }).data.text).toBe('world');
    expect(notifyChange).toHaveBeenCalledWith('text');
  });

  it('handleKeyDown reverts to stored text and exits on Escape, without a write', () => {
    const notifyChange = vi.fn();
    const { result } = renderHook(() => useEditableText('n1', { text: 'Hello' }), {
      wrapper: makeWrapper({ notifyChange, notifyRemoteLockedAttempt: vi.fn() }),
    });
    act(() => result.current.startEditing({ stopPropagation: () => {} }));
    act(() => result.current.handleTextChange({ target: { value: 'discard me' } }));
    notifyChange.mockClear();
    const preventDefault = vi.fn();
    act(() => result.current.handleKeyDown({ key: 'Escape', preventDefault }));
    expect(result.current.isEditing).toBe(false);
    expect(result.current.text).toBe('Hello');
    expect(preventDefault).toHaveBeenCalledTimes(1);
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('handleKeyDown ignores Enter by default (multi-line callers, e.g. NoteNode)', () => {
    const notifyChange = vi.fn();
    const { result } = renderHook(() => useEditableText('n1', { text: 'Hello' }), {
      wrapper: makeWrapper({ notifyChange, notifyRemoteLockedAttempt: vi.fn() }),
    });
    act(() => result.current.startEditing({ stopPropagation: () => {} }));
    const preventDefault = vi.fn();
    act(() => result.current.handleKeyDown({ key: 'Enter', preventDefault }));
    expect(result.current.isEditing).toBe(true);
    expect(preventDefault).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('handleKeyDown commits on Enter when commitOnEnter is set (e.g. LabelNode)', () => {
    const notifyChange = vi.fn();
    const { result } = renderHook(
      () => useEditableText('n1', { text: 'Hello' }, { commitOnEnter: true }),
      { wrapper: makeWrapper({ notifyChange, notifyRemoteLockedAttempt: vi.fn() }) }
    );
    act(() => result.current.startEditing({ stopPropagation: () => {} }));
    act(() => result.current.handleTextChange({ target: { value: '  world  ' } }));
    const preventDefault = vi.fn();
    act(() => result.current.handleKeyDown({ key: 'Enter', preventDefault }));
    expect(result.current.isEditing).toBe(false);
    expect(preventDefault).toHaveBeenCalledTimes(1);
    expect(applyUpdate({ id: 'n1', data: { text: 'Hello' } }).data.text).toBe('world');
  });

  it('refuses to enter edit mode while a remote client holds the selection claim', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    const { result } = renderHook(
      () =>
        useEditableText('n1', {
          text: 'Hello',
          remoteSelection: { color: '#f00', displayName: 'Ada' },
        }),
      { wrapper: makeWrapper({ notifyChange: vi.fn(), notifyRemoteLockedAttempt }) }
    );
    act(() => result.current.startEditing({ stopPropagation: () => {} }));
    expect(result.current.isEditing).toBe(false);
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
  });

  it('handleTextChange refuses to publish while remote-locked, but still updates local draft', () => {
    const notifyChange = vi.fn();
    const { result } = renderHook(
      () =>
        useEditableText('n1', {
          text: 'Hello',
          remoteSelection: { color: '#f00', displayName: 'Ada' },
        }),
      { wrapper: makeWrapper({ notifyChange, notifyRemoteLockedAttempt: vi.fn() }) }
    );
    act(() => result.current.handleTextChange({ target: { value: 'typed anyway' } }));
    expect(result.current.text).toBe('typed anyway');
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('commitText reverts the draft and surfaces the refusal when remote-locked', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    const { result } = renderHook(
      () =>
        useEditableText('n1', {
          text: 'Hello',
          remoteSelection: { color: '#f00', displayName: 'Ada' },
        }),
      { wrapper: makeWrapper({ notifyChange: vi.fn(), notifyRemoteLockedAttempt }) }
    );
    act(() => result.current.commitText());
    expect(result.current.isEditing).toBe(false);
    expect(result.current.text).toBe('Hello');
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyRemoteLockedAttempt).toHaveBeenCalledTimes(1);
  });

  // task-locked-annotation-doubleclick-guard: the persisted `data.locked` flag
  // gates every context menu, but until now not this double-click editor —
  // the one edit path that bypasses the menu entirely. Matches GroupNode's
  // rename guard.
  it('refuses to enter edit mode while locked, without treating it as a remote-claim attempt', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    const { result } = renderHook(() => useEditableText('n1', { text: 'Hello', locked: true }), {
      wrapper: makeWrapper({ notifyChange: vi.fn(), notifyRemoteLockedAttempt }),
    });
    act(() => result.current.startEditing({ stopPropagation: () => {} }));
    expect(result.current.isEditing).toBe(false);
    // Unlike a live remote claim, a standing lock is silent — the menu
    // already explains it via its Unlock button.
    expect(notifyRemoteLockedAttempt).not.toHaveBeenCalled();
  });

  it('closes the editor and discards the pending edit when a lock arrives mid-edit', () => {
    const notifyChange = vi.fn();
    const { result, rerender } = renderHook(({ data }) => useEditableText('n1', data), {
      initialProps: { data: { text: 'Hello' } },
      wrapper: makeWrapper({ notifyChange, notifyRemoteLockedAttempt: vi.fn() }),
    });
    act(() => result.current.startEditing({ stopPropagation: () => {} }));
    act(() => result.current.handleTextChange({ target: { value: 'typed before lock' } }));
    expect(result.current.text).toBe('typed before lock');
    hoisted.setNodes.mockClear();
    notifyChange.mockClear();

    // The lock arrives from another client (or MCP) while the editor is open.
    rerender({ data: { text: 'Hello', locked: true } });
    expect(result.current.isEditing).toBe(false);
    expect(result.current.text).toBe('Hello');

    // Backstop: even if a blur/Escape handler still fires after the
    // render-phase close above, it must not write the abandoned draft.
    act(() => result.current.commitText());
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });
});
