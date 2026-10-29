import { useEffect, useRef, useState } from 'react';
import { useI18n } from '../i18n';
import {
  deleteNodeAttachment,
  getNodeAttachmentUrl,
  listNodeAttachments,
  uploadNodeAttachment,
} from '../services/api';
import './NodeAttachmentsPanel.css';

// Error codes the attachment routes answer with (backend/service/attachments.py)
// that have their own message; anything else falls back to a generic one.
const KNOWN_ERRORS = new Set([
  'attachment_too_large',
  'node_attachment_limit',
  'unsupported_attachment_type',
  'attachments_not_allowed',
  'node_archived',
  'federated_node',
  'attachments_unavailable',
]);

const FEDERATED_PREFIX = 'federated::';

// Set once a list request is answered 503 attachments_unavailable, so later
// dialogs on the same server skip the request (and its console error). Nothing
// can be stored there, so the "already has files" gate of ADR 0008 sec 12 has
// nothing to find; a server that gains storage is picked up on page reload.
let attachmentsKnownUnavailable = false;

/** Forget the known-unavailable signal (tests). */
export function resetAttachmentsAvailability() {
  attachmentsKnownUnavailable = false;
}

function formatBytes(bytes, language) {
  const steps = [
    ['byte', 1],
    ['kilobyte', 1024],
    ['megabyte', 1024 * 1024],
    ['gigabyte', 1024 * 1024 * 1024],
  ];
  let [unit, size] = steps[0];
  for (const step of steps) {
    if (bytes >= step[1]) [unit, size] = step;
  }
  return new Intl.NumberFormat(language, {
    style: 'unit',
    unit,
    unitDisplay: 'short',
    maximumFractionDigits: 1,
  }).format(bytes / size);
}

/**
 * The error code the server would answer an upload of `file` with, or null.
 * Mirrors add_node_attachment: the file count first, then the smaller of the
 * per-file limit and the node's remaining bytes, a tie reading as per-file.
 */
export function precheckUpload(file, attachments, limits) {
  if (!limits) return null;
  if (attachments.length >= limits.max_files_per_node) return 'node_attachment_limit';
  const used = attachments.reduce((sum, a) => sum + (a.size || 0), 0);
  const remaining = Math.max(limits.max_bytes_per_node - used, 0);
  const capIsFileLimit = limits.max_file_bytes <= remaining;
  const cap = capIsFileLimit ? limits.max_file_bytes : remaining;
  if (file.size > cap) return capIsFileLimit ? 'attachment_too_large' : 'node_attachment_limit';
  return null;
}

