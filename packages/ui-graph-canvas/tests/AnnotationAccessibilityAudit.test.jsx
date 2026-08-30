import { describe, it, expect } from 'vitest';
import { useState, useCallback } from 'react';
import { render, screen, act } from '@testing-library/react';
import ReactFlow, { ReactFlowProvider, applyNodeChanges } from 'reactflow';
import NoteNode from '../src/components/NoteNode';
import LabelNode from '../src/components/LabelNode';
import ArrowNode from '../src/components/ArrowNode';
import GroupNode from '../src/components/GroupNode';
import GenericAnnotationNode from '../src/components/GenericAnnotationNode';
import FreehandAnnotationNode from '../src/components/FreehandAnnotationNode';

// task-annotation-accessibility-controls-audit: these tests exercise the REAL
// `reactflow` package (not the mocked one every other file in this suite
// uses — see GroupMembershipMissingParentCrash.test.jsx's doc comment for why
// that mocking exists elsewhere). GraphCanvas.jsx never sets `nodesFocusable`,
// `disableKeyboardA11y` or a node's `ariaLabel` field, so this proves what
// ReactFlow's own untouched defaults actually produce in this app's wiring —
// evidence the audit doc cites by file:line, not an assumption read off
// ReactFlow's source. This file intentionally does NOT prove real
// screen-reader or touch-hardware behaviour (see the audit doc's
// UNTESTABLE-HERE rows, deferred to task-annotation-manual-accessibility-touch-acceptance).

const nodeTypes = {
  note: NoteNode,
  label: LabelNode,
  arrow: ArrowNode,
  group: GroupNode,
  text: GenericAnnotationNode,
  icon: GenericAnnotationNode,
  freehand: FreehandAnnotationNode,
};

// A minimal controlled-nodes harness (applyNodeChanges is ReactFlow's own
// reducer, the same one useNodesState wraps) so onNodesChange actually
// updates position/selection the way the real app's controlled `nodes` prop
// does, rather than a fire-and-forget callback nothing reads back.
function Harness({ initialNodes, onNodes }) {
  const [nodes, setNodes] = useState(initialNodes);
  const onNodesChange = useCallback(
    (changes) => {
      setNodes((nds) => {
        const next = applyNodeChanges(changes, nds);
        onNodes?.(next);
        return next;
      });
    },
    [onNodes]
  );
  return (
    <ReactFlowProvider>
      <ReactFlow nodes={nodes} edges={[]} nodeTypes={nodeTypes} onNodesChange={onNodesChange} />
    </ReactFlowProvider>
  );
}

