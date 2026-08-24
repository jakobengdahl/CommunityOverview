// Human GUI half of image annotation ingest: clipboard paste, the toolbox's
// file-picker item, and dropping an image file from the OS onto the canvas.
// Unlike every other annotation kind (see GraphCanvasAnnotationToolbox.test.jsx),
// none of these touch local node state directly — GraphCanvas hands the read
// file off to onImageIngest and waits for the host's server round-trip to
// come back over the session's realtime channel (see App.jsx's
// handleImageIngest and backend/service/rest_api.py's ingest_session_image).
// These tests cover only the canvas-side plumbing: that the right File/data
// reaches onImageIngest with a model-space position, and that a paste/drop
// aimed at an unrelated input is left alone.
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children, onPaneContextMenu, onPaneMouseDown, onDrop, onDragOver }) => (
    <div data-testid="react-flow" className="react-flow">
      <div
        data-testid="pane"
        onMouseDown={(event) => onPaneMouseDown?.(event)}
        onContextMenu={(event) => onPaneContextMenu?.(event)}
        onDrop={(event) => onDrop?.(event)}
        onDragOver={(event) => onDragOver?.(event)}
      />
      {children}
    </div>
  );
  return {
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    useNodesState: (initial) => [initial || [], vi.fn(), vi.fn()],
    useEdgesState: (initial) => [initial || [], vi.fn(), vi.fn()],
    useReactFlow: () => ({
      fitView: vi.fn(),
      zoomIn: vi.fn(),
      zoomOut: vi.fn(),
      getNodes: () => [],
      getEdges: () => [],
      setNodes: vi.fn(),
      setEdges: vi.fn(),
      screenToFlowPosition: ({ x, y }) => ({ x, y }),
      setCenter: vi.fn(),
      getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
    }),
    useOnSelectionChange: () => {},
    Background: () => <div data-testid="background" />,
    Controls: () => <div data-testid="controls" />,
    MiniMap: () => <div data-testid="minimap" />,
    NodeResizer: () => null,
    SelectionMode: { Partial: 'partial' },
  };
});

function pngFile(name = 'pic.png', type = 'image/png') {
  return new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])], name, { type });
}

// jsdom has no DragEvent constructor (https://github.com/jsdom/jsdom/issues/2913),
// so testing-library's fireEvent.drop falls back to a plain Event and silently
// drops clientX/clientY (Event's constructor init dict has no such fields) —
// dispatch manually instead, the same way the paste tests below attach
// clipboardData directly, since a plain Event freely accepts extra properties.
function dispatchDrop(target, { dataTransfer, clientX, clientY }) {
  const event = new Event('drop', { bubbles: true, cancelable: true });
  event.dataTransfer = dataTransfer;
  event.clientX = clientX;
  event.clientY = clientY;
  target.dispatchEvent(event);
}

