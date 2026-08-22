import { memo } from 'react';
import './GenericAnnotationNode.css';

const DEFAULT_COLOR = '#94a3b8';

/**
 * GenericAnnotationNode - a simple, read-mostly visual representation for
 * the v1 annotation types that have no dedicated interactive canvas UX yet
 * (text, frame, shape, icon, vote_dot, image; see docs/ANNOTATION_CONTRACT.md).
 * These were previously normalized by annotationModel.js but dropped by the
 * overlay translation layer, so an MCP-created annotation of one of these
 * types never rendered. This component only needs to make them visible and
 * round-trip; editing them here is out of scope (unlike NoteNode/LabelNode).
 */
function GenericAnnotationNode({ type, data }) {
  const kind = type;
  const color = data?.color || DEFAULT_COLOR;

  if (kind === 'text') {
    return (
      <div className="graph-generic-annotation-node kind-text" style={{ color }}>
        {data.text || ''}
      </div>
    );
  }

  if (kind === 'frame') {
    return (
      <div
        className="graph-generic-annotation-node kind-frame"
        style={{ borderColor: color, width: '100%', height: '100%' }}
      />
    );
  }

  if (kind === 'shape') {
    const shape = data.shape || 'rectangle';
    return (
      <div
        className={`graph-generic-annotation-node kind-shape shape-${shape}`}
        style={{ backgroundColor: color, width: '100%', height: '100%' }}
      />
    );
  }

  if (kind === 'icon') {
    return (
      <div
        className="graph-generic-annotation-node kind-icon"
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
        className="graph-generic-annotation-node kind-vote_dot"
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
        <div className="graph-generic-annotation-node kind-image kind-image-empty">
          {data.alt || ''}
        </div>
      );
    }
    return (
      <img
        className="graph-generic-annotation-node kind-image"
        src={url}
        alt={data.alt || ''}
        style={{ width: '100%', height: '100%' }}
      />
    );
  }

  return null;
}

export default memo(GenericAnnotationNode);
