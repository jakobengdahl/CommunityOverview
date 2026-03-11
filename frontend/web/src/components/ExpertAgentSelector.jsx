import { useState, useRef, useEffect } from 'react';
import { PlusCircleFill, Robot, CheckSquareFill, Square, XLg } from 'react-bootstrap-icons';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import './ExpertAgentSelector.css';

function ExpertAgentSelector() {
  const [isOpen, setIsOpen] = useState(false);
  const panelRef = useRef(null);

  const {
    availableExperts,
    activeExperts,
    toggleExpertAgent,
  } = useGraphStore();

  const { t, language } = useI18n();

  useEffect(() => {
    const handleClickOutside = (e) => {
      if (panelRef.current && !panelRef.current.contains(e.target)) {
        setIsOpen(false);
      }
    };
    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [isOpen]);

  if (!availableExperts || availableExperts.length === 0) return null;

  const activeCount = activeExperts.length;

  return (
    <div className="expert-selector-wrapper" ref={panelRef}>
      <button
        className={`expert-selector-trigger ${activeCount > 0 ? 'has-active' : ''}`}
        onClick={() => setIsOpen(!isOpen)}
        title={t('experts.toggle_panel')}
      >
        <Robot size={13} />
        <PlusCircleFill size={9} className="expert-selector-plus" />
        {activeCount > 0 && (
          <span className="expert-selector-badge">{activeCount}</span>
        )}
      </button>

      {isOpen && (
        <div className="expert-selector-panel">
          <div className="expert-selector-header">
            <span className="expert-selector-title">{t('experts.title')}</span>
            <button className="expert-selector-close" onClick={() => setIsOpen(false)}>
              <XLg size={12} />
            </button>
          </div>
          <div className="expert-selector-list">
            {availableExperts.map((agent) => {
              const isActive = activeExperts.includes(agent.id);
              const agentName = language === 'sv' ? agent.name : (agent.name_en || agent.name);
              const specialty = language === 'sv' ? agent.specialty : (agent.specialty_en || agent.specialty);

              return (
                <button
                  key={agent.id}
                  className={`expert-selector-item ${isActive ? 'active' : ''}`}
                  onClick={() => toggleExpertAgent(agent.id, language)}
                >
                  <div className="expert-selector-checkbox">
                    {isActive
                      ? <CheckSquareFill size={14} style={{ color: agent.color }} />
                      : <Square size={14} />
                    }
                  </div>
                  <div className="expert-selector-dot" style={{ backgroundColor: agent.color }} />
                  <div className="expert-selector-info">
                    <span className="expert-selector-name">{agentName}</span>
                    <span className="expert-selector-specialty">{specialty}</span>
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

export default ExpertAgentSelector;
