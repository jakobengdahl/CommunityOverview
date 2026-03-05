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
  Diagram3Fill,
  QuestionCircleFill,
} from 'react-bootstrap-icons';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import './FloatingToolbar.css';

// Registry of available icons, keyed by Bootstrap Icon name.
// The schema_config.json "icon" field references these keys.
const ICON_REGISTRY = {
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
  Diagram3Fill,
  QuestionCircleFill,
};

// Legacy fallback: maps node type name -> icon name (used when schema has no icon field)
const LEGACY_ICON_MAP = {
  Actor: 'PersonFill',
  Initiative: 'RocketTakeoffFill',
  Capability: 'LightningFill',
  Resource: 'FileEarmarkTextFill',
  Legislation: 'ShieldFillCheck',
  Theme: 'TagsFill',
  Goal: 'TrophyFill',
  Event: 'CalendarEventFill',
  Data: 'DatabaseFill',
  Dataset: 'DatabaseFill',
  Risk: 'ExclamationTriangleFill',
  'Hållpunkt': 'PinAngleFill',
  'Undersökning': 'ClipboardDataFill',
  'Värdemängd': 'ListOl',
  'Variabel': 'Sliders',
  'Population': 'PeopleFill',
  'Klassifikation': 'Diagram3Fill',
  Agent: 'CpuFill',
  EventSubscription: 'BellFill',
  SavedView: 'BookmarkFill',
  Group: 'FolderFill',
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
  'Klassifikation': '#84CC16',
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

/**
 * Resolve icon component for a node type.
 * Priority: schema icon field -> legacy fallback -> QuestionCircleFill
 */
function resolveIcon(nodeType, schema) {
  // 1. Check schema icon field
  const schemaIcon = schema?.node_types?.[nodeType]?.icon;
  if (schemaIcon && ICON_REGISTRY[schemaIcon]) {
    return ICON_REGISTRY[schemaIcon];
  }
  // 2. Legacy fallback by node type name
  const legacyName = LEGACY_ICON_MAP[nodeType];
  if (legacyName && ICON_REGISTRY[legacyName]) {
    return ICON_REGISTRY[legacyName];
  }
  // 3. Default
  return QuestionCircleFill;
}

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

    // System types: show from schema if present, always include Group
    const systemTypes = SYSTEM_TYPES.filter(
      (st) => st === 'Group' || !schema?.node_types || schema.node_types[st]
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

          const Icon = resolveIcon(nodeType, schema);
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

// Export ICON_MAP as resolved components for backward compatibility
const ICON_MAP = Object.fromEntries(
  Object.entries(LEGACY_ICON_MAP).map(([k, v]) => [k, ICON_REGISTRY[v]])
);

export { ICON_MAP, COLOR_MAP, ICON_REGISTRY };
export default FloatingToolbar;
