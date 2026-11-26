// The interactive view's commands: what FloatingToolbar, SessionDrawer and the
// annotation toolbox offer, as registry entries calling the same handlers.

// System types always shown at the bottom of the toolbar (not from schema).
const SYSTEM_TYPES = ['Agent', 'Skill', 'EventSubscription', 'ActiveKnowledgeCollection', 'Group'];
const VIEW_TYPES = ['SavedView'];

// Fallback order when schema hasn't loaded yet
const FALLBACK_DOMAIN_ORDER = [
  'Actor',
  'Initiative',
  'Capability',
  'Resource',
  'Legislation',
  'Theme',
  'Goal',
  'Event',
  'Data',
  'Risk',
];

/** FloatingToolbar's items in display order; `null` marks a separator. */
export function getToolbarOrder(schema) {
  const domainTypes = schema?.node_types
    ? Object.entries(schema.node_types)
        .filter(([, config]) => config.category !== 'system')
        .map(([name]) => name)
    : FALLBACK_DOMAIN_ORDER;

  // System types: show from schema if present, always include Group
  const systemTypes = SYSTEM_TYPES.filter(
    (st) => st === 'Group' || !schema?.node_types || schema.node_types[st]
  );

  return [...domainTypes, null, ...systemTypes, null, ...VIEW_TYPES];
}

/** The node types plain-text suggestions offer: the toolbar's domain types. */
export function getSuggestedNodeTypes(schema, language) {
  const order = getToolbarOrder(schema);
  return order
    .slice(0, order.indexOf(null))
    .map((type) => ({ id: type, label: getTypeLabel(schema, type, language) }));
}

export function getTypeLabel(schema, nodeType, language) {
  const labels = schema?.node_types?.[nodeType]?.labels;
  return (language && labels?.[language]) || nodeType;
}

/** What a toolbar item does; the toolbar button and its command both call this. */
export function runToolbarItem(nodeType, handlers) {
  if (nodeType === 'Agent') {
    handlers.onCreateAgent?.();
  } else if (nodeType === 'EventSubscription') {
    handlers.onCreateSubscription?.();
  } else if (nodeType === 'SavedView') {
    handlers.onSaveView?.();
  } else if (nodeType === 'Group') {
    handlers.onCreateGroup?.();
  } else if (nodeType === 'ActiveKnowledgeCollection') {
    handlers.onCreateActiveKnowledgeCollection?.();
  } else {
    handlers.onCreateNode?.(nodeType);
  }
}

const TOOLBAR_COMMAND_KEYS = {
  Agent: { labelKey: 'commands.create_agent', descriptionKey: 'commands.create_agent_description' },
  EventSubscription: {
    labelKey: 'commands.create_subscription',
    descriptionKey: 'commands.create_subscription_description',
    aliasKeys: ['toolbar.webhook'],
  },
  ActiveKnowledgeCollection: {
    labelKey: 'commands.create_knowledge_collection',
    descriptionKey: 'commands.create_knowledge_collection_description',
  },
  Group: {
    labelKey: 'commands.create_group',
    descriptionKey: 'commands.create_group_description',
    aliasKeys: ['toolbar.group'],
  },
  SavedView: { labelKey: 'commands.save_view', descriptionKey: 'commands.save_view_description' },
};

// The annotation toolbox's tools, labelled and described by its own keys.
const ANNOTATION_TOOLS = [
  { id: 'select', kind: 'select', key: 'select' },
  { id: 'note', kind: 'note', key: 'note' },
  { id: 'text', kind: 'text', key: 'text' },
  ...['rectangle', 'circle', 'triangle', 'rhombus', 'hexagon', 'process_arrow'].map((shape) => ({
    id: `shape-${shape}`,
    kind: 'shape',
    options: { shape },
    key: `shape_${shape}`,
  })),
  { id: 'icon', kind: 'icon', key: 'icon' },
  { id: 'vote-dot', kind: 'vote_dot', key: 'vote_dot' },
  { id: 'heatmap', kind: 'heatmap', key: 'heatmap' },
  { id: 'image', kind: 'image', key: 'image' },
  { id: 'freehand', kind: 'freehand', key: 'freehand' },
  { id: 'eraser', kind: 'eraser', key: 'eraser' },
];

