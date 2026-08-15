import { useState, useEffect, useCallback } from 'react';
import { ClipboardFill } from 'react-bootstrap-icons';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import './CreateSubscriptionDialog.css'; // Reuse the same styles

const EXCLUDED_TYPES = [
  'SavedView',
  'VisualizationView',
  'EventSubscription',
  'Agent',
  'Skill',
  'ActiveKnowledgeCollection',
  'CollectionResponse',
];

// Tools the collection kiosk assistant may be granted, keyed by the exact tool
// name the backend advertises. Mirrors ChatProcessor._generate_tool_definitions
// in backend/ui/chat_logic.py — keep in sync when tools are added/removed there.
// An unset/empty allowlist means "unrestricted" (all tools), matching the
// AIAgent tool-permission model; a non-empty list is enforced server-side.
const ASSISTANT_TOOLS = [
  { name: 'search_graph', label: 'Search the graph' },
  { name: 'get_related_nodes', label: 'Get related nodes' },
  { name: 'find_similar_nodes', label: 'Find similar nodes' },
  { name: 'find_similar_nodes_batch', label: 'Find similar nodes (batch)' },
  { name: 'list_node_types', label: 'List node types' },
  { name: 'get_subtypes', label: 'Get subtypes' },
  { name: 'get_schema', label: 'Get schema' },
  { name: 'get_presentation', label: 'Get presentation config' },
  { name: 'add_nodes', label: 'Add nodes' },
  { name: 'propose_new_node', label: 'Propose a new node' },
  { name: 'update_node', label: 'Update a node' },
  { name: 'delete_nodes', label: 'Delete nodes' },
  { name: 'delete_edges', label: 'Delete edges' },
  { name: 'save_view', label: 'Save a view' },
  { name: 'get_saved_view', label: 'Load a saved view' },
  { name: 'list_saved_views', label: 'List saved views' },
  { name: 'clear_visualization', label: 'Clear the visualization' },
  { name: 'mark_nodes', label: 'Mark nodes' },
  { name: 'present_form', label: 'Present an input form' },
  { name: 'save_collection_response', label: 'Save a collection response' },
];

function filterExcludedPermissions(nodeTypePermissions = {}) {
  return Object.fromEntries(
    Object.entries(nodeTypePermissions).filter(([type]) => !EXCLUDED_TYPES.includes(type))
  );
}

/**
 * Dialog for creating/editing an ActiveKnowledgeCollection node.
 *
 * An ActiveKnowledgeCollection enables structured data gathering through
 * a special AI-assistant kiosk. It stores:
 *   - Basic info: name, short_name (slug), description
 *   - Collection config: introduction text, AI prompt
 *   - Node type permissions: per-type create/update/delete flags
 *
 * Two shareable URLs are generated:
 *   1. Kiosk URL (/collect/{shortName}) — focused AI chat, no graph UI
 *   2. Full App URL (/web/?akc={shortName}) — full graph with special AI
 */
