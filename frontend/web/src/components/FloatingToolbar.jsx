import { useState, useRef, useCallback, useMemo } from 'react';
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
  PinAngleFill,
  ClipboardDataFill,
  PeopleFill,
  Sliders,
  ListOl,
  QuestionCircleFill,
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
  Dataset: DatabaseFill,
  Risk: ExclamationTriangleFill,
  'Hållpunkt': PinAngleFill,
  'Undersökning': ClipboardDataFill,
  'Värdemängd': ListOl,
  'Variabel': Sliders,
  'Population': PeopleFill,
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
  Dataset: '#06B6D4',
  Risk: '#DC2626',
  'Hållpunkt': '#8B5CF6',
  'Undersökning': '#F97316',
  'Värdemängd': '#FBBF24',
  'Variabel': '#14B8A6',
  'Population': '#EF4444',
  Agent: '#EC4899',
  EventSubscription: '#8B5CF6',
  SavedView: '#6B7280',
  Group: '#646cff',
};

// System types always shown at the bottom (not from schema)
const SYSTEM_TYPES = ['Agent', 'EventSubscription', 'Group'];
const VIEW_TYPES = ['SavedView'];

// Fallback order when schema hasn't loaded yet
const FALLBACK_DOMAIN_ORDER = [
  'Actor', 'Initiative', 'Capability', 'Resource', 'Legislation',
  'Theme', 'Goal', 'Event', 'Data', 'Risk',
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
  const schema = useGraphStore((s) => s.schema);

  // Build toolbar order from schema node types
  const toolbarOrder = useMemo(() => {
    let domainTypes;
    if (schema?.node_types) {
      domainTypes = Object.entries(schema.node_types)
        .filter(([, config]) => config.category !== 'system')
        .map(([name]) => name);
    } else {
      domainTypes = FALLBACK_DOMAIN_ORDER;
    }

    const systemTypes = SYSTEM_TYPES.filter(
      (t) => !schema?.node_types || schema.node_types[t]
    );

    return [
      ...domainTypes,
      null, // separator
      ...systemTypes,
      null, // separator
      ...VIEW_TYPES,
    ];
  }, [schema]);

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
        {toolbarOrder.map((nodeType, index) => {
          if (nodeType === null) {
            return <div key={`sep-${index}`} className="floating-toolbar-separator" />;
          }

          const Icon = ICON_MAP[nodeType] || QuestionCircleFill;
          const color = COLOR_MAP[nodeType] || schema?.node_types?.[nodeType]?.color || '#9CA3AF';
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
