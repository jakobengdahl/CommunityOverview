/**
 * The card fields CustomNode reads from a graph node, shared by the canvas
 * and editor previews so both draw the same text and colour.
 */
export function nodeCardData(node, color) {
  return {
    ...node,
    label: node.name,
    summary: node.summary || node.description?.slice(0, 100),
    nodeType: node.type,
    color,
  };
}