export default function CreateActiveKnowledgeCollectionDialog({ onClose, onSave, initialData }) {
  const schema = useGraphStore((state) => state.schema);
  const { t } = useI18n();

  // Basic info
  const [name, setName] = useState('');
  const [shortName, setShortName] = useState('');
  const [description, setDescription] = useState('');
  const [aliases, setAliases] = useState('');

  // Collection configuration
  const [introductionText, setIntroductionText] = useState('');
  const [prompt, setPrompt] = useState('');
  // Link each submission's response node to every node created/updated in that run.
  const [linkResults, setLinkResults] = useState(true);

  // Node type permissions: { TypeName: { create: bool, update: bool, delete: bool } }
  const [nodeTypePermissions, setNodeTypePermissions] = useState({});

  // Tool access: when restrictTools is false the assistant is unrestricted (no
  // allowlist saved). When true, only the checked tools are saved as the
  // per-collection tool_allowlist and enforced server-side.
  const [restrictTools, setRestrictTools] = useState(false);
  const [allowedTools, setAllowedTools] = useState(() =>
    Object.fromEntries(ASSISTANT_TOOLS.map((t) => [t.name, true]))
  );

  // Copy feedback states
  const [copiedKiosk, setCopiedKiosk] = useState(false);
  const [copiedFull, setCopiedFull] = useState(false);

  // All domain node types from schema (excluding system types listed above)
  const nodeTypes = schema?.node_types
    ? Object.keys(schema.node_types).filter((t) => !EXCLUDED_TYPES.includes(t))
    : [];

  // Initialize form with initialData if provided (edit mode)
  useEffect(() => {
    if (initialData?.node) {
      const node = initialData.node;
      setName(node.name || '');
      setDescription(node.description || '');
      setAliases((node.aliases || []).join(', '));

      const meta = node.metadata || {};
      setShortName(meta.short_name || '');
      setIntroductionText(meta.introduction_text || '');
      setPrompt(meta.prompt || '');
      setLinkResults(meta.link_results !== false);

      if (meta.node_type_permissions) {
        setNodeTypePermissions(filterExcludedPermissions(meta.node_type_permissions));
      }

      // A stored tool_allowlist (non-empty array) means the assistant is
      // restricted to those tools; anything else means unrestricted.
      if (Array.isArray(meta.tool_allowlist) && meta.tool_allowlist.length > 0) {
        const allowed = new Set(meta.tool_allowlist);
        setRestrictTools(true);
        setAllowedTools(
          Object.fromEntries(ASSISTANT_TOOLS.map((t) => [t.name, allowed.has(t.name)]))
        );
      } else {
        setRestrictTools(false);
      }
    }
  }, [initialData]);

  // Initialise default permissions when schema loads and we have node types
  useEffect(() => {
    if (nodeTypes.length > 0 && Object.keys(nodeTypePermissions).length === 0 && !initialData) {
      const defaults = {};
      nodeTypes.forEach((type) => {
        defaults[type] = { create: true, update: true, delete: false };
      });
      setNodeTypePermissions(defaults);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeTypes.length, initialData]);

  // Auto-generate a slug from the name (only when creating, not editing)
  const handleNameChange = (value) => {
    setName(value);
    if (!initialData) {
      const slug = value
        .toLowerCase()
        .replace(/\s+/g, '-')
        .replace(/[^a-z0-9-]/g, '')
        .replace(/-+/g, '-')
        .replace(/^-|-$/g, '');
      setShortName(slug);
    }
  };

  const handlePermissionChange = (type, field, value) => {
    setNodeTypePermissions((prev) => ({
      ...prev,
      [type]: {
        ...(prev[type] || { create: true, update: true, delete: false }),
        [field]: value,
      },
    }));
  };

  const handleToolToggle = (toolName, value) => {
    setAllowedTools((prev) => ({ ...prev, [toolName]: value }));
  };

  const isShortNameValid = (value) => /^[a-z0-9]([a-z0-9-]{0,98}[a-z0-9])?$/.test(value);

  const kioskUrl = shortName ? `${window.location.origin}/collect/${shortName}` : '';
  const fullAppUrl = shortName ? `${window.location.origin}/web/?akc=${shortName}` : '';

  const copyToClipboard = useCallback(async (text, setCopied) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback for older browsers
      const el = document.createElement('textarea');
      el.value = text;
      document.body.appendChild(el);
      el.select();
      document.execCommand('copy');
      document.body.removeChild(el);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }, []);

  const handleSubmit = (e) => {
    e.preventDefault();

    if (!name.trim()) {
      alert(t('active_data_collection.alert_name_required'));
      return;
    }

    if (!shortName.trim()) {
      alert(t('active_data_collection.alert_short_name_required'));
      return;
    }

    if (!isShortNameValid(shortName.trim())) {
      alert(t('active_data_collection.alert_short_name_invalid'));
      return;
    }

    // Guard the "empty ⇒ unrestricted" model against a misleading UI outcome:
    // an enabled restriction with nothing checked would save an empty allowlist,
    // which the backend treats as "all tools". That inverts the admin's intent,
    // so require at least one tool or turning the restriction off.
    const selectedTools = ASSISTANT_TOOLS.filter((t) => allowedTools[t.name]).map((t) => t.name);
    if (restrictTools && selectedTools.length === 0) {
      alert(t('active_data_collection.alert_no_tools'));
      return;
    }

    const nodeObject = {
      name: name.trim(),
      type: 'ActiveKnowledgeCollection',
      description: description.trim(),
      summary: `Knowledge collection: ${name.trim()}`,
      tags: [],
      aliases: aliases
        .split(',')
        .map((a) => a.trim())
        .filter(Boolean),
      metadata: {
        short_name: shortName.trim(),
        introduction_text: introductionText.trim(),
        prompt: prompt.trim(),
        link_results: linkResults,
        node_type_permissions: filterExcludedPermissions(nodeTypePermissions),
        // Empty array = unrestricted (all tools). The backend normalizes a
        // falsy/empty allowlist to "no restriction", so turning restriction off
        // reliably clears any previously stored allowlist on update. When
        // restriction is on, selectedTools is guaranteed non-empty (see guard above).
        tool_allowlist: restrictTools ? selectedTools : [],
      },
    };

    if (initialData?.node?.id) {
      nodeObject.id = initialData.node.id;
    }

    onSave(nodeObject);
    onClose();
  };

  const shortNameInvalid = shortName.length > 0 && !isShortNameValid(shortName);

  return (
    <div className="dialog-overlay" onClick={onClose}>
      <div
        className="dialog-content subscription-dialog"
        style={{ maxWidth: '680px' }}
        onClick={(e) => e.stopPropagation()}
      >
        <h2>
          {initialData
            ? t('active_data_collection.title_edit')
            : t('active_data_collection.title_create')}
        </h2>
        <p className="dialog-description">{t('active_data_collection.dialog_description')}</p>

        <form onSubmit={handleSubmit}>
          {/* ── Section 1: Basic Info ─────────────────────────────── */}
          <div className="form-section">
            <h3>{t('active_data_collection.section_basic')}</h3>

            <div className="form-group">
              <label htmlFor="akc-name">{t('active_data_collection.name_label')} *</label>
              <input
                id="akc-name"
                type="text"
                value={name}
                onChange={(e) => handleNameChange(e.target.value)}
                placeholder={t('active_data_collection.name_placeholder')}
                required
              />
            </div>

            <div className="form-group">
              <label htmlFor="akc-short-name">
                {t('active_data_collection.short_name_label')} *{' '}
                <span style={{ color: '#888', fontWeight: 'normal', fontSize: '0.8rem' }}>
                  {t('active_data_collection.short_name_note')}
                </span>
              </label>
              <input
                id="akc-short-name"
                type="text"
                value={shortName}
                onChange={(e) =>
                  setShortName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))
                }
                placeholder={t('active_data_collection.short_name_placeholder')}
                required
                style={shortNameInvalid ? { borderColor: '#EF4444' } : {}}
              />
              {shortNameInvalid && (
                <small style={{ color: '#EF4444' }}>
                  {t('active_data_collection.short_name_invalid')}
                </small>
              )}
              {!shortNameInvalid && <small>{t('active_data_collection.short_name_help')}</small>}
            </div>

            <div className="form-group">
              <label htmlFor="akc-description">
                {t('active_data_collection.description_label')}
              </label>
              <textarea
                id="akc-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder={t('active_data_collection.description_placeholder')}
                rows={2}
              />
            </div>

            <div className="form-group">
              <label htmlFor="akc-aliases">{t('active_data_collection.aliases_label')}</label>
              <input
                id="akc-aliases"
                type="text"
                value={aliases}
                onChange={(e) => setAliases(e.target.value)}
                placeholder={t('active_data_collection.aliases_placeholder')}
              />
            </div>
          </div>

          {/* ── Section 2: Collection Configuration ──────────────── */}
          <div className="form-section">
            <h3>{t('active_data_collection.section_config')}</h3>

            <div className="form-group">
              <label htmlFor="akc-intro">{t('active_data_collection.intro_label')}</label>
              <textarea
                id="akc-intro"
                value={introductionText}
                onChange={(e) => setIntroductionText(e.target.value)}
                placeholder={t('active_data_collection.intro_placeholder')}
                rows={4}
              />
              <small>{t('active_data_collection.intro_help')}</small>
            </div>

            <div className="form-group">
              <label htmlFor="akc-prompt">{t('active_data_collection.prompt_label')}</label>
              <textarea
                id="akc-prompt"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder={t('active_data_collection.prompt_placeholder')}
                rows={8}
                style={{ fontFamily: 'monospace', fontSize: '0.85rem' }}
              />
              <small>{t('active_data_collection.prompt_help')}</small>
            </div>

            <div className="form-group">
              <label
                style={{
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: '0.5rem',
                  cursor: 'pointer',
                }}
              >
                <input
                  type="checkbox"
                  checked={linkResults}
                  onChange={(e) => setLinkResults(e.target.checked)}
                  style={{
                    accentColor: '#646cff',
                    width: '1rem',
                    height: '1rem',
                    marginTop: '0.15rem',
                    cursor: 'pointer',
                  }}
                />
                <span>{t('active_data_collection.link_results_label')}</span>
              </label>
              <small>{t('active_data_collection.link_results_help')}</small>
            </div>
          </div>

          {/* ── Section 3: Node Type Permissions ─────────────────── */}
          <div className="form-section">
            <h3>{t('active_data_collection.section_permissions')}</h3>
            <p style={{ margin: '0 0 0.75rem 0', color: '#888', fontSize: '0.85rem' }}>
              {t('active_data_collection.permissions_help')}
            </p>

            {nodeTypes.length === 0 ? (
              <p style={{ color: '#888' }}>{t('active_data_collection.loading_node_types')}</p>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table
                  style={{
                    width: '100%',
                    borderCollapse: 'collapse',
                    fontSize: '0.85rem',
                    color: '#ddd',
                  }}
                >
                  <thead>
                    <tr style={{ borderBottom: '1px solid #444' }}>
                      <th style={{ textAlign: 'left', padding: '0.4rem 0.5rem', color: '#aaa' }}>
                        {t('active_data_collection.col_node_type')}
                      </th>
                      <th style={{ textAlign: 'center', padding: '0.4rem 0.5rem', color: '#aaa' }}>
                        {t('active_data_collection.col_can_create')}
                      </th>
                      <th style={{ textAlign: 'center', padding: '0.4rem 0.5rem', color: '#aaa' }}>
                        {t('active_data_collection.col_can_update')}
                      </th>
                      <th style={{ textAlign: 'center', padding: '0.4rem 0.5rem', color: '#aaa' }}>
                        {t('active_data_collection.col_can_delete')}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {nodeTypes.map((type) => {
                      const perms = nodeTypePermissions[type] || {
                        create: true,
                        update: true,
                        delete: false,
                      };
                      return (
                        <tr key={type} style={{ borderBottom: '1px solid #333' }}>
                          <td style={{ padding: '0.35rem 0.5rem' }}>{type}</td>
                          {['create', 'update', 'delete'].map((op) => (
                            <td key={op} style={{ textAlign: 'center', padding: '0.35rem 0.5rem' }}>
                              <input
                                type="checkbox"
                                checked={!!perms[op]}
                                onChange={(e) => handlePermissionChange(type, op, e.target.checked)}
                                style={{
                                  accentColor: '#646cff',
                                  width: '1rem',
                                  height: '1rem',
                                  cursor: 'pointer',
                                }}
                              />
                            </td>
                          ))}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* ── Section 4: Assistant Tools ───────────────────────── */}
          <div className="form-section">
            <h3>{t('active_data_collection.section_tools')}</h3>
            <p style={{ margin: '0 0 0.75rem 0', color: '#888', fontSize: '0.85rem' }}>
              {t('active_data_collection.tools_help')}
            </p>

            <label
              style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}
            >
              <input
                type="checkbox"
                checked={restrictTools}
                onChange={(e) => setRestrictTools(e.target.checked)}
                style={{ accentColor: '#646cff', width: '1rem', height: '1rem', cursor: 'pointer' }}
              />
              <span style={{ color: '#ddd', fontSize: '0.9rem' }}>
                {t('active_data_collection.restrict_tools_label')}
              </span>
            </label>

            {restrictTools && (
              <div
                style={{
                  marginTop: '0.75rem',
                  display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))',
                  gap: '0.3rem 1rem',
                }}
              >
                {ASSISTANT_TOOLS.map((tool) => (
                  <label
                    key={tool.name}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '0.5rem',
                      fontSize: '0.85rem',
                      color: '#ddd',
                      cursor: 'pointer',
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={!!allowedTools[tool.name]}
                      onChange={(e) => handleToolToggle(tool.name, e.target.checked)}
                      style={{
                        accentColor: '#646cff',
                        width: '1rem',
                        height: '1rem',
                        cursor: 'pointer',
                      }}
                    />
                    <span>
                      {t(`active_data_collection.tools.${tool.name}`, undefined, tool.label)}
                    </span>
                  </label>
                ))}
              </div>
            )}
          </div>

          {/* ── Section 5: Shareable URLs ─────────────────────────── */}
          <div className="form-section" style={{ borderBottom: 'none' }}>
            <h3>{t('active_data_collection.section_urls')}</h3>

            {/* Kiosk URL */}
            <div style={{ marginBottom: '1rem' }}>
              <p style={{ margin: '0 0 0.4rem 0', color: '#aaa', fontSize: '0.85rem' }}>
                <strong style={{ color: '#ddd' }}>
                  {t('active_data_collection.kiosk_url_label')}
                </strong>{' '}
                {t('active_data_collection.kiosk_url_help')}
              </p>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '0.5rem',
                  background: '#1a1a1a',
                  border: `1px solid ${shortName ? '#444' : '#333'}`,
                  borderRadius: '6px',
                  padding: '0.5rem 0.75rem',
                  opacity: shortName ? 1 : 0.5,
                }}
              >
                <span
                  style={{
                    flex: 1,
                    fontFamily: 'monospace',
                    fontSize: '0.82rem',
                    color: shortName ? '#ccc' : '#666',
                    wordBreak: 'break-all',
                  }}
                >
                  {kioskUrl || `${window.location.origin}/collect/…`}
                </span>
                <button
                  type="button"
                  onClick={() => kioskUrl && copyToClipboard(kioskUrl, setCopiedKiosk)}
                  disabled={!shortName}
                  style={{
                    background: copiedKiosk ? '#10B981' : '#333',
                    color: '#fff',
                    border: 'none',
                    borderRadius: '4px',
                    padding: '0.3rem 0.6rem',
                    cursor: shortName ? 'pointer' : 'not-allowed',
                    fontSize: '0.8rem',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '0.3rem',
                    transition: 'background 0.2s',
                    whiteSpace: 'nowrap',
                  }}
                  title={t('active_data_collection.copy_kiosk_title')}
                >
                  <ClipboardFill size={12} />
                  {copiedKiosk
                    ? t('active_data_collection.copied')
                    : t('active_data_collection.copy')}
                </button>
              </div>
            </div>

            {/* Full App URL */}
            <div>
              <p style={{ margin: '0 0 0.4rem 0', color: '#aaa', fontSize: '0.85rem' }}>
                <strong style={{ color: '#ddd' }}>
                  {t('active_data_collection.full_url_label')}
                </strong>{' '}
                {t('active_data_collection.full_url_help')}
              </p>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '0.5rem',
                  background: '#1a1a1a',
                  border: `1px solid ${shortName ? '#444' : '#333'}`,
                  borderRadius: '6px',
                  padding: '0.5rem 0.75rem',
                  opacity: shortName ? 1 : 0.5,
                }}
              >
                <span
                  style={{
                    flex: 1,
                    fontFamily: 'monospace',
                    fontSize: '0.82rem',
                    color: shortName ? '#ccc' : '#666',
                    wordBreak: 'break-all',
                  }}
                >
                  {fullAppUrl || `${window.location.origin}/web/?akc=…`}
                </span>
                <button
                  type="button"
                  onClick={() => fullAppUrl && copyToClipboard(fullAppUrl, setCopiedFull)}
                  disabled={!shortName}
                  style={{
                    background: copiedFull ? '#10B981' : '#333',
                    color: '#fff',
                    border: 'none',
                    borderRadius: '4px',
                    padding: '0.3rem 0.6rem',
                    cursor: shortName ? 'pointer' : 'not-allowed',
                    fontSize: '0.8rem',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '0.3rem',
                    transition: 'background 0.2s',
                    whiteSpace: 'nowrap',
                  }}
                  title={t('active_data_collection.copy_full_title')}
                >
                  <ClipboardFill size={12} />
                  {copiedFull
                    ? t('active_data_collection.copied')
                    : t('active_data_collection.copy')}
                </button>
              </div>
            </div>
          </div>

          <div className="dialog-actions">
            <button type="button" className="btn-secondary" onClick={onClose}>
              {t('common.cancel')}
            </button>
            <button type="submit" className="btn-primary">
              {initialData
                ? t('active_data_collection.save_changes')
                : t('active_data_collection.create_collection')}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
