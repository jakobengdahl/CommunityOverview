import { useCallback, useEffect, useRef, useState } from 'react';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import * as api from '../services/api';
import { positionNewNodes } from '@community-graph/ui-graph-canvas';
import './GuideOverlay.css';

const TOOLTIP_WIDTH = 340;
const TOOLTIP_MARGIN = 16;

function getTooltipPlacement(targetEl) {
  if (!targetEl) return { style: { top: '50%', left: '50%', transform: 'translate(-50%, -50%)' }, arrow: 'none' };

  const rect = targetEl.getBoundingClientRect();
  const winW = window.innerWidth;
  const winH = window.innerHeight;

  const spaceRight = winW - rect.right;
  const spaceLeft = rect.left;
  const spaceBelow = winH - rect.bottom;
  const spaceAbove = rect.top;

  const clampTop = (top, h = 180) => Math.max(TOOLTIP_MARGIN, Math.min(top, winH - h - TOOLTIP_MARGIN));
  const clampLeft = (left) => Math.max(TOOLTIP_MARGIN, Math.min(left, winW - TOOLTIP_WIDTH - TOOLTIP_MARGIN));

  // Prefer left (most toolbars are on the right side)
  if (spaceLeft > TOOLTIP_WIDTH + TOOLTIP_MARGIN) {
    return {
      style: { top: clampTop(rect.top), left: rect.left - TOOLTIP_WIDTH - TOOLTIP_MARGIN },
      arrow: 'right',
    };
  }
  if (spaceRight > TOOLTIP_WIDTH + TOOLTIP_MARGIN) {
    return {
      style: { top: clampTop(rect.top), left: rect.right + TOOLTIP_MARGIN },
      arrow: 'left',
    };
  }
  if (spaceBelow > TOOLTIP_MARGIN + 60) {
    return {
      style: { top: rect.bottom + TOOLTIP_MARGIN, left: clampLeft(rect.left + rect.width / 2 - TOOLTIP_WIDTH / 2) },
      arrow: 'top',
    };
  }
  if (spaceAbove > TOOLTIP_MARGIN + 60) {
    return {
      style: { top: rect.top - TOOLTIP_MARGIN - 10, left: clampLeft(rect.left + rect.width / 2 - TOOLTIP_WIDTH / 2), transform: 'translateY(-100%)' },
      arrow: 'bottom',
    };
  }
  // Fallback center
  return { style: { top: '50%', left: '50%', transform: 'translate(-50%, -50%)' }, arrow: 'none' };
}

