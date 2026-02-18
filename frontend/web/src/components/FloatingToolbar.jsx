import { useState } from 'react';
import {
  PersonFill,
  RocketTakeoffFill,
  LightningFill,
  FileEarmarkTextFill,
  ShieldFillCheck,
  TagsFill,
  TrophyFill,
  CalendarEventFill,
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

  return (
    <div className="floating-toolbar">
      {TOOLBAR_ORDER.map((nodeType, index) => {
        if (nodeType === null) {
          return <div key={`sep-${index}`} className="floating-toolbar-separator" />;
        }

        const Icon = ICON_MAP[nodeType];
        const color = COLOR_MAP[nodeType];
        const isDraggable = nodeType !== 'SavedView';

        return (
          <div key={nodeType} className="floating-toolbar-item-wrapper">
            <button
              className="floating-toolbar-item"
              onClick={() => handleClick(nodeType)}
              onMouseEnter={() => setHoveredType(nodeType)}
              onMouseLeave={() => setHoveredType(null)}
              draggable={isDraggable}
              onDragStart={(e) => handleDragStart(e, nodeType)}
              style={{ '--toolbar-item-color': color }}
              title={nodeType}
            >
              {Icon && <Icon size={18} />}
            </button>
            {hoveredType === nodeType && (
              <div className="floating-toolbar-tooltip">
                {nodeType === 'EventSubscription' ? t('toolbar.webhook') : nodeType === 'Group' ? t('toolbar.group') : nodeType}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export { ICON_MAP, COLOR_MAP };
export default FloatingToolbar;
