import { memo, useContext } from 'react';
import { NodeResizer } from 'reactflow';
import { AnnotationContext } from './AnnotationContext';
import './GenericAnnotationNode.css';

const DEFAULT_COLOR = '#94a3b8';

// frame/shape/image are the generic kinds that carry an explicit box size
// (SIZED_GENERIC_KINDS in utils/annotations.js) and are the only ones
// resizable in this slice; text/icon/vote_dot render at a fixed intrinsic
// size, so resizing them has no model-space geometry to change.
const RESIZABLE_KINDS = new Set(['frame', 'shape', 'image']);
const MIN_SIZE = 40;

/**
 * GenericAnnotationNode - a simple visual representation for the v1
 * annotation types that have no dedicated per-type editor yet (text, frame,
 * shape, icon, vote_dot, image; see docs/ANNOTATION_CONTRACT.md). These were
 * previously normalized by annotationModel.js but dropped by the overlay
 * translation layer, so an MCP-created annotation of one of these types never
 * rendered. Selection and move (drag) are handled generically by GraphCanvas
 * for every annotation type; this component adds the visual selection
 * outline and, for the sized kinds, model-space resize via ReactFlow's
 * NodeResizer. Per-type property editors remain out of scope for v1, same as
 * documented for the MCP tools.
 */
function GenericAnnotationNode({ type, data, selected }) {
  const kind = type;
  const color = data?.color || DEFAULT_COLOR;
  const locked = Boolean(data?.locked);
  const { notifyChange } = useContext(AnnotationContext);
  const selectedClass = selected ? ' selected' : '';

  // Locked annotations already refuse to drag (draggable: !locked in
  // overlayToFlowNode); hide the resize handles too so "locked" reads as one
  // consistent geometry lock rather than only blocking one of two ways to
  // move/resize the object.
  const resizer = RESIZABLE_KINDS.has(kind) && (
    <NodeResizer
      minWidth={MIN_SIZE}
      minHeight={MIN_SIZE}
      isVisible={Boolean(selected) && !locked}
      lineStyle={{ stroke: color, strokeWidth: 2 }}
      handleStyle={{ width: 10, height: 10, background: color, border: '2px solid white' }}
      onResizeEnd={notifyChange}
    />
  );

  if (kind === 'text') {
    return (
      <div className={`graph-generic-annotation-node kind-text${selectedClass}`} style={{ color }}>
        {data.text || ''}
      </div>
    );
  }

  if (kind === 'frame') {
    return (
      <>
        {resizer}
        <div
          className={`graph-generic-annotation-node kind-frame${selectedClass}`}
          style={{ borderColor: color, width: '100%', height: '100%' }}
        />
      </>
    );
  }

  if (kind === 'shape') {
    const shape = data.shape || 'rectangle';
    return (
      <>
        {resizer}
        <div
          className={`graph-generic-annotation-node kind-shape shape-${shape}${selectedClass}`}
          style={{ backgroundColor: color, width: '100%', height: '100%' }}
        />
      </>
    );
  }

  if (kind === 'icon') {
    return (
      <div
        className={`graph-generic-annotation-node kind-icon${selectedClass}`}
        style={{ borderColor: color }}
        title={data.icon}
      >
        {(data.icon || '?').slice(0, 2)}
      </div>
    );
  }

  if (kind === 'vote_dot') {
    return (
      <div
        className={`graph-generic-annotation-node kind-vote_dot${selectedClass}`}
        style={{ backgroundColor: color }}
      >
        {data.value ?? ''}
      </div>
    );
  }

  if (kind === 'image') {
    const url = data.image?.url;
    if (!url) {
      return (
        <>
          {resizer}
          <div
            className={`graph-generic-annotation-node kind-image kind-image-empty${selectedClass}`}
          >
            {data.alt || ''}
          </div>
        </>
      );
    }
    return (
      <>
        {resizer}
        <img
          className={`graph-generic-annotation-node kind-image${selectedClass}`}
          src={url}
          alt={data.alt || ''}
          style={{ width: '100%', height: '100%' }}
        />
      </>
    );
  }

  return null;
}

export default memo(GenericAnnotationNode);
