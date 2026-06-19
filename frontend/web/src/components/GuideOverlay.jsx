import { useCallback, useEffect, useRef, useState } from 'react';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import * as api from '../services/api';
import { positionNewNodes } from '@community-graph/ui-graph-canvas';
import './GuideOverlay.css';

const TOOLTIP_WIDTH = 340;
const TOOLTIP_MARGIN = 16;
const TOOLTIP_MIN_HEIGHT = 180;

function getTooltipPlacement(targetEl) {
  if (!targetEl) return { style: { top: '50%', left: '50%', transform: 'translate(-50%, -50%)' }, arrow: 'none' };

  const rect = targetEl.getBoundingClientRect();
  const winW = window.innerWidth;
  const winH = window.innerHeight;

  const spaceRight = winW - rect.right;
  const spaceLeft = rect.left;
  const spaceBelow = winH - rect.bottom;
  const spaceAbove = rect.top;

  const clampTop = (top) => Math.max(TOOLTIP_MARGIN, Math.min(top, winH - TOOLTIP_MIN_HEIGHT - TOOLTIP_MARGIN));
  const clampLeft = (left) => Math.max(TOOLTIP_MARGIN, Math.min(left, winW - TOOLTIP_WIDTH - TOOLTIP_MARGIN));

  if (spaceLeft > TOOLTIP_WIDTH + TOOLTIP_MARGIN) {
    return { style: { top: clampTop(rect.top), left: rect.left - TOOLTIP_WIDTH - TOOLTIP_MARGIN }, arrow: 'right' };
  }
  if (spaceRight > TOOLTIP_WIDTH + TOOLTIP_MARGIN) {
    return { style: { top: clampTop(rect.top), left: rect.right + TOOLTIP_MARGIN }, arrow: 'left' };
  }
  if (spaceBelow > TOOLTIP_MIN_HEIGHT + TOOLTIP_MARGIN) {
    return { style: { top: rect.bottom + TOOLTIP_MARGIN, left: clampLeft(rect.left + rect.width / 2 - TOOLTIP_WIDTH / 2) }, arrow: 'top' };
  }
  if (spaceAbove > TOOLTIP_MIN_HEIGHT + TOOLTIP_MARGIN) {
    return { style: { top: rect.top - TOOLTIP_MARGIN - 10, left: clampLeft(rect.left + rect.width / 2 - TOOLTIP_WIDTH / 2), transform: 'translateY(-100%)' }, arrow: 'bottom' };
  }
  return { style: { top: '50%', left: '50%', transform: 'translate(-50%, -50%)' }, arrow: 'none' };
}

// All step types that trigger an async action before (or instead of) showing a tooltip
const ACTIONABLE_TYPES = [
  'search_nodes',
  'create_node', 'delete_node', 'show_node_detail', 'update_node',
  'create_edge', 'delete_edge',
  'load_saved_view',
  'clear_visualization', 'clear',
  'focus_node',
  'fill_chat_input',
  'fill_search_input',
  'minimize_chat', 'maximize_chat', 'toggle_chat',
];

