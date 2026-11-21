// covers: packages/ui-graph-canvas/src/components/GraphCanvas.jsx
import { it, expect, vi } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

it('selects triangle bodies and cards through pane selection and node changes', async () => {
  const resize = global.ResizeObserver;
  vi.stubGlobal(
    'DOMMatrixReadOnly',
    class {
      constructor() {
        this.m22 = 1;
      }
    }
  );
  global.ResizeObserver = class {
    constructor(callback) {
      this.callback = callback;
    }
    observe(target) {
      this.callback([{ target, contentRect: { width: 240, height: 180 } }]);
    }
    unobserve() {}
    disconnect() {}
  };
  const bounds = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    x: 0,
    y: 0,
    left: 0,
    top: 0,
    right: 800,
    bottom: 600,
    width: 800,
    height: 600,
  });
  const schema = {
    node_shapes: {
      triangle: {
        version: 1,
        base: 'triangle-up',
        scale: 1,
        placement: 'centre',
        areas: { first: 'type', second: 'name', third: null },
      },
    },
    node_types: { Risk: { shape: 'triangle' } },
  };
  const { container, unmount } = render(
    <GraphCanvas
      nodes={[
        { id: 'triangle', name: 'Risk', type: 'Risk', _savedPosition: { x: 100, y: 100 } },
        { id: 'card', name: 'Card', type: 'Actor', _savedPosition: { x: 400, y: 100 } },
      ]}
      edges={[]}
      schema={schema}
      touchMode="off"
    />
  );
  const pane = container.querySelector('.react-flow__pane');
  pane.classList.add('nopan');
  const label = container.querySelector('.graph-shape-outside-label .graph-shape-label-chip');
  // An own property, not vi.spyOn: since Vitest 4 spying on the inherited,
  // already-mocked prototype method returns that same mock, so this rect would
  // leak to every element, the canvas wrapper included.
  label.getBoundingClientRect = () => ({
    left: 190,
    right: 220,
    top: 75,
    bottom: 90,
    width: 30,
    height: 15,
  });
  await waitFor(() => expect(container.querySelector('[data-id="triangle"]')).toBeTruthy());
  fireEvent.mouseDown(pane, { button: 0, shiftKey: true, clientX: 102, clientY: 102 });
  fireEvent.mouseMove(pane, { clientX: 110, clientY: 110 });
  fireEvent.mouseUp(pane, { button: 0 });
  expect(container.querySelector('[data-id="triangle"]').classList.contains('selected')).toBe(
    false
  );
  fireEvent.mouseDown(pane, { button: 0, clientX: 105, clientY: 76 });
  fireEvent.mouseMove(pane, { clientX: 115, clientY: 85 });
  fireEvent.mouseUp(pane, { button: 0 });
  expect(container.querySelector('[data-id="triangle"]').classList.contains('selected')).toBe(
    false
  );
  fireEvent.mouseDown(pane, { button: 0, clientX: 195, clientY: 76 });
  fireEvent.mouseMove(pane, { clientX: 205, clientY: 85 });
  fireEvent.mouseUp(pane, { button: 0 });
  expect(container.querySelector('[data-id="triangle"]').classList.contains('selected')).toBe(true);
  fireEvent.mouseDown(pane, { button: 0, clientX: 200, clientY: 220 });
  fireEvent.mouseMove(pane, { clientX: 230, clientY: 250 });
  fireEvent.mouseUp(pane, { button: 0 });
  expect(container.querySelector('[data-id="triangle"]').classList.contains('selected')).toBe(true);
  fireEvent.mouseDown(pane, { button: 0, clientX: 405, clientY: 105 });
  fireEvent.mouseMove(pane, { clientX: 420, clientY: 120 });
  fireEvent.mouseUp(pane, { button: 0 });
  expect(container.querySelector('[data-id="card"]').classList.contains('selected')).toBe(true);
  unmount();
  bounds.mockRestore();
  global.ResizeObserver = resize;
});
