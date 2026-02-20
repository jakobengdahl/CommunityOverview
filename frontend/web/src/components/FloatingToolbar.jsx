import { useState, useRef, useEffect, useCallback } from 'react';
import { createPortal } from 'react-dom';
import {
  PersonFill,
  RocketTakeoffFill,
  LightningFill,
  FileEarmarkTextFill,
  ShieldFillCheck,
  TagsFill,
  TrophyFill,
  CalendarEventFill,
  DatabaseFill,
  ExclamationTriangleFill,
  CpuFill,
  BellFill,
  BookmarkFill,
  FolderFill,
} from 'react-bootstrap-icons';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import './FloatingToolbar.css';

const ICON_MAP = {
  Actor: PersonFill,
  Initiative: RocketTakeoffFill,
  Capability: LightningFill,
  Resource: FileEarmarkTextFill,
  Legislation: ShieldFillCheck,
  Theme: TagsFill,
  Goal: TrophyFill,
  Event: CalendarEventFill,
  Data: DatabaseFill,
  Risk: ExclamationTriangleFill,
  Agent: CpuFill,
  EventSubscription: BellFill,
  SavedView: BookmarkFill,
  Group: FolderFill,
};

const COLOR_MAP = {
  Actor: '#3B82F6',
  Initiative: '#10B981',
  Capability: '#F97316',
  Resource: '#FBBF24',
  Legislation: '#EF4444',
  Theme: '#14B8A6',
  Goal: '#6366F1',
  Event: '#D946EF',
  Data: '#06B6D4',
  Risk: '#DC2626',
  Agent: '#EC4899',
  EventSubscription: '#8B5CF6',
  SavedView: '#6B7280',
  Group: '#646cff',
};

// Order of toolbar items: metadata types first, then system types (agents/webhooks/groups), then views
const TOOLBAR_ORDER = [
  'Actor',
  'Initiative',
  'Capability',
  'Resource',
  'Legislation',
  'Theme',
  'Goal',
  'Event',
  'Data',
  'Risk',
  null, // separator
  'Agent',
  'EventSubscription',
  'Group',
  null, // separator
  'SavedView',
];

function FloatingToolbar({
  onCreateNode,
  onCreateAgent,
  onCreateSubscription,
  onSaveView,
  onCreateGroup,
}) {
  const { t } = useI18n();
  const [hoveredType, setHoveredType] = useState(null);
  const [tooltipPos, setTooltipPos] = useState(null);
  const buttonRefs = useRef({});

  const handleClick = (nodeType) => {
    if (nodeType === 'Agent') {
      onCreateAgent?.();
    } else if (nodeType === 'EventSubscription') {
      onCreateSubscription?.();
    } else if (nodeType === 'SavedView') {
      onSaveView?.();
    } else if (nodeType === 'Group') {
      onCreateGroup?.();
    } else {
      onCreateNode?.(nodeType);
    }
  };

  const handleDragStart = (event, nodeType) => {
    if (nodeType === 'SavedView') {
      event.preventDefault();
      return;
    }
    event.dataTransfer.setData('application/reactflow-nodetype', nodeType);
    event.dataTransfer.effectAllowed = 'move';
  };

  const handleMouseEnter = useCallback((nodeType) => {
    setHoveredType(nodeType);
    const btn = buttonRefs.current[nodeType];
    if (btn) {
      const rect = btn.getBoundingClientRect();
      setTooltipPos({
        top: rect.top + rect.height / 2,
        left: rect.right + 12,
      });
    }
  }, []);

  const handleMouseLeave = useCallback(() => {
    setHoveredType(null);
    setTooltipPos(null);
  }, []);

  const getTooltipLabel = (nodeType) => {
    if (nodeType === 'EventSubscription') return t('toolbar.webhook');
    if (nodeType === 'Group') return t('toolbar.group');
    return nodeType;
  };

  return (
    <>
      <div className="floating-toolbar">
        {TOOLBAR_ORDER.map((nodeType, index) => {
          if (nodeType === null) {
            return <div key={`sep-${index}`} className="floating-toolbar-separator" />;
          }

          const Icon = ICON_MAP[nodeType];
          const color = COLOR_MAP[nodeType];
          const isDraggable = nodeType !== 'SavedView';

          return (
            <button
              key={nodeType}
              ref={(el) => { buttonRefs.current[nodeType] = el; }}
              className="floating-toolbar-item"
              onClick={() => handleClick(nodeType)}
              onMouseEnter={() => handleMouseEnter(nodeType)}
              onMouseLeave={handleMouseLeave}
              draggable={isDraggable}
              onDragStart={(e) => handleDragStart(e, nodeType)}
              style={{ '--toolbar-item-color': color }}
            >
              {Icon && <Icon size={18} />}
            </button>
          );
        })}
      </div>
      {hoveredType && tooltipPos && createPortal(
        <div
          className="floating-toolbar-tooltip"
          style={{ top: tooltipPos.top, left: tooltipPos.left }}
        >
          {getTooltipLabel(hoveredType)}
        </div>,
        document.body
      )}
    </>
  );
}

export { ICON_MAP, COLOR_MAP };
export default FloatingToolbar;
