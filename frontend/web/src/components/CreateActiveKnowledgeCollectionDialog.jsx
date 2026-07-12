import { useState, useEffect, useCallback } from 'react';
import { ClipboardFill } from 'react-bootstrap-icons';
import useGraphStore from '../store/graphStore';
import './CreateSubscriptionDialog.css'; // Reuse the same styles

const EXCLUDED_TYPES = [
  'SavedView',
  'VisualizationView',
  'EventSubscription',
  'Agent',
  'ActiveKnowledgeCollection',
  'CollectionResponse',
];

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

  // Basic info
  const [name, setName] = useState('');
  const [shortName, setShortName] = useState('');
  const [description, setDescription] = useState('');
  const [aliases, setAliases] = useState('');

  // Collection configuration
  const [introductionText, setIntroductionText] = useState('');
  const [prompt, setPrompt] = useState('');

  // Node type permissions: { TypeName: { create: bool, update: bool, delete: bool } }
  const [nodeTypePermissions, setNodeTypePermissions] = useState({});

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

      if (meta.node_type_permissions) {
        setNodeTypePermissions(meta.node_type_permissions);
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
      alert('Name is required');
      return;
    }

    if (!shortName.trim()) {
      alert('Short name is required');
      return;
    }

    if (!isShortNameValid(shortName.trim())) {
      alert(
        'Short name must only contain lowercase letters, numbers, and hyphens (e.g. my-collection)'
      );
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
        node_type_permissions: nodeTypePermissions,
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
        <h2>{initialData ? 'Edit Knowledge Collection' : 'Create Knowledge Collection'}</h2>
        <p className="dialog-description">
          An Active Knowledge Collection lets you set up a structured data-gathering session with a
          special AI assistant available via a dedicated kiosk link.
        </p>

        <form onSubmit={handleSubmit}>
          {/* ── Section 1: Basic Info ─────────────────────────────── */}
          <div className="form-section">
            <h3>Basic Information</h3>

            <div className="form-group">
              <label htmlFor="akc-name">Name *</label>
              <input
                id="akc-name"
                type="text"
                value={name}
                onChange={(e) => handleNameChange(e.target.value)}
                placeholder="e.g. 'Q1 Partner Feedback'"
                required
              />
            </div>

            <div className="form-group">
              <label htmlFor="akc-short-name">
                Short Name *{' '}
                <span style={{ color: '#888', fontWeight: 'normal', fontSize: '0.8rem' }}>
                  (URL identifier — must be unique)
                </span>
              </label>
              <input
                id="akc-short-name"
                type="text"
                value={shortName}
                onChange={(e) =>
                  setShortName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))
                }
                placeholder="e.g. q1-partner-feedback"
                required
                style={shortNameInvalid ? { borderColor: '#EF4444' } : {}}
              />
              {shortNameInvalid && (
                <small style={{ color: '#EF4444' }}>
                  Only lowercase letters, numbers, and hyphens are allowed.
                </small>
              )}
              {!shortNameInvalid && (
                <small>Used as a URL identifier. Must be unique across all collections.</small>
              )}
            </div>

            <div className="form-group">
              <label htmlFor="akc-description">Description</label>
              <textarea
                id="akc-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Describe the purpose of this knowledge collection"
                rows={2}
              />
            </div>

            <div className="form-group">
              <label htmlFor="akc-aliases">Aliases / synonyms (comma-separated)</label>
              <input
                id="akc-aliases"
                type="text"
                value={aliases}
                onChange={(e) => setAliases(e.target.value)}
                placeholder="alternative name, abbreviation, synonym"
              />
            </div>
          </div>

          {/* ── Section 2: Collection Configuration ──────────────── */}
          <div className="form-section">
            <h3>Collection Configuration</h3>

            <div className="form-group">
              <label htmlFor="akc-intro">Introduction Text</label>
              <textarea
                id="akc-intro"
                value={introductionText}
                onChange={(e) => setIntroductionText(e.target.value)}
                placeholder="Write the text shown to users when they open the collection link. Explain who you are, what information you are gathering, and why."
                rows={4}
              />
              <small>
                Shown to users when they open the collection link (before the chat starts).
              </small>
            </div>

            <div className="form-group">
              <label htmlFor="akc-prompt">Prompt for AI Collector Assistant</label>
              <textarea
                id="akc-prompt"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder={`Describe how the AI assistant should guide the data collection. Example:

You are collecting information about digital initiatives from government agencies.
Ask the user about:
1. Their organization name and role
2. Current digital projects they are involved in
3. Challenges they are facing
4. Resources or tools they use

Add each item to the knowledge graph as appropriate.`}
                rows={8}
                style={{ fontFamily: 'monospace', fontSize: '0.85rem' }}
              />
              <small>
                The AI assistant will use this as its special instructions for guiding data
                collection. The standard graph assistant prompt is automatically appended.
              </small>
            </div>
          </div>

          {/* ── Section 3: Node Type Permissions ─────────────────── */}
          <div className="form-section">
            <h3>Node Type Permissions</h3>
            <p style={{ margin: '0 0 0.75rem 0', color: '#888', fontSize: '0.85rem' }}>
              Control which operations the AI assistant is permitted to perform for each node type.
            </p>

            {nodeTypes.length === 0 ? (
              <p style={{ color: '#888' }}>Loading node types…</p>
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
                        Node Type
                      </th>
                      <th style={{ textAlign: 'center', padding: '0.4rem 0.5rem', color: '#aaa' }}>
                        Can Create
                      </th>
                      <th style={{ textAlign: 'center', padding: '0.4rem 0.5rem', color: '#aaa' }}>
                        Can Update
                      </th>
                      <th style={{ textAlign: 'center', padding: '0.4rem 0.5rem', color: '#aaa' }}>
                        Can Delete
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

          {/* ── Section 4: Shareable URLs ─────────────────────────── */}
          <div className="form-section" style={{ borderBottom: 'none' }}>
            <h3>Shareable URLs</h3>

            {/* Kiosk URL */}
            <div style={{ marginBottom: '1rem' }}>
              <p style={{ margin: '0 0 0.4rem 0', color: '#aaa', fontSize: '0.85rem' }}>
                <strong style={{ color: '#ddd' }}>Kiosk Collection URL</strong> — Send this to
                people you want to gather knowledge from. They will see a focused AI assistant
                without the full graph interface.
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
                  title="Copy kiosk URL"
                >
                  <ClipboardFill size={12} />
                  {copiedKiosk ? 'Copied!' : 'Copy'}
                </button>
              </div>
            </div>

            {/* Full App URL */}
            <div>
              <p style={{ margin: '0 0 0.4rem 0', color: '#aaa', fontSize: '0.85rem' }}>
                <strong style={{ color: '#ddd' }}>Full App Collection URL</strong> — Send this to
                allow full graph access with the special collection assistant pre-loaded.
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
                  title="Copy full app URL"
                >
                  <ClipboardFill size={12} />
                  {copiedFull ? 'Copied!' : 'Copy'}
                </button>
              </div>
            </div>
          </div>

          <div className="dialog-actions">
            <button type="button" className="btn-secondary" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn-primary">
              {initialData ? 'Save Changes' : 'Create Collection'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