function GuideOverlay() {
  const {
    guide,
    advanceGuide,
    stopGuide,
    setGuideStepInput,
    setGuideExecutingAction,
    nodes,
    edges,
    addNodesToVisualization,
    updateVisualization,
    clearVisualization,
    setFocusNodeId,
  } = useGraphStore();

  const { t, language } = useI18n();
  const [placement, setPlacement] = useState({ style: {}, arrow: 'none' });
  const [inputValue, setInputValue] = useState('');
  const [actionError, setActionError] = useState(null);
  const tooltipRef = useRef(null);
  const inputRef = useRef(null);

  const { isActive, activeGuide, currentStepIndex, isExecutingAction } = guide;

  const steps = activeGuide?.steps || [];
  const currentStep = steps[currentStepIndex];
  const isLastStep = currentStepIndex >= steps.length - 1;

  const stepText = language === 'sv' && currentStep?.text_sv ? currentStep.text_sv : (currentStep?.text || '');
  const inputLabel = language === 'sv' && currentStep?.input_label_sv ? currentStep.input_label_sv : (currentStep?.input_label || '');
  const inputPlaceholder = language === 'sv' && currentStep?.input_placeholder_sv ? currentStep.input_placeholder_sv : (currentStep?.input_placeholder || '');

  // Resolve and position tooltip whenever step changes
  useEffect(() => {
    if (!isActive || !currentStep) return;

    const target = currentStep.target || 'center';
    if (target === 'center') {
      setPlacement({ style: { top: '50%', left: '50%', transform: 'translate(-50%, -50%)' }, arrow: 'none' });
      return;
    }

    const el = document.getElementById('guide-target-' + target);
    setPlacement(getTooltipPlacement(el));
  }, [isActive, currentStep, currentStepIndex]);

  // Reset input value when step changes
  useEffect(() => {
    setInputValue('');
    setActionError(null);
  }, [currentStepIndex]);

  // Focus input when an input step is shown
  useEffect(() => {
    if (currentStep?.type === 'input' && inputRef.current) {
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [currentStep]);

  // Execute action steps
  useEffect(() => {
    if (!isActive || !currentStep) return;
    // Only run for steps that have an action field (not pure tooltip/input types)
    const actionableTypes = ['search_nodes', 'add_node', 'focus_node', 'clear_visualization'];
    const isActionStep = currentStep.action || actionableTypes.includes(currentStep.type);
    if (!isActionStep) return;

    const action = currentStep.action || currentStep.type;

    const executeAction = async () => {
      setGuideExecutingAction(true);
      setActionError(null);
      try {
        if (action === 'search_nodes') {
          const query = currentStep.query || '';
          const nodeType = currentStep.node_type || currentStep.nodeType;
          const result = await api.searchGraph(query, {
            nodeTypes: nodeType ? [nodeType] : undefined,
            limit: 30,
          });
          if (result.nodes && result.nodes.length > 0) {
            const currentNodes = useGraphStore.getState().nodes;
            const currentEdges = useGraphStore.getState().edges;
            const allEdges = [...currentEdges, ...(result.edges || [])];
            const positioned = positionNewNodes(result.nodes, currentNodes, allEdges);
            addNodesToVisualization(positioned, result.edges || []);
          }
        } else if (action === 'clear_visualization') {
          clearVisualization();
        } else if (action === 'focus_node') {
          const nodeId = currentStep.node_id || currentStep.nodeId;
          if (nodeId) setFocusNodeId(nodeId);
        } else if (action === 'add_node') {
          const nodeData = currentStep.node_data || currentStep.nodeData;
          if (nodeData) {
            const result = await api.addNodes([nodeData], []);
            if (result.added_node_ids?.length > 0) {
              const nodeWithId = { ...nodeData, id: result.added_node_ids[0] };
              const currentNodes = useGraphStore.getState().nodes;
              const currentEdges = useGraphStore.getState().edges;
              addNodesToVisualization(positionNewNodes([nodeWithId], currentNodes, currentEdges), []);
            }
          }
        }
      } catch (err) {
        console.error('[GuideOverlay] Action error:', err);
        setActionError(err.message);
      } finally {
        setGuideExecutingAction(false);
      }
    };

    executeAction();
  }, [currentStepIndex]); // re-run when step index changes — intentionally not including all deps

  const handleNext = useCallback(() => {
    if (currentStep?.type === 'input' && currentStep.store_as) {
      setGuideStepInput(currentStep.store_as, inputValue);
    }
    advanceGuide();
  }, [currentStep, inputValue, setGuideStepInput, advanceGuide]);

  const handleKeyDown = useCallback((e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleNext();
    } else if (e.key === 'Escape') {
      stopGuide();
    }
  }, [handleNext, stopGuide]);

  if (!isActive || !activeGuide || !currentStep) return null;

  const guideName = language === 'sv' && activeGuide.name_sv ? activeGuide.name_sv : (activeGuide.name || '');

  return (
    <>
      <div className="guide-backdrop" aria-hidden="true" />

      <div
        ref={tooltipRef}
        className={`guide-tooltip guide-arrow-${placement.arrow}`}
        style={{ position: 'fixed', width: TOOLTIP_WIDTH, ...placement.style }}
        role="dialog"
        aria-modal="false"
        aria-label={guideName || t('guide.title')}
        onKeyDown={handleKeyDown}
      >
        <div className="guide-header">
          <span className="guide-name">{guideName}</span>
          <div className="guide-progress">
            {steps.map((_, i) => (
              <span
                key={i}
                className={`guide-progress-dot ${i === currentStepIndex ? 'active' : i < currentStepIndex ? 'done' : ''}`}
              />
            ))}
          </div>
          <button
            className="guide-close"
            onClick={stopGuide}
            title={t('guide.cancel')}
            aria-label={t('guide.cancel')}
          >
            ×
          </button>
        </div>

        <div className="guide-body">
          {isExecutingAction ? (
            <div className="guide-executing">
              <span className="guide-spinner" />
              <span>{t('guide.loading')}</span>
            </div>
          ) : (
            <p className="guide-text">{stepText}</p>
          )}

          {actionError && (
            <p className="guide-error">{actionError}</p>
          )}

          {currentStep.type === 'input' && (
            <div className="guide-input-wrap">
              {inputLabel && <label className="guide-input-label">{inputLabel}</label>}
              <input
                ref={inputRef}
                className="guide-input"
                type="text"
                value={inputValue}
                placeholder={inputPlaceholder}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleNext()}
              />
            </div>
          )}
        </div>

        <div className="guide-footer">
          <span className="guide-step-counter">
            {t('guide.step_counter', { current: currentStepIndex + 1, total: steps.length })}
          </span>
          <div className="guide-actions">
            <button className="guide-btn guide-btn-cancel" onClick={stopGuide}>
              {t('guide.cancel')}
            </button>
            <button
              className="guide-btn guide-btn-next"
              onClick={handleNext}
              disabled={isExecutingAction}
            >
              {isLastStep ? t('guide.close') : t('guide.next')}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

export default GuideOverlay;
