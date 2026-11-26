import { createContext, useContext, useEffect, useSyncExternalStore } from 'react';

/**
 * The command box's registry (docs/COMMAND_BOX_DESIGN.md, "Command registry
 * sketch"). A command is
 *
 *   { id, labelKey, labelParams?, descriptionKey, aliasKeys?,
 *     isAvailable(context), execute(context) }
 *
 * where `id` is stable and language-independent, and `execute` opens the same
 * action flow the classic surface's button does. Availability here only decides
 * what is offered; the handlers and the backend stay the authorization boundary.
 */
export function createCommandRegistry() {
  const commands = new Map();
  const listeners = new Set();
  let version = 0;

  const notify = () => {
    version += 1;
    listeners.forEach((listener) => listener());
  };

  return {
    register(command) {
      if (commands.has(command.id)) {
        throw new Error(`Command "${command.id}" is already registered`);
      }
      commands.set(command.id, command);
      notify();
      let registered = true;
      return () => {
        if (!registered) return;
        registered = false;
        if (commands.get(command.id) === command) {
          commands.delete(command.id);
          notify();
        }
      };
    },
    list(context) {
      return [...commands.values()].filter((command) => command.isAvailable(context));
    },
    get(id) {
      return commands.get(id);
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getVersion() {
      return version;
    },
  };
}

// Provided by the interactive App only. Kiosk views render outside it, so they
// neither register commands nor get suggestions.
export const CommandRegistryContext = createContext(null);

export function useCommandRegistry() {
  return useContext(CommandRegistryContext);
}

const noopSubscribe = () => () => {};

/** Re-renders the caller whenever a command is registered or removed. */
export function useCommandRegistryVersion(registry) {
  return useSyncExternalStore(
    registry ? registry.subscribe : noopSubscribe,
    registry ? registry.getVersion : () => 0
  );
}

/**
 * Registers `commands` for as long as the caller is mounted and `commands`
 * keeps its identity; memoize the array. A no-op without a registry.
 */
export function useRegisterCommands(commands) {
  const registry = useCommandRegistry();
  useEffect(() => {
    if (!registry) return undefined;
    const cleanups = commands.map((command) => registry.register(command));
    return () => cleanups.forEach((cleanup) => cleanup());
  }, [registry, commands]);
}