function NodeAttachmentsPanel({ nodeId, typeAllowsAttachments }) {
  const { t, language } = useI18n();
  const federated = typeof nodeId === 'string' && nodeId.startsWith(FEDERATED_PREFIX);
  const [listing, setListing] = useState(null);
  // Error code of a failed list ('' when the server gave none), else null.
  const [loadError, setLoadError] = useState(() =>
    attachmentsKnownUnavailable ? 'attachments_unavailable' : null
  );
  const [actionError, setActionError] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [confirmingId, setConfirmingId] = useState(null);
  // Ids whose DELETE is in flight; per row, so one slow delete neither
  // re-enables nor closes another row's confirmation.
  const [pendingDeletes, setPendingDeletes] = useState(() => new Set());
  const fileInputRef = useRef(null);
  const confirmCancelRef = useRef(null);

  useEffect(() => {
    // The Delete button unmounts when the prompt opens, which would drop
    // keyboard focus to the page body; Cancel is the safe default.
    if (confirmingId) confirmCancelRef.current?.focus();
  }, [confirmingId]);

  useEffect(() => {
    if (federated || !nodeId || attachmentsKnownUnavailable) return undefined;
    // The dialog keys this panel by node id, so state starts fresh per node.
    let active = true;
    listNodeAttachments(nodeId)
      .then((result) => {
        if (active) setListing(result);
      })
      .catch((err) => {
        if (err?.code === 'attachments_unavailable') attachmentsKnownUnavailable = true;
        if (active) setLoadError(err?.code || '');
      });
    return () => {
      active = false;
    };
  }, [nodeId, federated]);

  if (federated) return null;
  const attachments = listing?.attachments || [];
  if (!typeAllowsAttachments && attachments.length === 0) return null;

  const limits = listing?.limits;
  const errorParams = limits
    ? {
        max: formatBytes(limits.max_file_bytes, language),
        files: limits.max_files_per_node,
        total: formatBytes(limits.max_bytes_per_node, language),
      }
    : {};

  const messageFor = (code, detail) => {
    if (KNOWN_ERRORS.has(code)) return t(`attachments.errors.${code}`, errorParams);
    if (detail) return t('attachments.errors.generic_with_detail', { detail });
    return t('attachments.errors.generic');
  };

  const handleFileChosen = async (event) => {
    const file = event.target.files?.[0];
    // Clear the input so choosing the same file again fires another change.
    event.target.value = '';
    if (!file) return;
    setActionError(null);
    const refused = precheckUpload(file, attachments, limits);
    if (refused) {
      setActionError(messageFor(refused));
      return;
    }
    setUploading(true);
    try {
      const record = await uploadNodeAttachment(nodeId, file);
      setListing((prev) => ({ ...prev, attachments: [...(prev?.attachments || []), record] }));
    } catch (err) {
      setActionError(messageFor(err.code, err.message));
    } finally {
      setUploading(false);
    }
  };

  const handleDelete = async (attachmentId) => {
    setActionError(null);
    setPendingDeletes((prev) => new Set(prev).add(attachmentId));
    try {
      await deleteNodeAttachment(nodeId, attachmentId);
      setListing((prev) => ({
        ...prev,
        attachments: (prev?.attachments || []).filter((a) => a.id !== attachmentId),
      }));
    } catch (err) {
      setActionError(messageFor(err.code, err.message));
    } finally {
      setPendingDeletes((prev) => {
        const next = new Set(prev);
        next.delete(attachmentId);
        return next;
      });
      setConfirmingId((current) => (current === attachmentId ? null : current));
    }
  };

  return (
    <div className="node-detail-section node-attachments" data-testid="node-attachments">
      <label>{t('attachments.title')}</label>

      {loadError !== null && (
        <p className="node-attachments-error">
          {loadError === 'attachments_unavailable'
            ? messageFor(loadError)
            : t('attachments.load_error')}
        </p>
      )}
      {!listing && loadError === null && (
        <p className="node-attachments-muted">{t('attachments.loading')}</p>
      )}
      {listing && attachments.length === 0 && (
        <p className="node-attachments-muted">{t('attachments.empty')}</p>
      )}

      {attachments.length > 0 && (
        <ul className="node-attachments-list">
          {attachments.map((a) => (
            <li key={a.id} className="node-attachments-row">
              <div className="node-attachments-info">
                <a
                  href={getNodeAttachmentUrl(nodeId, a.id)}
                  download
                  className="node-attachments-name"
                  title={t('attachments.download')}
                >
                  {a.filename}
                </a>
                <span className="node-attachments-muted">
                  {formatBytes(a.size, language)} ·{' '}
                  {a.uploaded_at ? new Date(a.uploaded_at).toLocaleString(language) : ''}
                </span>
              </div>
              {confirmingId === a.id || pendingDeletes.has(a.id) ? (
                <div className="node-attachments-confirm">
                  <span>{t('attachments.confirm_delete', { name: a.filename })}</span>
                  <button
                    type="button"
                    className="danger"
                    disabled={pendingDeletes.has(a.id)}
                    onClick={() => handleDelete(a.id)}
                  >
                    {t('attachments.delete')}
                  </button>
                  <button
                    type="button"
                    className="secondary"
                    ref={confirmingId === a.id ? confirmCancelRef : null}
                    disabled={pendingDeletes.has(a.id)}
                    onClick={() => setConfirmingId(null)}
                  >
                    {t('attachments.cancel')}
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  className="secondary"
                  aria-label={t('attachments.delete_named', { name: a.filename })}
                  onClick={() => setConfirmingId(a.id)}
                >
                  {t('attachments.delete')}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {listing?.uploads_allowed && (
        <div className="node-attachments-upload">
          <input
            ref={fileInputRef}
            type="file"
            className="node-attachments-file-input"
            data-testid="node-attachments-input"
            onChange={handleFileChosen}
            disabled={uploading}
          />
          <button
            type="button"
            className="secondary"
            disabled={uploading}
            onClick={() => fileInputRef.current?.click()}
          >
            {uploading ? t('attachments.uploading') : t('attachments.upload')}
          </button>
          {limits && (
            <span className="node-attachments-muted">
              {t('attachments.limits_hint', errorParams)}
            </span>
          )}
        </div>
      )}

      {actionError && (
        <p className="node-attachments-error" role="alert">
          {actionError}
        </p>
      )}
    </div>
  );
}

export default NodeAttachmentsPanel;
