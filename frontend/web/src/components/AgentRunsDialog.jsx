import { Fragment, useCallback, useEffect, useState } from 'react';
import { useI18n } from '../i18n';
import './CreateSubscriptionDialog.css';
import './AgentRunsDialog.css';

const STATUS_CLASS = {
  queued: 'agent-run-status-queued',
  running: 'agent-run-status-running',
  succeeded: 'agent-run-status-succeeded',
  failed: 'agent-run-status-failed',
  cancelled: 'agent-run-status-cancelled',
};

function formatTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/**
 * Read-only viewer for durable AgentRun history (GET /agents/runs).
 *
 * Lists runs newest-first with status, trigger, and timestamps; each row
 * expands to show event type, attempts, correlation, error and terminal
 * result. When `agentId` is set the list is scoped to that agent.
 */
export default function AgentRunsDialog({ agentId = null, onClose }) {
  const { t } = useI18n();
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (agentId) params.set('agent_id', agentId);
      const qs = params.toString();
      const res = await fetch(`/agents/runs${qs ? `?${qs}` : ''}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setRuns(await res.json());
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="dialog-overlay" onClick={onClose}>
      <div className="dialog-content subscription-dialog" onClick={(e) => e.stopPropagation()}>
        <h2>{t('agent_runs.title')}</h2>
        <p className="dialog-description">{t('agent_runs.description')}</p>

        <div className="form-section">
          <button type="button" className="btn-secondary" onClick={load} disabled={loading}>
            {t('agent_runs.refresh')}
          </button>

          {loading && <p>{t('agent_runs.loading')}</p>}
          {error && <p className="agent-run-error">{t('agent_runs.load_error', { error })}</p>}
          {!loading && !error && runs.length === 0 && (
            <p className="agent-runs-empty">{t('agent_runs.empty')}</p>
          )}

          {!loading && !error && runs.length > 0 && (
            <table className="agent-runs-table">
              <thead>
                <tr>
                  <th>{t('agent_runs.col_status')}</th>
                  <th>{t('agent_runs.col_agent')}</th>
                  <th>{t('agent_runs.col_trigger')}</th>
                  <th>{t('agent_runs.col_started')}</th>
                  <th>{t('agent_runs.col_finished')}</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <Fragment key={run.id}>
                    <tr
                      className="agent-run-row"
                      onClick={() => setExpanded(expanded === run.id ? null : run.id)}
                    >
                      <td>
                        <span className={`agent-run-status ${STATUS_CLASS[run.status] || ''}`}>
                          {t(`agent_runs.status_${run.status}`)}
                        </span>
                      </td>
                      <td>{run.agent_name || run.agent_id}</td>
                      <td>{t(`agent_runs.trigger_${run.trigger}`)}</td>
                      <td>{formatTime(run.started_at)}</td>
                      <td>{formatTime(run.finished_at)}</td>
                    </tr>
                    {expanded === run.id && (
                      <tr className="agent-run-detail">
                        <td colSpan={5}>
                          <dl>
                            <dt>{t('agent_runs.detail_event_type')}</dt>
                            <dd>{run.event_type || '—'}</dd>
                            <dt>{t('agent_runs.detail_attempts')}</dt>
                            <dd>{run.attempts}</dd>
                            <dt>{t('agent_runs.detail_correlation')}</dt>
                            <dd>{run.correlation_id || '—'}</dd>
                            {run.error && (
                              <>
                                <dt>{t('agent_runs.detail_error')}</dt>
                                <dd className="agent-run-error">{run.error}</dd>
                              </>
                            )}
                            {run.result && (
                              <>
                                <dt>{t('agent_runs.detail_result')}</dt>
                                <dd>
                                  <code>{JSON.stringify(run.result)}</code>
                                </dd>
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
            {t('agent_runs.close')}
          </button>
        </div>
      </div>
    </div>
  );
}
