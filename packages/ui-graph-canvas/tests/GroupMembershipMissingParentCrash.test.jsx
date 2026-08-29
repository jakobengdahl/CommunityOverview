import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import ReactFlow, { ReactFlowProvider } from 'reactflow';

// Regression test for: a remote `group_membership_changed` op naming a group
// this client does not have (not yet arrived, or filtered out of this
// client's view) must not crash the canvas.
//
// This file deliberately does NOT mock 'reactflow' - it proves, with the
// real package, the exact failure mechanism GraphCanvas.jsx's membership
// branch used to be able to trigger: ReactFlow v11 throws
// `Parent node <id> not found` from inside its own store update
// (updateAbsoluteNodePositions), synchronously during the passive-effect
// flush that applies a controlled `nodes` prop - outside every React error
// boundary. It renders a bare <ReactFlow>, not the full GraphCanvas: under
// jsdom, GraphCanvasInner's unrelated viewport/fitView effects never settle
// (no real layout, no DOMMatrixReadOnly) regardless of this bug - a
// pre-existing environment limitation, not something this fix touches, and
// the reason every other test in this suite mocks reactflow. Driving
// ReactFlow's own controlled-nodes update path directly still exercises the
// exact code that throws, without depending on that unrelated machinery.
//
// See GraphCanvasRemote.test.jsx for the paired test that GraphCanvas's own
// membership-op handler is what keeps this dangling-parentId shape from
// ever reaching ReactFlow.
describe('a node with a parentId naming a missing group (real reactflow)', () => {
  it('crashes ReactFlow - reproducing the bug this fix guards against', () => {
    const nodes = [{ id: 'n1', position: { x: 0, y: 0 }, data: { label: 'N1' }, parentId: 'grp-missing' }];
    let caught = null;
    try {
      render(
        <ReactFlowProvider>
          <ReactFlow nodes={nodes} edges={[]} />
        </ReactFlowProvider>
      );
    } catch (e) {
      caught = e;
    }
    expect(caught).not.toBeNull();
    expect(caught.message).toMatch(/Parent node grp-missing not found/);
  });

  it('does not crash when no node carries a parentId to a missing group - the guarded shape', () => {
    const nodes = [{ id: 'n1', position: { x: 0, y: 0 }, data: { label: 'N1' } }];
    expect(() =>
      render(
        <ReactFlowProvider>
          <ReactFlow nodes={nodes} edges={[]} />
        </ReactFlowProvider>
      )
    ).not.toThrow();
  });
});
