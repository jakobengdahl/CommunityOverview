import { Component } from 'react';
import { useI18n } from '../i18n';
import './MetamodelExplorerDialog.css';

function Fallback({ onRetry, onClose }) {
  const { t } = useI18n();
  return (
    <div className="mme-overlay" onClick={onClose}>
      <div
        className="mme-dialog mme-error"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="mme-error-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mme-header">
          <span id="mme-error-title" className="mme-title">
            {t('metamodel.error_title')}
          </span>
        </div>
        <p className="mme-readonly-note">{t('metamodel.error_message')}</p>
        <div className="mme-error-actions">
          <button onClick={onRetry}>{t('metamodel.error_retry')}</button>
          <button onClick={onClose}>{t('metamodel.error_close')}</button>
        </div>
      </div>
    </div>
  );
}

/**
 * Contains render errors thrown by the metamodel explorer so they surface as a
 * recoverable message inside Settings instead of unmounting the whole app.
 */
class MetamodelExplorerBoundary extends Component {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error, info) {
    console.error('Metamodel explorer failed to render', error, info?.componentStack);
  }

  render() {
    if (this.state.failed) {
      return (
        <Fallback onRetry={() => this.setState({ failed: false })} onClose={this.props.onClose} />
      );
    }
    return this.props.children;
  }
}

export default MetamodelExplorerBoundary;
