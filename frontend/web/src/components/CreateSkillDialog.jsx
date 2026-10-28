import { useState, useEffect } from 'react';
import { useI18n } from '../i18n';
import './CreateSubscriptionDialog.css';

export default function CreateSkillDialog({ nodeType = 'Skill', onClose, onSave, initialData }) {
  const { t } = useI18n();
  const isEditing = !!initialData;

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [aliases, setAliases] = useState('');
  const [whenToUse, setWhenToUse] = useState('');
  const [content, setContent] = useState('');
  const [sourceUrl, setSourceUrl] = useState('');
  const [allowedTools, setAllowedTools] = useState('');
  const [version, setVersion] = useState('');
  const [effort, setEffort] = useState('');

  useEffect(() => {
    if (!initialData) return;
    setName(initialData.name || '');
    setDescription(initialData.description || '');
    setAliases((initialData.aliases || []).join(', '));
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

    const aliasList = aliases
      .split(',')
      .map((a) => a.trim())
      .filter(Boolean);

    const metadata = {
      ...(initialData?.metadata || {}),
      when_to_use: whenToUse.trim(),
      content: content.trim(),
      source_url: sourceUrl.trim(),
      allowed_tools: allowedTools
        .split(',')
        .map((tool) => tool.trim())
        .filter(Boolean),
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
          aliases: aliasList,
          metadata,
        },
      });
    } else {
      onSave({
        name: name.trim(),
        type: nodeType,
        description: description.trim(),
        summary: whenToUse.trim().slice(0, 120) || name.trim(),
        aliases: aliasList,
        metadata,
        communities: [],
      });
    }
    onClose();
  };

  return (
    <div className="dialog-overlay" onClick={onClose}>
      <div
        className="dialog-content subscription-dialog"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-skill-dialog-title"
      >
        <h2 id="create-skill-dialog-title">
          {isEditing
            ? t('skill_dialog.title_edit', { type: nodeType })
            : t('skill_dialog.title', { type: nodeType })}
        </h2>
        <p className="dialog-description">{t('skill_dialog.description')}</p>

        <form onSubmit={handleSubmit}>
          <div className="form-section">
            <h3>{t('skill_dialog.section_basic')}</h3>

            <div className="form-group">
              <label htmlFor="skill-name">{t('skill_dialog.name_label')}</label>
              <input
                id="skill-name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={t('skill_dialog.name_placeholder')}
                required
              />
            </div>

            <div className="form-group">
              <label htmlFor="skill-description">{t('skill_dialog.description_label')}</label>
              <textarea
                id="skill-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder={t('skill_dialog.description_placeholder')}
                rows={2}
              />
            </div>

            <div className="form-group">
              <label htmlFor="skill-aliases">{t('skill_dialog.aliases_label')}</label>
              <input
                id="skill-aliases"
                type="text"
                value={aliases}
                onChange={(e) => setAliases(e.target.value)}
                placeholder={t('skill_dialog.aliases_placeholder')}
              />
            </div>

            <div className="form-group">
              <label htmlFor="skill-when">{t('skill_dialog.when_label')}</label>
              <textarea
                id="skill-when"
                value={whenToUse}
                onChange={(e) => setWhenToUse(e.target.value)}
                placeholder={t('skill_dialog.when_placeholder')}
                rows={3}
              />
            </div>
          </div>

          <div className="form-section">
            <h3>{t('skill_dialog.section_instructions')}</h3>

            <div className="form-group">
              <label htmlFor="skill-content">{t('skill_dialog.content_label')}</label>
              <textarea
                id="skill-content"
                value={content}
                onChange={(e) => setContent(e.target.value)}
                placeholder={t('skill_dialog.content_placeholder')}
                rows={10}
                style={{ fontFamily: 'monospace', fontSize: '0.85rem' }}
              />
              <small>{t('skill_dialog.content_help')}</small>
            </div>
          </div>

          <div className="form-section">
            <h3>{t('skill_dialog.section_metadata')}</h3>

            <div className="form-group">
              <label htmlFor="skill-source-url">{t('skill_dialog.source_url_label')}</label>
              <input
                id="skill-source-url"
                type="text"
                value={sourceUrl}
                onChange={(e) => setSourceUrl(e.target.value)}
                placeholder="https://github.com/org/repo/blob/main/SKILL.md"
              />
              <small>{t('skill_dialog.source_url_help')}</small>
            </div>

            <div className="form-group">
              <label htmlFor="skill-tools">{t('skill_dialog.allowed_tools_label')}</label>
              <input
                id="skill-tools"
                type="text"
                value={allowedTools}
                onChange={(e) => setAllowedTools(e.target.value)}
                placeholder="search_graph, get_related_nodes, add_nodes"
              />
            </div>

            <div style={{ display: 'flex', gap: '1rem' }}>
              <div className="form-group" style={{ flex: 1 }}>
                <label htmlFor="skill-version">{t('skill_dialog.version_label')}</label>
                <input
                  id="skill-version"
                  type="text"
                  value={version}
                  onChange={(e) => setVersion(e.target.value)}
                  placeholder="1.0.0"
                />
              </div>
              <div className="form-group" style={{ flex: 1 }}>
                <label htmlFor="skill-effort">{t('skill_dialog.effort_label')}</label>
                <select
                  id="skill-effort"
                  value={effort}
                  onChange={(e) => setEffort(e.target.value)}
                  style={{
                    width: '100%',
                    padding: '0.4rem 0.5rem',
                    borderRadius: '4px',
                    border: '1px solid #444',
                    background: '#2a2a2a',
                    color: 'inherit',
                  }}
                >
                  <option value="">{t('skill_dialog.effort_unset')}</option>
                  <option value="low">{t('skill_dialog.effort_low')}</option>
                  <option value="medium">{t('skill_dialog.effort_medium')}</option>
                  <option value="high">{t('skill_dialog.effort_high')}</option>
                </select>
              </div>
            </div>
          </div>

          <div className="dialog-actions">
            <button type="button" className="btn-secondary" onClick={onClose}>
              {t('skill_dialog.cancel')}
            </button>
            <button type="submit" className="btn-primary">
              {isEditing
                ? t('skill_dialog.submit_edit')
                : t('skill_dialog.submit', { type: nodeType })}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
