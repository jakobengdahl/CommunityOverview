import { useState, useRef, useEffect } from 'react';
import useGraphStore from '../store/graphStore';
import './ShapeRectangle.css';

const COLOR_PALETTE = [
  '#EF4444', '#F97316', '#F59E0B', '#FBBF24', '#84CC16',
  '#10B981', '#14B8A6', '#06B6D4', '#3B82F6', '#6366F1',
  '#8B5CF6', '#A855F7', '#D946EF', '#EC4899', '#F43F5E',
  '#6B7280', '#4B5563', '#374151', '#1F2937', '#111827'
];

function ShapeRectangle({ shape, reactFlowInstance, reactFlowWrapper }) {
  const { updateShape, deleteShape, nodes, updateNodePositions } = useGraphStore();
  const [isHovered, setIsHovered] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [showColorPicker, setShowColorPicker] = useState(false);
  const [title, setTitle] = useState(shape.title || 'Untitled');
  const [isDragging, setIsDragging] = useState(false);
  const [isResizing, setIsResizing] = useState(false);
  const [resizeEdge, setResizeEdge] = useState(null);
  const dragStartPos = useRef({ x: 0, y: 0 });
  const shapeStartPos = useRef({ x: 0, y: 0 });
  const shapeStartSize = useRef({ width: 0, height: 0 });

  // Get ReactFlow's viewport transform for coordinate conversion
  const getReactFlowPosition = () => {
    if (!reactFlowInstance) return { x: shape.x || 0, y: shape.y || 0 };

    const viewport = reactFlowInstance.getViewport();
    const x = (shape.x || 0) * viewport.zoom + viewport.x;
    const y = (shape.y || 0) * viewport.zoom + viewport.y;

    return { x, y, zoom: viewport.zoom };
  };

  const handleTitleChange = (e) => {
    setTitle(e.target.value);
  };

  const handleTitleBlur = () => {
    updateShape(shape.id, { title });
  };

  const handleColorChange = (color) => {
    updateShape(shape.id, { color });
    setShowColorPicker(false);
  };

  const handleEditClick = () => {
    setIsEditing(true);
  };

  const handleCloseEdit = () => {
    setIsEditing(false);
    setShowColorPicker(false);
  };

  const handleMouseDown = (e, edge = null) => {
    if (edge) {
      // Resizing
      e.stopPropagation();
      setIsResizing(true);
      setResizeEdge(edge);
      dragStartPos.current = { x: e.clientX, y: e.clientY };
      shapeStartPos.current = { x: shape.x, y: shape.y };
      shapeStartSize.current = { width: shape.width, height: shape.height };
    } else if (e.target.classList.contains('shape-header') || e.target.closest('.shape-header')) {
      // Dragging from header
      setIsDragging(true);

      if (reactFlowInstance) {
        const viewport = reactFlowInstance.getViewport();
        dragStartPos.current = {
          x: (e.clientX - viewport.x) / viewport.zoom,
          y: (e.clientY - viewport.y) / viewport.zoom
        };
      } else {
        dragStartPos.current = { x: e.clientX, y: e.clientY };
      }

      shapeStartPos.current = { x: shape.x, y: shape.y };
    }
  };

  const handleMouseMove = (e) => {
    if (isDragging && reactFlowInstance) {
      const viewport = reactFlowInstance.getViewport();
      const currentX = (e.clientX - viewport.x) / viewport.zoom;
      const currentY = (e.clientY - viewport.y) / viewport.zoom;

      const deltaX = currentX - dragStartPos.current.x;
      const deltaY = currentY - dragStartPos.current.y;

      const newX = shapeStartPos.current.x + deltaX;
      const newY = shapeStartPos.current.y + deltaY;

      updateShape(shape.id, { x: newX, y: newY });

      // Move nodes that belong to this shape
      if (shape.nodeIds && shape.nodeIds.length > 0) {
        const nodeUpdates = shape.nodeIds.map(nodeId => {
          const node = nodes.find(n => n.id === nodeId);
          if (node && node.position) {
            return {
              id: nodeId,
              position: {
                x: node.position.x + deltaX,
                y: node.position.y + deltaY
              }
            };
          }
          return null;
        }).filter(Boolean);

        if (nodeUpdates.length > 0) {
          updateNodePositions(nodeUpdates);
        }
      }

      // Update drag start position for smooth dragging
      dragStartPos.current = { x: currentX, y: currentY };
      shapeStartPos.current = { x: newX, y: newY };
    } else if (isResizing && reactFlowInstance) {
      const viewport = reactFlowInstance.getViewport();
      const deltaX = (e.clientX - dragStartPos.current.x) / viewport.zoom;
      const deltaY = (e.clientY - dragStartPos.current.y) / viewport.zoom;

      let newX = shape.x;
      let newY = shape.y;
      let newWidth = shape.width;
      let newHeight = shape.height;

      switch (resizeEdge) {
        case 'right':
          newWidth = Math.max(100, shapeStartSize.current.width + deltaX);
          break;
        case 'bottom':
          newHeight = Math.max(60, shapeStartSize.current.height + deltaY);
          break;
        case 'left':
          newWidth = Math.max(100, shapeStartSize.current.width - deltaX);
          newX = shapeStartPos.current.x + deltaX;
          break;
        case 'top':
          newHeight = Math.max(60, shapeStartSize.current.height - deltaY);
          newY = shapeStartPos.current.y + deltaY;
          break;
        case 'top-left':
          newWidth = Math.max(100, shapeStartSize.current.width - deltaX);
          newHeight = Math.max(60, shapeStartSize.current.height - deltaY);
          newX = shapeStartPos.current.x + deltaX;
          newY = shapeStartPos.current.y + deltaY;
          break;
        case 'top-right':
          newWidth = Math.max(100, shapeStartSize.current.width + deltaX);
          newHeight = Math.max(60, shapeStartSize.current.height - deltaY);
          newY = shapeStartPos.current.y + deltaY;
          break;
        case 'bottom-left':
          newWidth = Math.max(100, shapeStartSize.current.width - deltaX);
          newHeight = Math.max(60, shapeStartSize.current.height + deltaY);
          newX = shapeStartPos.current.x + deltaX;
          break;
        case 'bottom-right':
          newWidth = Math.max(100, shapeStartSize.current.width + deltaX);
          newHeight = Math.max(60, shapeStartSize.current.height + deltaY);
          break;
      }

      updateShape(shape.id, { x: newX, y: newY, width: newWidth, height: newHeight });
    }
  };

  const handleMouseUp = () => {
    setIsDragging(false);
    setIsResizing(false);
    setResizeEdge(null);
  };

  useEffect(() => {
    if (isDragging || isResizing) {
      window.addEventListener('mousemove', handleMouseMove);
      window.addEventListener('mouseup', handleMouseUp);
      return () => {
        window.removeEventListener('mousemove', handleMouseMove);
        window.removeEventListener('mouseup', handleMouseUp);
      };
    }
  }, [isDragging, isResizing, shape, nodes]);

  const pos = getReactFlowPosition();

  return (
    <div
      className={`shape-rectangle ${isHovered ? 'hovered' : ''}`}
      style={{
        left: pos.x,
        top: pos.y,
        width: (shape.width || 200) * (pos.zoom || 1),
        height: (shape.height || 150) * (pos.zoom || 1),
        backgroundColor: shape.color || '#3B82F680',
        border: `2px solid ${shape.color || '#3B82F6'}`,
      }}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
    >
      {/* Header with title */}
      <div
        className="shape-header"
        onMouseDown={handleMouseDown}
        style={{ cursor: isDragging ? 'grabbing' : 'grab' }}
      >
        {isEditing ? (
          <input
            type="text"
            value={title}
            onChange={handleTitleChange}
            onBlur={handleTitleBlur}
            className="shape-title-input"
            autoFocus
            onClick={(e) => e.stopPropagation()}
          />
        ) : (
          <span className="shape-title">{title}</span>
        )}
      </div>

      {/* Edit controls */}
      {isHovered && !isEditing && (
        <button
          className="shape-edit-button"
          onClick={handleEditClick}
          title="Edit"
        >
          ✏️
        </button>
      )}

      {isEditing && (
        <div className="shape-edit-controls">
          {/* Color picker button */}
          <div className="color-picker-container">
            <button
              className="color-swatch color-swatch-gradient"
              onClick={() => setShowColorPicker(!showColorPicker)}
              title="Change color"
            />
            {showColorPicker && (
              <div className="color-picker-palette">
                {COLOR_PALETTE.map(color => (
                  <button
                    key={color}
                    className="color-option"
                    style={{ backgroundColor: color }}
                    onClick={() => handleColorChange(color)}
                  />
                ))}
              </div>
            )}
          </div>

          {/* Close button */}
          <button
            className="shape-close-button"
            onClick={handleCloseEdit}
            title="Close"
          >
            ✕
          </button>
        </div>
      )}

      {/* Resize handles */}
      {isHovered && (
        <>
          <div
            className="resize-handle resize-top"
            onMouseDown={(e) => handleMouseDown(e, 'top')}
          />
          <div
            className="resize-handle resize-bottom"
            onMouseDown={(e) => handleMouseDown(e, 'bottom')}
          />
          <div
            className="resize-handle resize-left"
            onMouseDown={(e) => handleMouseDown(e, 'left')}
          />
          <div
            className="resize-handle resize-right"
            onMouseDown={(e) => handleMouseDown(e, 'right')}
          />
          <div
            className="resize-handle resize-corner resize-top-left"
            onMouseDown={(e) => handleMouseDown(e, 'top-left')}
          />
          <div
            className="resize-handle resize-corner resize-top-right"
            onMouseDown={(e) => handleMouseDown(e, 'top-right')}
          />
          <div
            className="resize-handle resize-corner resize-bottom-left"
            onMouseDown={(e) => handleMouseDown(e, 'bottom-left')}
          />
          <div
            className="resize-handle resize-corner resize-bottom-right"
            onMouseDown={(e) => handleMouseDown(e, 'bottom-right')}
          />
        </>
      )}
    </div>
  );
}

export default ShapeRectangle;
