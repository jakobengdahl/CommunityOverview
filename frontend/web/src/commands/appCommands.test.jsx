// covers: frontend/web/src/commands/appCommands.js
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import en from '../i18n/en.json';
import sv from '../i18n/sv.json';
import FloatingToolbar from '../components/FloatingToolbar';
import { buildAppCommands, buildExpertCommands, getToolbarOrder } from './appCommands';

const schema = vi.hoisted(() => ({
  node_types: {
    Actor: { category: 'domain', labels: { sv: 'Aktör' } },
    Agent: { category: 'system' },
    Skill: { category: 'system', ui_form: 'skill' },
    EventSubscription: { category: 'system' },
    ActiveKnowledgeCollection: { category: 'system' },
  },
}));

vi.mock('../store/graphStore', () => ({ default: (selector) => selector({ schema }) }));
vi.mock('../i18n', () => ({ useI18n: () => ({ t: (key) => key }) }));

const TOOLBAR_HANDLERS = [
  'onCreateNode',
  'onCreateAgent',
  'onCreateSubscription',
  'onSaveView',
  'onCreateGroup',
  'onCreateActiveKnowledgeCollection',
];
const APP_HANDLERS = [
  ...TOOLBAR_HANDLERS,
  'onSaveTemplate',
  'onOpenSettings',
  'onNewSession',
  'onConnectSession',
  'onRenameSession',
  'onCopySession',
  'onCopySessionLink',
  'onCopyTriggerUrl',
  'onDeleteSession',
  'onToggleLock',
  'onEnterFullscreen',
  'onSelectAnnotationTool',
];
const mockHandlers = (names) => Object.fromEntries(names.map((name) => [name, vi.fn()]));
const calls = (handlers) =>
  Object.entries(handlers)
    .filter(([, fn]) => fn.mock.calls.length > 0)
    .map(([name, fn]) => [name, fn.mock.calls]);
const state = {
  schema,
  language: 'sv',
  sessionId: 's-1',
  sessionListed: true,
  canvasLocked: false,
};
const resolve = (dict, key) => key.split('.').reduce((node, part) => node?.[part], dict);
const context = { view: 'interactive', locale: 'sv' };

describe('buildAppCommands', () => {
  const items = getToolbarOrder(schema).filter(Boolean);

  it.each(items)('the %s toolbar button and its command call the same handler', async (type) => {
    const toolbarHandlers = mockHandlers(TOOLBAR_HANDLERS);
    render(<FloatingToolbar {...toolbarHandlers} />);
    fireEvent.click(screen.getAllByRole('button')[items.indexOf(type)]);

    const commandHandlers = mockHandlers(APP_HANDLERS);
    const commands = buildAppCommands(state, commandHandlers);
    const command = commands.find(
      (c) => c.id === (type === 'SavedView' ? 'save-view' : `create:${type}`)
    );
    await command.execute(context);

    expect(calls(commandHandlers)).toEqual(calls(toolbarHandlers));
    expect(calls(commandHandlers)).toHaveLength(1);
  });

  it('registers every listed surface action with keys both languages define', async () => {
    const handlers = mockHandlers(APP_HANDLERS);
    const commands = buildAppCommands(state, handlers);
    for (const command of commands) {
      for (const key of [command.labelKey, command.descriptionKey, ...(command.aliasKeys || [])]) {
        expect([command.id, typeof resolve(en, key), typeof resolve(sv, key)]).toEqual([
          command.id,
          'string',
          'string',
        ]);
      }
      expect(command.isAvailable(context)).toBe(true);
      expect(command.isAvailable({ ...context, view: 'kiosk' })).toBe(false);
      await command.execute(context);
    }
    expect(new Set(commands.map((c) => c.id)).size).toBe(commands.length);
    expect(commands.find((c) => c.id === 'create:Actor').labelParams).toEqual({ type: 'Aktör' });
    // Session actions act on the current session, as the drawer's row menu does.
    for (const name of ['onRenameSession', 'onCopySession', 'onDeleteSession']) {
      expect(handlers[name]).toHaveBeenCalledWith('s-1');
    }
    expect(handlers.onSelectAnnotationTool).toHaveBeenCalledWith('shape', { shape: 'circle' });
    expect(APP_HANDLERS.filter((name) => handlers[name].mock.calls.length === 0)).toEqual([]);
  });

  it('offers row actions only for a session the drawer lists', () => {
    const commands = buildAppCommands(
      { ...state, sessionListed: false },
      mockHandlers(APP_HANDLERS)
    );
    const unavailable = commands.filter((c) => !c.isAvailable(context)).map((c) => c.id);
    expect(unavailable).toEqual([
      'session:rename',
      'session:copy',
      'session:copy-link',
      'session:copy-trigger-url',
      'session:delete',
    ]);
  });
});

describe('buildExpertCommands', () => {
  it('offers only experts not yet active, and none while a request is in flight', async () => {
    const experts = [
      { id: 'e1', name: 'Expert' },
      { id: 'e2', name: 'Other' },
    ];
    const onToggle = vi.fn();
    const idle = buildExpertCommands(experts, { activeExperts: ['e2'], language: 'en' }, onToggle);
    expect(idle.filter((c) => c.isAvailable(context)).map((c) => c.id)).toEqual(['expert:e1']);
    await idle[0].execute(context);
    expect(onToggle).toHaveBeenCalledWith('e1', 'en');

    const busy = buildExpertCommands(experts, { activeExperts: [], disabled: true }, onToggle);
    expect(busy.filter((c) => c.isAvailable(context))).toEqual([]);
  });
});
