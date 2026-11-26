import { useEffect, useRef, useState } from 'react';
import { useI18n } from '../i18n';
import * as api from '../services/api';
import './InputDialog.css';

const errorCodes = new Set([
  'access_denied',
  'staged_changes',
  'nodes_limit',
  'edges_limit',
  'annotations_limit',
  'revision_conflict',
]);

export function templateSummary(source, selected, schema) {
  if (!schema?.node_types) return null;
  const whole = selected.length === 0;
  const chosenNodes = new Set(selected.filter((item) => !item.annotation).map((item) => item.id));
  const chosenAnnotations = new Set(
    selected.filter((item) => item.annotation).map((item) => item.id)
  );
  const nodes = (source.resolved?.nodes || []).filter(
    (node) =>
      (whole || chosenNodes.has(node.id)) && schema?.node_types?.[node.type]?.category !== 'system'
  );
  const ids = new Set(nodes.map((node) => node.id));
  const edges = (source.resolved?.edges || []).filter(
    (edge) => ids.has(edge.source) && ids.has(edge.target)
  );
  const annotations = (source.state?.annotations || []).filter((annotation) => {
    if (!whole && !chosenAnnotations.has(annotation.id)) return false;
    const merged = { ...annotation, ...annotation.content };
    const target = typeof merged.target === 'object' ? merged.target?.id : merged.target;
    return !(
      (annotation.type || annotation.kind) === 'reference' &&
      merged.target_kind === 'resource' &&
      target &&
      !ids.has(target)
    );
  });
  return { nodes: nodes.length, edges: edges.length, annotations: annotations.length };
}

export default function SaveTemplateDialog({
  sessionId,
  selected,
  schema,
  prepare,
  onClose,
  onSaved,
}) {
  const { t } = useI18n();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [tags, setTags] = useState('');
  const [source, setSource] = useState(null);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const prepareRef = useRef(prepare);
  useEffect(() => {
    let active = true;
    prepareRef
      .current()
      .then((value) => {
        if (active) setSource(value);
      })
      .catch((err) => {
        if (active) setError(errorCodes.has(err.code) ? err.code : 'other');
      });
    return () => {
      active = false;
    };
  }, []);

  async function submit(event) {
    event.preventDefault();
    if (!name.trim() || !summary || saving) return;
    setSaving(true);
    setError(null);
    const body = {
      name: name.trim(),
      description,
      tags: tags
        .split(',')
        .map((tag) => tag.trim())
        .filter(Boolean),
      expected_revision: source.seq,
    };
    if (selected.length) {
      body.node_ids = selected.filter((item) => !item.annotation).map((item) => item.id);
      body.annotation_ids = selected.filter((item) => item.annotation).map((item) => item.id);
    }
    try {
      const result = await api.saveTemplate(sessionId, body);
      const template = result.template;
      onSaved({
        nodes: template.nodes.length,
        edges: template.edges.length,
        annotations: template.annotation_document.annotations.length,
      });
    } catch (err) {
      setError(errorCodes.has(err.code) ? err.code : 'other');
      setSaving(false);
    }
  }
  const summary = source && templateSummary(source, selected, schema);
  return (
    <div className="input-dialog-overlay">
      <form
        className="input-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="template-title"
        onSubmit={submit}
        onKeyDown={(event) => {
          if (event.key === 'Escape' && !saving) onClose();
        }}
      >
        <div className="input-dialog-header">
          <h3 id="template-title">{t('template.save')}</h3>
        </div>
        <div className="input-dialog-content">
          <label className="input-dialog-label" htmlFor="template-name">
            {t('template.name')}
          </label>
          <input
            id="template-name"
            className="input-dialog-input"
            value={name}
            onChange={(event) => setName(event.target.value)}
            autoFocus
          />
          <label className="input-dialog-label" htmlFor="template-description">
            {t('template.description')}
          </label>
          <textarea
            id="template-description"
            className="input-dialog-input"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
          <label className="input-dialog-label" htmlFor="template-tags">
            {t('template.tags')}
          </label>
          <input
            id="template-tags"
            className="input-dialog-input"
            value={tags}
            onChange={(event) => setTags(event.target.value)}
          />
          <p>{summary ? t('template.summary', summary) : !error && t('template.loading')}</p>
          {error && <p role="alert">{t(`template.errors.${error}`)}</p>}
        </div>
        <div className="input-dialog-actions">
          <button
            type="button"
            className="input-dialog-button cancel"
            onClick={onClose}
            disabled={saving}
          >
            {t('common.cancel')}
          </button>
          <button
            type="submit"
            className="input-dialog-button primary"
            disabled={!name.trim() || !summary || saving}
          >
            {t(saving ? 'template.saving' : 'template.save_button')}
          </button>
        </div>
      </form>
    </div>
  );
}
