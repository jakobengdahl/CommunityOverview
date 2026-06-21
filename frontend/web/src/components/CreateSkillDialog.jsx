import { useState, useEffect } from 'react';
import './CreateSubscriptionDialog.css';

export default function CreateSkillDialog({ nodeType = 'Skill', onClose, onSave, initialData }) {
  const isEditing = !!initialData;

  const [name, setName]             = useState('');
  const [description, setDescription] = useState('');
  const [whenToUse, setWhenToUse]   = useState('');
  const [content, setContent]       = useState('');
  const [sourceUrl, setSourceUrl]   = useState('');
  const [allowedTools, setAllowedTools] = useState('');
  const [version, setVersion]       = useState('');
  const [effort, setEffort]         = useState('');

  useEffect(() => {
    if (!initialData) return;
    setName(initialData.name || '');
    setDescription(initialData.description || '');
    const m = initialData.metadata || {};
    setWhenToUse(m.when_to_use || '');
    setContent(m.content || '');
    setSourceUrl(m.source_url || '');
    setAllowedTools((m.allowed_tools || []).join(', '));
    setVersion(m.version || '');
    setEffort(m.effort || '');
  }, [initialData]);

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!name.trim()) return;

    const metadata = {
      ...(initialData?.metadata || {}),
      when_to_use: whenToUse.trim(),
      content: content.trim(),
      source_url: sourceUrl.trim(),
      allowed_tools: allowedTools.split(',').map(t => t.trim()).filter(Boolean),
      version: version.trim(),
      effort: effort.trim(),
    };

    if (isEditing) {
      onSave({
        id: initialData.id,
        updates: {
          name: name.trim(),
          description: description.trim(),
          summary: whenToUse.trim().slice(0, 120) || name.trim(),
          metadata,
        },
      });
    } else {
      onSave({
        name: name.trim(),
        type: nodeType,
        description: description.trim(),
        summary: whenToUse.trim().slice(0, 120) || name.trim(),
        metadata,
        communities: [],
      });
    }
    onClose();
  };

  return (
    <div className="dialog-overlay" onClick={onClose}>
      <div className="dialog-content subscription-dialog" onClick={e => e.stopPropagation()}>
        <h2>{isEditing ? `Edit ${nodeType}` : `Create ${nodeType}`}</h2>
        <p className="dialog-description">
          Defines reusable instructions for an AI agent. Compatible with the SKILL.md format.
        </p>

        <form onSubmit={handleSubmit}>
          <div className="form-section">
            <h3>Basic information</h3>

            <div className="form-group">
              <label htmlFor="skill-name">Name *</label>
              <input
                id="skill-name"
                type="text"
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder="E.g. 'Graph analysis'"
                required
              />
            </div>

            <div className="form-group">
              <label htmlFor="skill-description">Description</label>
              <textarea
                id="skill-description"
                value={description}
                onChange={e => setDescription(e.target.value)}
                placeholder="Short description of what this skill does"
                rows={2}
              />
            </div>

            <div className="form-group">
              <label htmlFor="skill-when">When to use</label>
              <textarea
                id="skill-when"
                value={whenToUse}
                onChange={e => setWhenToUse(e.target.value)}
                placeholder="Describe the situations where the agent should apply this skill"
                rows={3}
              />
            </div>
          </div>

          <div className="form-section">
            <h3>Skill instructions</h3>

            <div className="form-group">
              <label htmlFor="skill-content">Content (SKILL.md)</label>
              <textarea
                id="skill-content"
                value={content}
                onChange={e => setContent(e.target.value)}
                placeholder={`## Steps\n1. ...\n2. ...\n\n## Output format\n...`}
                rows={10}
                style={{ fontFamily: 'monospace', fontSize: '0.85rem' }}
              />
              <small>Markdown instructions for the agent. Follows SKILL.md format.</small>
            </div>
          </div>

          <div className="form-section">
            <h3>Metadata</h3>

            <div className="form-group">
              <label htmlFor="skill-source-url">Source URL</label>
              <input
                id="skill-source-url"
                type="text"
                value={sourceUrl}
                onChange={e => setSourceUrl(e.target.value)}
                placeholder="https://github.com/org/repo/blob/main/SKILL.md"
              />
              <small>Optional: URL to the original SKILL.md file or GitHub repo.</small>
            </div>

            <div className="form-group">
              <label htmlFor="skill-tools">Allowed tools (comma-separated)</label>
              <input
                id="skill-tools"
                type="text"
                value={allowedTools}
                onChange={e => setAllowedTools(e.target.value)}
                placeholder="search_graph, get_related_nodes, add_nodes"
              />
            </div>

            <div style={{ display: 'flex', gap: '1rem' }}>
              <div className="form-group" style={{ flex: 1 }}>
                <label htmlFor="skill-version">Version</label>
                <input
                  id="skill-version"
                  type="text"
                  value={version}
                  onChange={e => setVersion(e.target.value)}
                  placeholder="1.0.0"
                />
              </div>
              <div className="form-group" style={{ flex: 1 }}>
                <label htmlFor="skill-effort">Effort</label>
                <select
                  id="skill-effort"
                  value={effort}
                  onChange={e => setEffort(e.target.value)}
                  style={{ width: '100%', padding: '0.4rem 0.5rem', borderRadius: '4px', border: '1px solid #444', background: '#2a2a2a', color: 'inherit' }}
                >
                  <option value="">— unset —</option>
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                </select>
              </div>
            </div>
          </div>

          <div className="dialog-actions">
            <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn-primary">
              {isEditing ? 'Save changes' : `Create ${nodeType}`}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
