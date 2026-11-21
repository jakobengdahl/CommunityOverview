import { describe, it, expect } from 'vitest';
import { renderHook } from '@testing-library/react';
import {
  CommandRegistryContext,
  createCommandRegistry,
  useRegisterCommands,
} from './commandRegistry';

const command = (id, available = true) => ({
  id,
  labelKey: `label.${id}`,
  descriptionKey: `description.${id}`,
  isAvailable: () => available,
  execute: async () => {},
});

describe('createCommandRegistry', () => {
  it('rejects duplicate ids, lists only available commands and cleans up by identity', () => {
    const registry = createCommandRegistry();
    const open = command('open');
    const cleanup = registry.register(open);
    registry.register(command('hidden', false));

    expect(() => registry.register(command('open'))).toThrow();
    expect(registry.get('open')).toBe(open);
    expect(registry.get('hidden')).toBeDefined();
    expect(registry.list({ view: 'interactive' }).map((c) => c.id)).toEqual(['open']);

    cleanup();
    expect(registry.get('open')).toBeUndefined();
    const again = command('open');
    registry.register(again);
    // A stale cleanup must not remove the command registered after it.
    cleanup();
    expect(registry.get('open')).toBe(again);
  });
});

describe('useRegisterCommands', () => {
  it('registers for the mounted lifetime and does nothing without a registry (kiosk)', () => {
    const commands = [command('a'), command('b')];
    const registry = createCommandRegistry();
    const wrapper = ({ children }) => (
      <CommandRegistryContext.Provider value={registry}>{children}</CommandRegistryContext.Provider>
    );
    const { unmount } = renderHook(() => useRegisterCommands(commands), { wrapper });
    expect(registry.list({}).map((c) => c.id)).toEqual(['a', 'b']);
    unmount();
    expect(registry.list({})).toEqual([]);

    expect(() => renderHook(() => useRegisterCommands(commands))).not.toThrow();
  });
});