function GuideOverlay() {
  const { guide, advanceGuide, stopGuide, setGuideStepInput } = useGraphStore();
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

  // Reposition tooltip whenever step changes
  useEffect(() => {
    if (!isActive || !currentStep) return;
    const target = currentStep.target || 'center';
    if (target === 'center') {
      setPlacement({ style: { top: '50%', left: '50%', transform: 'translate(-50%, -50%)' }, arrow: 'none' });
      return;
    }
    const el = document.getElementById('guide-target-' + target);
    setPlacement(getTooltipPlacement(el));
  }, [isActive, currentStepIndex]);

  // Focus tooltip div on each non-input step for keyboard navigation
  useEffect(() => {
    if (!isActive || !currentStep || currentStep.type === 'input') return;
    tooltipRef.current?.focus();
  }, [isActive, currentStepIndex, currentStep]);

  // Reset input and error when step changes
  useEffect(() => {
    setInputValue('');
    setActionError(null);
  }, [currentStepIndex]);

  // Focus input field on input steps
  useEffect(() => {
    if (!isActive || currentStep?.type !== 'input') return;
    const timer = setTimeout(() => inputRef.current?.focus(), 50);
    return () => clearTimeout(timer);
  }, [isActive, currentStep?.type, currentStepIndex]);

  // Execute action steps — reads live store state to avoid stale closures;
  // uses a cancelled flag to suppress async callbacks after guide is stopped or step changes.
  useEffect(() => {
    if (!isActive || !currentStep) return;

    const action = currentStep.action || (ACTIONABLE_TYPES.includes(currentStep.type) ? currentStep.type : null);
    if (!action) return;

    let cancelled = false;

    const executeAction = async () => {
      const store = useGraphStore.getState();
      store.setGuideExecutingAction(true);
      setActionError(null);

      try {
        // ── Node search ──────────────────────────────────────────────────
        if (action === 'search_nodes') {
          const result = await api.searchGraph(currentStep.query || '', {
            nodeTypes: currentStep.node_type ? [currentStep.node_type] : undefined,
            limit: 30,
          });
          if (!cancelled && result.nodes?.length > 0) {
            const s = useGraphStore.getState();
            const allEdges = [...s.edges, ...(result.edges || [])];
            s.addNodesToVisualization(positionNewNodes(result.nodes, s.nodes, allEdges), result.edges || []);
          }

        // ── Node CRUD ────────────────────────────────────────────────────
        } else if (action === 'create_node') {
          const nodeType = currentStep.node_type;
          const nodeData = currentStep.node_data || {};
          const result = await api.addNodes([{ type: nodeType, ...nodeData }], []);
          if (!cancelled && result.added_node_ids?.length > 0) {
            const nodeWithId = { type: nodeType, ...nodeData, id: result.added_node_ids[0] };
            const s = useGraphStore.getState();
            s.addNodesToVisualization(positionNewNodes([nodeWithId], s.nodes, s.edges), []);
          }

        } else if (action === 'update_node') {
          const nodeId = currentStep.node_id;
          const updates = currentStep.node_data || {};
          if (!cancelled && nodeId) {
            await api.updateNode(nodeId, updates);
            const s = useGraphStore.getState();
            const updatedNodes = s.nodes.map(n => n.id === nodeId ? { ...n, ...updates } : n);
            s.updateVisualization(updatedNodes, s.edges);
          }

        } else if (action === 'delete_node') {
          const nodeId = currentStep.node_id;
          if (nodeId) {
            await api.deleteNodes([nodeId], true);
            if (!cancelled) useGraphStore.getState().removeNode(nodeId);
          }

        } else if (action === 'show_node_detail') {
          const nodeId = currentStep.node_id;
          if (nodeId) {
            const result = await api.getNodeDetails(nodeId);
            if (!cancelled && result.success) {
              useGraphStore.getState().setDetailNode({ id: nodeId, data: result.node });
            }
          }

        // ── Edge CRUD ────────────────────────────────────────────────────
        } else if (action === 'create_edge') {
          const result = await api.addEdge(
            currentStep.source_id,
            currentStep.target_id,
            { type: currentStep.edge_type, label: currentStep.edge_label }
          );
          if (!cancelled && result.success && result.edge) {
            useGraphStore.getState().addNodesToVisualization([], [result.edge]);
          }

        } else if (action === 'delete_edge') {
          const edgeId = currentStep.edge_id;
          if (edgeId) {
            await api.deleteEdge(edgeId);
            if (!cancelled) useGraphStore.getState().removeEdge(edgeId);
          }

        // ── Saved view ───────────────────────────────────────────────────
        } else if (action === 'load_saved_view') {
          const nameOrId = currentStep.view_name || currentStep.node_id;
          if (nameOrId) {
            const result = await api.searchGraph(nameOrId, { nodeTypes: ['SavedView'], limit: 10 });
            const viewNode = result.nodes?.find(n => n.id === nameOrId || n.name === nameOrId);
            if (!cancelled && viewNode) {
              const nodeIds = viewNode.metadata?.node_ids || [];
              if (nodeIds.length > 0) {
                const s = useGraphStore.getState();
                s.clearVisualization();
                const details = await Promise.all(nodeIds.map(id => api.getNodeDetails(id).catch(() => null)));
                const loadedNodes = details.filter(d => d?.success).map(d => d.node);
                const savedEdges = viewNode.metadata?.edges || [];
                if (!cancelled) s.addNodesToVisualization(loadedNodes, savedEdges);
              }
            }
          }

        // ── Visualization control ─────────────────────────────────────────
        } else if (action === 'clear_visualization' || action === 'clear') {
          if (!cancelled) useGraphStore.getState().clearVisualization();

        } else if (action === 'focus_node') {
          const nodeId = currentStep.node_id;
          if (!cancelled && nodeId) useGraphStore.getState().setFocusNodeId(nodeId);

        // ── UI fill actions ───────────────────────────────────────────────
        } else if (action === 'fill_chat_input') {
          if (!cancelled) {
            useGraphStore.getState().setGuideChatInput({
              text: currentStep.query || currentStep.fill_text || '',
              animated: currentStep.animated !== false,
              auto_send: currentStep.auto_send === true,
            });
          }

        } else if (action === 'fill_search_input') {
          if (!cancelled) {
            useGraphStore.getState().setGuideSearchInput({
              text: currentStep.query || currentStep.fill_text || '',
              animated: currentStep.animated !== false,
            });
          }

        // ── Chat panel ────────────────────────────────────────────────────
        } else if (action === 'minimize_chat') {
          if (!cancelled) useGraphStore.getState().setChatPanelOpen(false);

        } else if (action === 'maximize_chat') {
          if (!cancelled) useGraphStore.getState().setChatPanelOpen(true);

        } else if (action === 'toggle_chat') {
          if (!cancelled) useGraphStore.getState().toggleChatPanel();
        }

      } catch (err) {
        console.error('[GuideOverlay] Action error:', err);
        if (!cancelled) setActionError(err.message);
      } finally {
        if (!cancelled) useGraphStore.getState().setGuideExecutingAction(false);
      }
    };

    executeAction();
    return () => { cancelled = true; };
  }, [currentStepIndex, isActive]); // eslint-disable-line react-hooks/exhaustive-deps

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
        tabIndex={-1}
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

          {actionError && <p className="guide-error">{actionError}</p>}

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
