import React from 'react';
import ReactDOM from 'react-dom/client';
import Widget from './Widget';
import './widget.css';

// Mount widget to the specified element or default
const containerId = window.GRAPH_WIDGET_CONTAINER || 'widget-root';
const container = document.getElementById(containerId);

if (container) {
  ReactDOM.createRoot(container).render(
    <React.StrictMode>
      <Widget />
    </React.StrictMode>
  );
} else {
  console.error(`Graph widget container #${containerId} not found`);
}

// Export for programmatic use
export { default as Widget } from './Widget';
export * from './mcpClient';