const interactive = (context) => context.view === 'interactive';

/**
 * Builds the interactive view's commands from the handlers its surfaces are
 * given. `state` carries what decides labels and availability.
 */
export function buildAppCommands(
  { schema, language, sessionId, sessionListed, canvasLocked },
  handlers
) {
  const command = (id, labelKey, descriptionKey, execute, extra = {}) => ({
    id,
    labelKey,
    descriptionKey,
    isAvailable: interactive,
    execute: async () => execute(),
    ...extra,
  });

  const toolbarCommands = getToolbarOrder(schema)
    .filter(Boolean)
    .map((nodeType) => {
      const keys = TOOLBAR_COMMAND_KEYS[nodeType] || {
        labelKey: 'commands.create_node',
        descriptionKey: 'commands.create_node_description',
        labelParams: { type: getTypeLabel(schema, nodeType, language) },
      };
      const { labelKey, descriptionKey, ...extra } = keys;
      return command(
        nodeType === 'SavedView' ? 'save-view' : `create:${nodeType}`,
        labelKey,
        descriptionKey,
        () => runToolbarItem(nodeType, handlers),
        extra
      );
    });

  // The drawer offers row actions only on listed sessions; a new session
  // stays unlisted until first edited, so its row actions are not offered.
  const sessionCommand = (id, labelKey, handler) =>
    command(
      `session:${id}`,
      labelKey,
      `commands.session_${id.replace(/-/g, '_')}_description`,
      () => handler(sessionId),
      { isAvailable: (context) => interactive(context) && sessionListed }
    );

  return [
    ...toolbarCommands,
    command('save-template', 'template.save', 'template.command_description', () =>
      handlers.onSaveTemplate()
    ),
    command('settings', 'sessions.settings', 'commands.settings_description', () =>
      handlers.onOpenSettings()
    ),
    command('new-session', 'sessions.new_session', 'commands.new_session_description', () =>
      handlers.onNewSession()
    ),
    command(
      'session:connect',
      'sessions.connect_session',
      'commands.session_connect_description',
      () => handlers.onConnectSession()
    ),
    sessionCommand('rename', 'sessions.rename_session', handlers.onRenameSession),
    sessionCommand('copy', 'sessions.create_copy', handlers.onCopySession),
    sessionCommand('copy-link', 'sessions.copy_link', handlers.onCopySessionLink),
    ...(handlers.onCopyTriggerUrl
      ? [sessionCommand('copy-trigger-url', 'sessions.copy_trigger_url', handlers.onCopyTriggerUrl)]
      : []),
    sessionCommand('delete', 'sessions.delete_session', handlers.onDeleteSession),
    command(
      'canvas:toggle-lock',
      canvasLocked ? 'sessions.unlock_canvas' : 'sessions.lock_canvas',
      'commands.toggle_lock_description',
      () => handlers.onToggleLock()
    ),
    command('canvas:fullscreen', 'fullscreen.enter', 'commands.fullscreen_description', () =>
      handlers.onEnterFullscreen()
    ),
    ...ANNOTATION_TOOLS.map((tool) =>
      command(
        `annotation:${tool.id}`,
        `annotation_toolbox.${tool.key}`,
        `annotation_toolbox.${tool.key}_hint`,
        () => handlers.onSelectAnnotationTool(tool.kind, tool.options)
      )
    ),
  ];
}

/** One command per expert agent not yet in the conversation. */
export function buildExpertCommands(experts, { activeExperts, language, disabled }, onToggle) {
  return experts.map((agent) => ({
    id: `expert:${agent.id}`,
    labelKey: 'commands.add_expert',
    labelParams: { name: language === 'sv' ? agent.name : agent.name_en || agent.name },
    descriptionKey: 'commands.add_expert_description',
    isAvailable: (context) =>
      interactive(context) && !disabled && !activeExperts.includes(agent.id),
    execute: async () => onToggle(agent.id, language),
  }));
}