describe('annotation nodes under real ReactFlow (no mock) — role/name/keyboard', () => {
  it.each([
    ['note', 'note-1', {}],
    ['label', 'label-1', {}],
    ['arrow', 'arrow-1', {}],
    ['group', 'group-1', {}],
    ['text', 'text-1', {}],
    ['icon', 'icon-1', {}],
    ['freehand', 'freehand-1', {}],
  ])(
    '%s: gets role="button"/tabIndex=0 from ReactFlow defaults but NO aria-label (GraphCanvas.jsx never sets node.ariaLabel)',
    (type, id, data) => {
      render(<Harness initialNodes={[{ id, type, position: { x: 0, y: 0 }, data }]} />);
      const wrapper = screen.getByTestId(`rf__node-${id}`);
      expect(wrapper.getAttribute('role')).toBe('button');
      expect(wrapper.getAttribute('tabindex')).toBe('0');
      // The concrete, testable form of "no meaningful screen-reader name is
      // constructed for this annotation": ReactFlow only ever writes
      // aria-label from `node.ariaLabel`, and no v1 overlay builder
      // (overlayToFlowNode / createAnnotation in GraphCanvas.jsx) sets that
      // field for any kind.
      expect(wrapper.hasAttribute('aria-label')).toBe(false);
    }
  );

  it('note: a keyboard-invoked contextmenu (Shift+F10/Menu key) on the FOCUSED node wrapper does not open the annotation property menu', () => {
    render(
      <Harness
        initialNodes={[{ id: 'note-1', type: 'note', position: { x: 0, y: 0 }, data: {} }]}
      />
    );
    const wrapper = screen.getByTestId('rf__node-note-1');
    wrapper.focus();
    expect(document.activeElement).toBe(wrapper);
    // This is exactly what a real Shift+F10/context-menu-key press dispatches:
    // a `contextmenu` event whose target is the currently focused element.
    act(() => {
      wrapper.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }));
    });
    // NoteNode's own onContextMenu handler is bound on `.graph-note-node`, a
    // DESCENDANT of this focused wrapper — a bubbling event whose target is
    // an ancestor never reaches a handler on a deeper-nested child, so the
    // annotation's menu never opens from this input.
    expect(document.querySelector('.graph-annotation-context-menu')).toBeNull();
  });

  it('note: the identical contextmenu event, dispatched on the inner content div a real right-click/long-press targets, DOES open the menu', () => {
    render(
      <Harness
        initialNodes={[{ id: 'note-1', type: 'note', position: { x: 0, y: 0 }, data: {} }]}
      />
    );
    const inner = document.querySelector('.graph-note-node');
    expect(inner).not.toBeNull();
    act(() => {
      inner.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }));
    });
    expect(document.querySelector('.graph-annotation-context-menu')).not.toBeNull();
  });

  it('text (GenericAnnotationNode): same keyboard-contextmenu-vs-pointer-contextmenu split as note', () => {
    render(
      <Harness
        initialNodes={[
          { id: 'text-1', type: 'text', position: { x: 0, y: 0 }, data: { text: 'Hi' } },
        ]}
      />
    );
    const wrapper = screen.getByTestId('rf__node-text-1');
    wrapper.focus();
    act(() => {
      wrapper.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }));
    });
    expect(document.querySelector('.graph-annotation-context-menu')).toBeNull();

    const inner = document.querySelector('.kind-text');
    act(() => {
      inner.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true }));
    });
    expect(document.querySelector('.graph-annotation-context-menu')).not.toBeNull();
  });

  it("label: ArrowRight nudges a SELECTED, focused node by 5px (ReactFlow's default step) via its own built-in keyboard handling (no app code involved)", () => {
    let latest = null;
    render(
      <Harness
        initialNodes={[
          { id: 'label-1', type: 'label', position: { x: 100, y: 100 }, data: {}, selected: true },
        ]}
        onNodes={(nds) => {
          latest = nds;
        }}
      />
    );
    const wrapper = screen.getByTestId('rf__node-label-1');
    wrapper.focus();
    act(() => {
      wrapper.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, cancelable: true })
      );
    });
    const moved = latest.find((n) => n.id === 'label-1');
    expect(moved.position.x).toBe(105);
    expect(moved.position.y).toBe(100);
  });

  it('label: Enter on a focused, UNselected node selects it (ReactFlow default; no annotation-authored keyboard path exists for this)', () => {
    let latest = null;
    render(
      <Harness
        initialNodes={[{ id: 'label-1', type: 'label', position: { x: 0, y: 0 }, data: {} }]}
        onNodes={(nds) => {
          latest = nds;
        }}
      />
    );
    const wrapper = screen.getByTestId('rf__node-label-1');
    wrapper.focus();
    act(() => {
      wrapper.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
      );
    });
    expect(latest.find((n) => n.id === 'label-1').selected).toBe(true);
  });

  it('label: Enter/double-click-only text editing has no keyboard equivalent — Enter on a selected, focused label only (re)selects, it never opens the inline editor', () => {
    render(
      <Harness
        initialNodes={[
          {
            id: 'label-1',
            type: 'label',
            position: { x: 0, y: 0 },
            data: { text: 'Q3' },
            selected: true,
          },
        ]}
      />
    );
    const wrapper = screen.getByTestId('rf__node-label-1');
    wrapper.focus();
    act(() => {
      wrapper.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
      );
    });
    // The editor is a controlled <input class="graph-label-input">; its
    // absence after Enter is the concrete evidence that only
    // onDoubleClick={startEditing} (LabelNode.jsx) reaches edit mode.
    expect(document.querySelector('.graph-label-input')).toBeNull();
  });
});

// The "computed accessible name per kind" evidence lives in
// AnnotationAccessibleNameContent.test.jsx: real ReactFlow marks every node
// `visibility: hidden` under jsdom (no real layout -> `node.width/height`
// never resolve, see updateNodeDimensions's `DOMMatrixReadOnly` dependency,
// which jsdom does not implement — the same limitation
// GroupMembershipMissingParentCrash.test.jsx's doc comment names), and an
// accname computation correctly treats hidden content as unnamed — so that
// file mocks reactflow (this suite's usual pattern) to test the actual
// per-kind rendered content in isolation from that jsdom artifact.