describe('GraphCanvas image ingest', () => {
  describe('toolbox file picker', () => {
    it('does not call onImageIngest merely from opening the picker', () => {
      const onImageIngest = vi.fn();
      render(<GraphCanvas nodes={[]} edges={[]} onImageIngest={onImageIngest} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
      fireEvent.click(screen.getByRole('button', { name: /^image$/i }));
      expect(onImageIngest).not.toHaveBeenCalled();
    });

    it('reads the selected image file and hands a data URL + position to onImageIngest', async () => {
      const onImageIngest = vi.fn();
      render(<GraphCanvas nodes={[]} edges={[]} onImageIngest={onImageIngest} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
      fireEvent.click(screen.getByRole('button', { name: /^image$/i }));

      const input = screen.getByTestId('graph-image-file-input');
      fireEvent.change(input, { target: { files: [pngFile()] } });

      await waitFor(() => expect(onImageIngest).toHaveBeenCalledTimes(1));
      const [dataUrl, position] = onImageIngest.mock.calls[0];
      expect(dataUrl).toMatch(/^data:image\/png;base64,/);
      expect(typeof position.x).toBe('number');
      expect(typeof position.y).toBe('number');
    });

    it('ignores a non-image file selected via the picker', async () => {
      const onImageIngest = vi.fn();
      render(<GraphCanvas nodes={[]} edges={[]} onImageIngest={onImageIngest} />);
      const input = screen.getByTestId('graph-image-file-input');
      fireEvent.change(input, {
        target: { files: [new File(['hello'], 'notes.txt', { type: 'text/plain' })] },
      });
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(onImageIngest).not.toHaveBeenCalled();
    });

    it('is a no-op when the host has not wired onImageIngest', () => {
      // Guards against a null-prop crash — the toolbox item still opens the
      // native picker even when the host does not support ingest.
      render(<GraphCanvas nodes={[]} edges={[]} />);
      fireEvent.click(screen.getByRole('button', { name: /add annotation/i }));
      expect(() => fireEvent.click(screen.getByRole('button', { name: /^image$/i }))).not.toThrow();
    });
  });

  describe('clipboard paste', () => {
    it('ingests a pasted image at the viewport centre', async () => {
      const onImageIngest = vi.fn();
      render(<GraphCanvas nodes={[]} edges={[]} onImageIngest={onImageIngest} />);
      const file = pngFile();
      const event = new Event('paste', { bubbles: true, cancelable: true });
      event.clipboardData = { items: [{ type: 'image/png', getAsFile: () => file }] };
      document.dispatchEvent(event);

      await waitFor(() => expect(onImageIngest).toHaveBeenCalledTimes(1));
      expect(onImageIngest.mock.calls[0][0]).toMatch(/^data:image\/png;base64,/);
    });

    it('ignores a paste with no image item (plain text copy)', async () => {
      const onImageIngest = vi.fn();
      render(<GraphCanvas nodes={[]} edges={[]} onImageIngest={onImageIngest} />);
      const event = new Event('paste', { bubbles: true, cancelable: true });
      event.clipboardData = { items: [{ type: 'text/plain', getAsFile: () => null }] };
      document.dispatchEvent(event);

      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(onImageIngest).not.toHaveBeenCalled();
    });

    it('does not intercept a paste aimed at an ordinary text field', () => {
      const onImageIngest = vi.fn();
      render(
        <div>
          <input data-testid="some-input" />
          <GraphCanvas nodes={[]} edges={[]} onImageIngest={onImageIngest} />
        </div>
      );
      const file = pngFile();
      fireEvent.paste(screen.getByTestId('some-input'), {
        clipboardData: { items: [{ type: 'image/png', getAsFile: () => file }] },
      });
      expect(onImageIngest).not.toHaveBeenCalled();
    });
  });

  describe('OS file drop', () => {
    it('ingests a dropped image file instead of falling through to onDropCreateNode', async () => {
      const onImageIngest = vi.fn();
      const onDropCreateNode = vi.fn();
      render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          onImageIngest={onImageIngest}
          onDropCreateNode={onDropCreateNode}
        />
      );
      const file = pngFile();
      dispatchDrop(screen.getByTestId('pane'), {
        dataTransfer: { files: [file], getData: () => '' },
        clientX: 42,
        clientY: 84,
      });

      await waitFor(() => expect(onImageIngest).toHaveBeenCalledTimes(1));
      const [dataUrl, position] = onImageIngest.mock.calls[0];
      expect(dataUrl).toMatch(/^data:image\/png;base64,/);
      expect(position).toEqual({ x: 42, y: 84 });
      expect(onDropCreateNode).not.toHaveBeenCalled();
    });

    it('still routes a non-image (node-type) drag through onDropCreateNode', () => {
      const onImageIngest = vi.fn();
      const onDropCreateNode = vi.fn();
      render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          onImageIngest={onImageIngest}
          onDropCreateNode={onDropCreateNode}
        />
      );
      dispatchDrop(screen.getByTestId('pane'), {
        dataTransfer: {
          files: [],
          getData: (fmt) => (fmt === 'application/reactflow-nodetype' ? 'Actor' : ''),
        },
        clientX: 10,
        clientY: 20,
      });

      expect(onImageIngest).not.toHaveBeenCalled();
      expect(onDropCreateNode).toHaveBeenCalledWith('Actor', { x: 10, y: 20 });
    });
  });
});
