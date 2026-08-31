import { Fragment, useCallback, useEffect, useState } from 'react';
import { useI18n } from '../i18n';
import './CreateSubscriptionDialog.css';
import './AgentRunsDialog.css';

// Proposal status → the run-status badge classes (reused from AgentRunsDialog.css).
const STATUS_CLASS = {
  pending: 'agent-run-status-queued',
  approved: 'agent-run-status-running',
  applied: 'agent-run-status-succeeded',
  rejected: 'agent-run-status-cancelled',
  apply_failed: 'agent-run-status-failed',
};

function formatTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/**
 * Viewer + approve/reject surface for durable agent proposals
 * (GET /agents/proposals). Pending proposals can be approved or rejected;
 * approving an act_after_approval proposal applies the captured action.
 */
export default function AgentProposalsDialog({ agentId = null, onClose }) {
  const { t } = useI18n();
  const [proposals, setProposals] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [busyId, setBusyId] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (agentId) params.set('agent_id', agentId);
      const qs = params.toString();
      const res = await fetch(`/agents/proposals${qs ? `?${qs}` : ''}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setProposals(await res.json());
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    load();
  }, [load]);

  const decide = useCallback(
    async (proposalId, action) => {
      setBusyId(proposalId);
      setError(null);
      try {
        const res = await fetch(`/agents/proposals/${proposalId}/${action}`, {
          method: 'POST',
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        await load();
      } catch (e) {
        setError(e.message || String(e));
      } finally {
        setBusyId(null);
      }
    },
    [load]
  );

  return (
    <div className="dialog-overlay" onClick={onClose}>
      <div className="dialog-content subscription-dialog" onClick={(e) => e.stopPropagation()}>
        <h2>{t('agent_proposals.title')}</h2>
        <p className="dialog-description">{t('agent_proposals.description')}</p>

        <div className="form-section">
          <button type="button" className="btn-secondary" onClick={load} disabled={loading}>
            {t('agent_proposals.refresh')}
          </button>

          {loading && <p>{t('agent_proposals.loading')}</p>}
          {error && <p className="agent-run-error">{t('agent_proposals.load_error', { error })}</p>}
          {!loading && !error && proposals.length === 0 && (
            <p className="agent-runs-empty">{t('agent_proposals.empty')}</p>
          )}

          {!loading && !error && proposals.length > 0 && (
            <table className="agent-runs-table">
              <thead>
                <tr>
                  <th>{t('agent_proposals.col_status')}</th>
                  <th>{t('agent_proposals.col_agent')}</th>
                  <th>{t('agent_proposals.col_tool')}</th>
                  <th>{t('agent_proposals.col_created')}</th>
                  <th>{t('agent_proposals.col_actions')}</th>
                </tr>
              </thead>
              <tbody>
                {proposals.map((p) => (
                  <Fragment key={p.id}>
                    <tr
                      className="agent-run-row"
                      onClick={() => setExpanded(expanded === p.id ? null : p.id)}
                    >
                      <td>
                        <span className={`agent-run-status ${STATUS_CLASS[p.status] || ''}`}>
                          {t(`agent_proposals.status_${p.status}`)}
                        </span>
                      </td>
                      <td>{p.agent_name || p.agent_id}</td>
                      <td>
                        <code>{p.tool}</code>
                      </td>
                      <td>{formatTime(p.created_at)}</td>
                      <td onClick={(e) => e.stopPropagation()}>
                        {p.status === 'pending' ? (
                          <>
                            <button
                              type="button"
                              className="btn-primary"
                              disabled={busyId === p.id}
                              onClick={() => decide(p.id, 'approve')}
                            >
                              {t('agent_proposals.approve')}
                            </button>{' '}
                            <button
                              type="button"
                              className="btn-secondary"
                              disabled={busyId === p.id}
                              onClick={() => decide(p.id, 'reject')}
                            >
                              {t('agent_proposals.reject')}
                            </button>
                          </>
                        ) : (
                          <span className="agent-runs-empty">—</span>
                        )}
                      </td>
                    </tr>
                    {expanded === p.id && (
                      <tr className="agent-run-detail">
                        <td colSpan={5}>
                          <dl>
                            <dt>{t('agent_proposals.detail_autonomy')}</dt>
                            <dd>{p.autonomy_level}</dd>
                            <dt>{t('agent_proposals.detail_args')}</dt>
                            <dd>
                              <code>{JSON.stringify(p.input_args)}</code>
                            </dd>
                            {p.decided_by && (
                              <>
                                <dt>{t('agent_proposals.detail_decided_by')}</dt>
                                <dd>{p.decided_by}</dd>
                              </>
                            )}
                            {p.apply_result && (
                              <>
                                <dt>{t('agent_proposals.detail_apply_result')}</dt>
                                <dd>
                                  <code>{JSON.stringify(p.apply_result)}</code>
                                </dd>
                              </>
                            )}
                            {p.apply_error && (
                              <>
                                <dt>{t('agent_proposals.detail_apply_error')}</dt>
                                <dd className="agent-run-error">{p.apply_error}</dd>
                              </>
                            )}
                          </dl>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="dialog-actions">
          <button type="button" className="btn-primary" onClick={onClose}>
            {t('agent_proposals.close')}
          </button>
        </div>
      </div>
    </div>
  );
}
