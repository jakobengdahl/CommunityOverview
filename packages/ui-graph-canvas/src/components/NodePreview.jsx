import { ReactFlowProvider } from 'reactflow';
import CustomNode from './CustomNode';
import { nodeCardData } from '../utils/nodeCardData';
import { nodeShapeGeometry } from '../utils/nodeShapes';

/**
 * One node drawn by the canvas renderer outside a flow, for editors that
 * preview a look before applying it. A null `definition` is the built-in
 * card; `values` are the area texts to draw. `theme` ('light' | 'dark')
 * scopes the card tokens; omitted, the page theme applies.
 */
export default function NodePreview({ node, color, definition, values, theme }) {
  const shapeRender = definition && {
    definition,
    values,
    geometry: nodeShapeGeometry(definition.base, definition.scale),
  };
  return (
    <ReactFlowProvider>
      <div className={`graph-node-preview${theme ? ` graph-card-theme-${theme}` : ''}`}>
        <CustomNode
          id={node.id}
          selected={false}
          data={{ ...nodeCardData(node, color), ...(shapeRender && { shapeRender }) }}
        />
      </div>
    </ReactFlowProvider>
  );
}
