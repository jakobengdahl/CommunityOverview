import { describe, it, expect } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import ReactFlow, { ReactFlowProvider } from 'reactflow';
import NoteNode from '../src/components/NoteNode';
import GroupNode from '../src/components/GroupNode';

// task-annotation-accessible-shared-controls: the real-reactflow (unmocked)
// half of the Shift+F10/Menu-key reachability fix — mirrors
// AnnotationAccessibilityAudit.test.jsx's own Harness exactly (same
// nodeTypes-driven <ReactFlow>, no mock), since that file already proved,
// against real ReactFlow node wrappers, the OLD half of this claim that is
// still true and unchanged: a bare `contextmenu` event dispatched on the
// focused node wrapper itself never reaches any kind's own `onContextMenu`
// handler (bound on a DOM descendant). GraphCanvas.jsx does not try to make
// that dispatch work; its keydown handler instead finds and clicks the
// visible Edit button — `event.target.closest('.react-flow__node')
// ?.querySelector('.annotation-edit-trigger')` — the one visible, working
// entry point task-annotation-responsive-bottom-toolbox already built for
// five kinds and this task adds to the sixth (`group`). This file proves
// that DOM lookup actually resolves and the button actually opens the menu,
// against a real, focused, `.react-flow__node` wrapper — not a description
// of the code, an executed one. GraphCanvas.jsx's own keydown handler is not
// mounted here (it is a few lines of key-string comparison around this exact
// lookup — low risk, not worth a full GraphCanvas mount to re-prove).
function Harness({ initialNodes }) {
  return (
    <ReactFlowProvider>
      <ReactFlow
        nodes={initialNodes}
        edges={[]}
        nodeTypes={{ note: NoteNode, group: GroupNode }}
        onNodesChange={() => {}}
      />
    </ReactFlowProvider>
  );
}

describe('Shift+F10/Menu-key reachability mechanism (real reactflow, unmocked)', () => {
  it('finds and can activate the visible Edit button from the focused node wrapper, opening the property menu with no pointer event involved', () => {
    render(
      <Harness
        initialNodes={[
          {
            id: 'note-1',
            type: 'note',
            position: { x: 0, y: 0 },
            data: { text: 'x' },
            selected: true,
          },
        ]}
      />
    );
    const wrapper = screen.getByTestId('rf__node-note-1');
    wrapper.focus();
    expect(document.activeElement).toBe(wrapper);
    const trigger = wrapper.querySelector('.annotation-edit-trigger');
    expect(trigger).not.toBeNull();
    act(() => {
      trigger.click();
    });
    expect(document.querySelector('.graph-annotation-context-menu')).not.toBeNull();
  });

  it('the same lookup finds group\'s new Edit button too — group had none until this task, and was the one kind the audit named as still missing the whole "keyboard way in" row', () => {
    render(
      <Harness
        initialNodes={[
          {
            id: 'group-1',
            type: 'group',
            position: { x: 0, y: 0 },
            data: { label: 'G' },
            selected: true,
          },
        ]}
      />
    );
    const wrapper = screen.getByTestId('rf__node-group-1');
    wrapper.focus();
    const trigger = wrapper.querySelector('.annotation-edit-trigger');
    expect(trigger).not.toBeNull();
    act(() => {
      trigger.click();
    });
    expect(document.querySelector('.graph-annotation-context-menu')).not.toBeNull();
  });

  it('an unselected node still has no Edit button — the button appears only once selected, same gate every kind already used for the touch/click path', () => {
    render(
      <Harness
        initialNodes={[
          { id: 'note-1', type: 'note', position: { x: 0, y: 0 }, data: { text: 'x' } },
        ]}
      />
    );
    const wrapper = screen.getByTestId('rf__node-note-1');
    expect(wrapper.querySelector('.annotation-edit-trigger')).toBeNull();
  });
});
