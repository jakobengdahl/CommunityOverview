import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { GraphCanvas } from '../src/index';

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn() }));

vi.mock('reactflow', () => {
  const MockReactFlow = ({ children }) => <div data-testid="react-flow">{children}</div>;
  return {
    default: MockReactFlow,
    ReactFlow: MockReactFlow,
    ReactFlowProvider: ({ children }) => <div>{children}</div>,
    useNodesState: (initial) => [initial || [], hoisted.setNodes, vi.fn()],
    useEdgesState: (initial) => [initial || [], vi.fn(), vi.fn()],
    useReactFlow: () => ({
      fitView: vi.fn(),
      getNodes: () => [],
      getEdges: () => [],
      setNodes: hoisted.setNodes,
      setEdges: vi.fn(),
      screenToFlowPosition: ({ x, y }) => ({ x, y }),
      setCenter: vi.fn(),
      getViewport: () => ({ x: 0, y: 0, zoom: 1 }),
    }),
    useOnSelectionChange: () => {},
    Background: () => null,
    Controls: () => null,
    MiniMap: () => null,
    NodeResizer: () => null,
    SelectionMode: { Partial: 'partial' },
  };
});

// Several effects call setNodes (including the input-sync effect that resets to
// the empty `nodes` prop). Apply each captured updater to a fresh copy of `seed`
// independently and return the first result that satisfies `predicate`, isolating
// the remote-apply updater under test from the others.
function findResult(seed, predicate) {
  for (const call of hoisted.setNodes.mock.calls) {
    if (typeof call[0] !== 'function') continue;
    let result;
    try {
      result = call[0](seed.map((n) => ({ ...n })));
    } catch {
      continue;
    }
    if (Array.isArray(result) && predicate(result)) return result;
  }
  return null;
}

describe('GraphCanvas remote apply (design step 6)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('applies a remote absolute position to a matching node', () => {
    const onRemotePositionsApplied = vi.fn();
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remotePositions={{ 'node-a': { x: 42, y: 99 } }}
        onRemotePositionsApplied={onRemotePositionsApplied}
      />
    );
    const seed = [{ id: 'node-a', type: 'custom', position: { x: 0, y: 0 } }];
    const result = findResult(seed, (r) => r.find((n) => n.id === 'node-a')?.position.x === 42);
    expect(result.find((n) => n.id === 'node-a').position).toEqual({ x: 42, y: 99 });
    expect(onRemotePositionsApplied).toHaveBeenCalled();
  });

  it('applies a remote position once its node mounts, even if it arrived first', () => {
    // Race in the backlog: node_moved for a node reaches the client before the
    // paired nodes_added has resolved its async node-details fetch, so the
    // position effect fires while the node doesn't exist in ReactFlow yet.
    const { rerender } = render(
      <GraphCanvas nodes={[]} edges={[]} remotePositions={{ 'late-node': { x: 33, y: 44 } }} />
    );
    // The node now mounts (its nodes_added resolved) and the parent has since
    // cleared remotePositions, as App.jsx does once notified. Only the mount
    // itself should be enough to apply the position that arrived too early.
    hoisted.setNodes.mockClear();
    rerender(
      <GraphCanvas
        nodes={[{ id: 'late-node', type: 'Actor', name: 'Late' }]}
        edges={[]}
        remotePositions={null}
      />
    );
    const seed = [{ id: 'late-node', type: 'custom', position: { x: 0, y: 0 } }];
    const result = findResult(seed, (r) => r.find((n) => n.id === 'late-node')?.position.x === 33);
    expect(result?.find((n) => n.id === 'late-node').position).toEqual({ x: 33, y: 44 });
  });

  it('applies a grouped node position verbatim (already relative — no re-offset)', () => {
    // The emit side stores n.position, which ReactFlow keeps relative to the
    // parent for a grouped node, so the apply side must not subtract the parent
    // position again.
    render(<GraphCanvas nodes={[]} edges={[]} remotePositions={{ child: { x: 50, y: 20 } }} />);
    const seed = [
      { id: 'grp', type: 'group', position: { x: 100, y: 100 } },
      { id: 'child', type: 'custom', parentId: 'grp', position: { x: 0, y: 0 } },
    ];
    const result = findResult(seed, (r) => r.find((n) => n.id === 'child')?.position.x === 50);
    expect(result.find((n) => n.id === 'child').position).toEqual({ x: 50, y: 20 });
  });

  it('upserts a remote overlay annotation', () => {
    const onApplied = vi.fn();
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteAnnotationOps={[
          {
            action: 'upsert-overlay',
            overlay: { id: 'note-x', kind: 'note', position: { x: 1, y: 2 }, text: 'hi' },
          },
        ]}
        onRemoteAnnotationsApplied={onApplied}
      />
    );
    const result = findResult([], (r) => r.some((n) => n.id === 'note-x' && n.type === 'note'));
    expect(result.some((n) => n.id === 'note-x' && n.type === 'note')).toBe(true);
    expect(onApplied).toHaveBeenCalled();
  });

  it('applies every op in a queued burst (no coalescing)', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteAnnotationOps={[
          {
            action: 'upsert-overlay',
            overlay: { id: 'note-1', kind: 'note', position: { x: 0, y: 0 }, text: 'a' },
          },
          {
            action: 'upsert-overlay',
            overlay: { id: 'note-2', kind: 'label', position: { x: 0, y: 0 }, text: 'b' },
          },
        ]}
      />
    );
    expect(findResult([], (r) => r.some((n) => n.id === 'note-1'))).toBeTruthy();
    expect(findResult([], (r) => r.some((n) => n.id === 'note-2'))).toBeTruthy();
  });

  it('deletes a remote annotation and un-parents its children', () => {
    render(
      <GraphCanvas nodes={[]} edges={[]} remoteAnnotationOps={[{ action: 'delete', id: 'grp' }]} />
    );
    const seed = [
      { id: 'grp', type: 'group', position: { x: 0, y: 0 } },
      { id: 'child', type: 'custom', parentId: 'grp', position: { x: 0, y: 0 } },
    ];
    const result = findResult(
      seed,
      (r) => !r.some((n) => n.id === 'grp') && r.some((n) => n.id === 'child')
    );
    expect(result.some((n) => n.id === 'grp')).toBe(false);
    expect(result.find((n) => n.id === 'child').parentId).toBeUndefined();
  });

  it('injects remoteSelections into the matching node data (step 7)', () => {
    render(
      <GraphCanvas
        nodes={[{ id: 'node-a', type: 'Actor', name: 'A' }]}
        edges={[]}
        remoteSelections={{ 'node-a': { clientId: 'c2', color: '#e6194b', displayName: 'Ada' } }}
      />
    );
    const result = findResult([], (r) =>
      r.some((n) => n.id === 'node-a' && n.data?.remoteSelection?.displayName === 'Ada')
    );
    expect(result.find((n) => n.id === 'node-a').data.remoteSelection).toEqual({
      clientId: 'c2',
      color: '#e6194b',
      displayName: 'Ada',
    });
  });

  it('stamps a live remote edit lease onto an annotation node and refuses local dragging (task-annotation-exclusive-edit-leases)', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteLeases={{ 'note-1': { clientId: 'c2', color: '#e6194b', displayName: 'Ada' } }}
      />
    );
    const seed = [
      { id: 'note-1', type: 'note', position: { x: 0, y: 0 }, data: {}, draggable: true },
    ];
    const result = findResult(
      seed,
      (r) => r.find((n) => n.id === 'note-1')?.data?.remoteLease?.displayName === 'Ada'
    );
    const note = result.find((n) => n.id === 'note-1');
    expect(note.data.remoteLease).toEqual({
      clientId: 'c2',
      color: '#e6194b',
      displayName: 'Ada',
    });
    expect(note.draggable).toBe(false);
  });

  it('a mere remote selection claim (no edit lease) never blocks local dragging (task-annotation-exclusive-edit-leases)', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteSelections={{ 'note-1': { clientId: 'c2', color: '#e6194b', displayName: 'Ada' } }}
      />
    );
    const seed = [
      { id: 'note-1', type: 'note', position: { x: 0, y: 0 }, data: {}, draggable: true },
    ];
    const result = findResult(
      seed,
      (r) => r.find((n) => n.id === 'note-1')?.data?.remoteSelection?.displayName === 'Ada'
    );
    const note = result.find((n) => n.id === 'note-1');
    expect(note.data.remoteSelection).toEqual({
      clientId: 'c2',
      color: '#e6194b',
      displayName: 'Ada',
    });
    expect(note.draggable).toBe(true);
  });

  it('leaves a graph ("custom") node untouched by the annotation remote-claim effect', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteSelections={{
          'graph-node-a': { clientId: 'c2', color: '#e6194b', displayName: 'Ada' },
        }}
      />
    );
    const seed = [
      { id: 'graph-node-a', type: 'custom', position: { x: 0, y: 0 }, data: {}, draggable: true },
    ];
    // The annotation-claim effect's updater must be a no-op for a 'custom' node
    // (graph nodes get their remoteSelection marker from the separate
    // reactFlowNodes memo, not this effect) — applying every captured updater
    // to the seed must never flip draggable to false via this path.
    for (const call of hoisted.setNodes.mock.calls) {
      if (typeof call[0] !== 'function') continue;
      let result;
      try {
        result = call[0](seed.map((n) => ({ ...n, data: { ...n.data } })));
      } catch {
        continue;
      }
      if (!Array.isArray(result)) continue;
      const node = result.find((n) => n.id === 'graph-node-a');
      if (node) expect(node.draggable).not.toBe(false);
    }
  });

  it('restores dragging once a remote edit lease is released', () => {
    const { rerender } = render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteLeases={{ 'note-1': { clientId: 'c2', color: '#e6194b', displayName: 'Ada' } }}
      />
    );
    hoisted.setNodes.mockClear();
    rerender(<GraphCanvas nodes={[]} edges={[]} remoteLeases={{}} />);
    const seed = [
      {
        id: 'note-1',
        type: 'note',
        position: { x: 0, y: 0 },
        data: { remoteLease: { clientId: 'c2', color: '#e6194b', displayName: 'Ada' } },
        draggable: false,
      },
    ];
    const result = findResult(
      seed,
      (r) => r.find((n) => n.id === 'note-1')?.data?.remoteLease == null
    );
    const note = result.find((n) => n.id === 'note-1');
    expect(note.data.remoteLease).toBeNull();
    expect(note.draggable).toBe(true);
  });

  it('keeps an anchored arrow non-draggable even once its remote edit lease clears (isArrowAnchored still applies)', () => {
    const { rerender } = render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteLeases={{ 'arrow-1': { clientId: 'c2', color: '#e6194b', displayName: 'Ada' } }}
      />
    );
    hoisted.setNodes.mockClear();
    rerender(<GraphCanvas nodes={[]} edges={[]} remoteLeases={{}} />);
    const seed = [
      {
        id: 'arrow-1',
        type: 'arrow',
        position: { x: 0, y: 0 },
        data: {
          startAnchor: 'graph-node-a',
          remoteLease: { clientId: 'c2', color: '#e6194b', displayName: 'Ada' },
        },
        draggable: false,
      },
    ];
    const result = findResult(
      seed,
      (r) => r.find((n) => n.id === 'arrow-1')?.data?.remoteLease == null
    );
    expect(result.find((n) => n.id === 'arrow-1').draggable).toBe(false);
  });

  it('reassigns membership from a remote group_membership_changed', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteAnnotationOps={[{ action: 'membership', groupId: 'grp', members: ['a'] }]}
      />
    );
    const seed = [
      { id: 'grp', type: 'group', position: { x: 0, y: 0 } },
      { id: 'a', type: 'custom', position: { x: 0, y: 0 } },
      { id: 'b', type: 'custom', parentId: 'grp', position: { x: 0, y: 0 } },
    ];
    const result = findResult(seed, (r) => r.find((n) => n.id === 'a')?.parentId === 'grp');
    expect(result.find((n) => n.id === 'a').parentId).toBe('grp');
    expect(result.find((n) => n.id === 'b').parentId).toBeUndefined();
  });

  // Regression test: a membership op naming a group this client does not have
  // (not yet arrived, or filtered out of this client's view) used to set
  // parentId to that missing id anyway. Real ReactFlow throws
  // `Parent node <id> not found` from inside its own store update for that
  // shape - outside every React error boundary - crashing the whole canvas
  // (see GroupMembershipMissingParentCrash.test.jsx, which proves that with
  // real, unmocked reactflow). The handler must leave the node list
  // unchanged instead of introducing that dangling parentId.
  it('drops a membership op naming a group not present on this client', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteAnnotationOps={[{ action: 'membership', groupId: 'grp-missing', members: ['a'] }]}
      />
    );
    const seed = [{ id: 'a', type: 'custom', position: { x: 0, y: 0 } }];
    // Check every updater this render triggered, not just the first that
    // "succeeds" (several effects call setNodes) - the invariant under test
    // is that NONE of them ever introduces a parentId naming an absent node,
    // which is exactly the shape that crashes real ReactFlow.
    const introducedDanglingParent = hoisted.setNodes.mock.calls.some((call) => {
      if (typeof call[0] !== 'function') return false;
      let result;
      try {
        result = call[0](seed.map((n) => ({ ...n })));
      } catch {
        return false;
      }
      return Array.isArray(result) && result.some((n) => n.parentId === 'grp-missing');
    });
    expect(introducedDanglingParent).toBe(false);
  });

  // Regression test for the delete->undo round trip: removeGroupKeepChildren
  // (GroupNode.jsx) un-parents a group's members on delete by ADDING the
  // group's origin to each member's position (parent-relative -> absolute).
  // Undoing that delete replays it as a remote upsert-group op that re-parents
  // the members. Before this fix it did so without SUBTRACTING the group's
  // origin back out, so every member reappeared shifted by exactly the
  // group's (x, y) - and nothing ever corrected it, so the shift was what the
  // next autosave persisted. A group at (400, 300) with a member whose
  // parent-relative position was (50, 20) - i.e. (450, 320) once un-parented -
  // must land back at exactly (50, 20) once the group is restored.
  it('un-displaces a member restored by a remote upsert-group after a delete round trip', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteAnnotationOps={[
          {
            action: 'upsert-group',
            group: { id: 'grp', label: 'Team', position: { x: 400, y: 300 } },
            members: ['n1'],
          },
        ]}
      />
    );
    // What the canvas holds right after removeGroupKeepChildren ran: the
    // member has no parent and its position is already absolute.
    const seed = [{ id: 'n1', type: 'custom', position: { x: 450, y: 320 } }];
    const result = findResult(seed, (r) => r.find((n) => n.id === 'n1')?.parentId === 'grp');
    const restored = result.find((n) => n.id === 'n1');
    expect(restored.parentId).toBe('grp');
    // Position-preserving: back to the parent-relative coordinates it held
    // before the delete, not still the displaced absolute ones.
    expect(restored.position).toEqual({ x: 50, y: 20 });
  });

  // A redundant upsert-group (e.g. the group being dragged, which echoes a
  // position change with the same membership) must not re-subtract the
  // group's new origin from a member that is already parented here - that
  // would displace it in the opposite direction on every such echo. The
  // group's position in the op deliberately differs from its seeded position
  // so the guard is actually exercised: with the same position in both, the
  // add-then-subtract math cancels out whether or not the guard runs, which
  // would let this test pass even with the guard removed.
  it('leaves an already-adopted member position untouched on a redundant upsert-group', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteAnnotationOps={[
          {
            action: 'upsert-group',
            group: { id: 'grp', label: 'Renamed', position: { x: 500, y: 300 } },
            members: ['n1'],
          },
        ]}
      />
    );
    const seed = [
      { id: 'grp', type: 'group', position: { x: 400, y: 300 } },
      { id: 'n1', type: 'custom', parentId: 'grp', position: { x: 50, y: 20 } },
    ];
    const result = findResult(
      seed,
      (r) => r.find((n) => n.id === 'grp')?.data?.label === 'Renamed'
    );
    expect(result.find((n) => n.id === 'n1').position).toEqual({ x: 50, y: 20 });
  });

  // Regression test for the mirror-image gap left by PR #506: that PR fixed
  // the JOIN case in this same handler (the round-trip test above) but the
  // LEAVE branch - a member dropped from the group's membership, e.g. because
  // a label/color edit was bundled with a membership shrink in the same
  // autosave - still cleared parentId without converting the member's
  // parent-relative position back to absolute. The op's group position
  // deliberately differs from the seeded one (mirroring a group being
  // dragged in the same autosave that shrank it) so this proves the fix uses
  // the PRE-upsert group origin, not the one the op is about to apply.
  it('un-displaces a member dropped by a remote upsert-group that shrinks membership', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteAnnotationOps={[
          {
            action: 'upsert-group',
            group: { id: 'grp', label: 'Team', position: { x: 500, y: 300 } },
            members: ['n2'],
          },
        ]}
      />
    );
    const seed = [
      { id: 'grp', type: 'group', position: { x: 400, y: 300 } },
      { id: 'n1', type: 'custom', parentId: 'grp', position: { x: 50, y: 20 } },
      { id: 'n2', type: 'custom', parentId: 'grp', position: { x: 10, y: 10 } },
    ];
    const result = findResult(seed, (r) => r.find((n) => n.id === 'grp')?.data?.label === 'Team');
    const dropped = result.find((n) => n.id === 'n1');
    expect(dropped.parentId).toBeUndefined();
    // 400+50, 300+20 - the PRIOR group origin (400, 300), not the new one
    // (500, 300) the same op carries for the group that stayed.
    expect(dropped.position).toEqual({ x: 450, y: 320 });
    // The member that stayed is untouched by the leave-branch conversion.
    expect(result.find((n) => n.id === 'n2').parentId).toBe('grp');
  });

  // Regression test: unlike the two upsert-group cases above, a remote
  // group_membership_changed op did no position conversion in either
  // direction at all - masked in the normal in-app drag gesture because the
  // sender's own computeGroupPlacement already fixed the position and a
  // separate position-sync op carried it, but any membership change that
  // arrives without a paired position op (e.g. an MCP-driven membership
  // update) would displace the member exactly like the restore bug PR #506
  // fixed. This covers the JOIN direction.
  it('un-displaces a member joining via a remote group_membership_changed op', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteAnnotationOps={[{ action: 'membership', groupId: 'grp', members: ['a'] }]}
      />
    );
    const seed = [
      { id: 'grp', type: 'group', position: { x: 400, y: 300 } },
      { id: 'a', type: 'custom', position: { x: 450, y: 320 } },
    ];
    const result = findResult(seed, (r) => r.find((n) => n.id === 'a')?.parentId === 'grp');
    const joined = result.find((n) => n.id === 'a');
    expect(joined.parentId).toBe('grp');
    expect(joined.position).toEqual({ x: 50, y: 20 });
  });

  // Same gap, LEAVE direction: a member removed from op.members while still
  // parented to the group must have its parent-relative position converted
  // back to absolute when parentId is cleared.
  it('un-displaces a member leaving via a remote group_membership_changed op', () => {
    render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        remoteAnnotationOps={[{ action: 'membership', groupId: 'grp', members: [] }]}
      />
    );
    const seed = [
      { id: 'grp', type: 'group', position: { x: 400, y: 300 } },
      { id: 'a', type: 'custom', parentId: 'grp', position: { x: 50, y: 20 } },
    ];
    const result = findResult(
      seed,
      (r) => r.some((n) => n.id === 'a') && r.find((n) => n.id === 'a').parentId === undefined
    );
    const left = result.find((n) => n.id === 'a');
    expect(left.parentId).toBeUndefined();
    expect(left.position).toEqual({ x: 450, y: 320 });
  });
});

// The six v1 annotation kinds beyond note/label/group — those three are
// covered above. `frame` is excluded: no longer a separate kind (merged into
// `shape`, PR #521). Per-kind reconnect/catch-up/duplicate-suppression/
// lock-ownership audit (task-annotation-shared-session-realtime remaining_
// scope item 2): a reconnect's full-session reload (App.jsx's
// resyncFromServer) rebuilds the canvas through the same overlayToFlowNode
// translator a live 'op' event's remote-apply effect below exercises, so a
// live-delivered upsert-overlay/delete op here stands in for "the annotation-
// side application logic a reconnect's rebuild and a live op both funnel
// through" — see this suite's own module doc comment for the two-mock-
// reactflow harness this relies on. No kind-specific gap was found: every
// case below already passes against GraphCanvas's existing, kind-agnostic
// remote-op effect and remote-claim effect (both keyed on
// GENERIC_OVERLAY_TYPES/ANNOTATION_TYPES, never on a per-kind branch).
const SIX_AUDITED_KINDS = ['text', 'shape', 'icon', 'vote_dot', 'image', 'freehand'];

describe('GraphCanvas remote apply — per-kind audit (task-annotation-shared-session-realtime)', () => {
  beforeEach(() => vi.clearAllMocks());

  for (const kind of SIX_AUDITED_KINDS) {
    it(`upserts a remote overlay annotation of kind '${kind}'`, () => {
      const onApplied = vi.fn();
      render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          remoteAnnotationOps={[
            {
              action: 'upsert-overlay',
              overlay: { id: `ann-${kind}`, kind, position: { x: 1, y: 2 } },
            },
          ]}
          onRemoteAnnotationsApplied={onApplied}
        />
      );
      const result = findResult([], (r) =>
        r.some((n) => n.id === `ann-${kind}` && n.type === kind)
      );
      expect(result?.some((n) => n.id === `ann-${kind}` && n.type === kind)).toBe(true);
      expect(onApplied).toHaveBeenCalled();
    });

    // Duplicate-delivery suppression: the same annotation id delivered a
    // second time (e.g. the catch-up-boundary double-delivery
    // sessionSyncClient.js's R15 guard documents, or a retried batch) applied
    // on top of an already-hydrated node list must converge to exactly one
    // node, not two — overlayToFlowNode's upsert is filter-then-append by id,
    // so a second delivery replaces rather than duplicates. Uses rerender (as
    // the mount-race and claim-release tests above do) to chain the second
    // delivery's seed onto the first delivery's actual result, rather than
    // relying on findResult's own per-call isolation to fold a burst — see
    // that helper's doc comment on why it deliberately does not chain calls.
    it(`re-applying the same upsert-overlay op for kind '${kind}' converges to exactly one node (no duplicate)`, () => {
      const overlay = { id: `ann-${kind}`, kind, position: { x: 0, y: 0 } };
      const { rerender } = render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          remoteAnnotationOps={[{ action: 'upsert-overlay', overlay }]}
        />
      );
      const firstResult = findResult([], (r) => r.some((n) => n.id === `ann-${kind}`));
      expect(firstResult?.filter((n) => n.id === `ann-${kind}`)).toHaveLength(1);

      hoisted.setNodes.mockClear();
      rerender(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          remoteAnnotationOps={[{ action: 'upsert-overlay', overlay }]}
        />
      );
      const secondResult = findResult(firstResult, (r) => r.some((n) => n.id === `ann-${kind}`));
      expect(secondResult?.filter((n) => n.id === `ann-${kind}`)).toHaveLength(1);
    });

    // Lock ownership: a live remote edit lease on this kind is stamped and
    // blocks local dragging identically to how the note/arrow cases above
    // already prove it — this is the same GraphCanvas remote-lease effect
    // (ANNOTATION_TYPES.has(n.type)), keyed on type membership rather than a
    // per-kind branch, so every ANNOTATION_TYPES member should behave alike.
    it(`stamps a live remote edit lease onto a '${kind}' annotation and refuses local dragging`, () => {
      render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          remoteLeases={{
            [`ann-${kind}`]: { clientId: 'c2', color: '#e6194b', displayName: 'Ada' },
          }}
        />
      );
      const seed = [
        { id: `ann-${kind}`, type: kind, position: { x: 0, y: 0 }, data: {}, draggable: true },
      ];
      const result = findResult(
        seed,
        (r) => r.find((n) => n.id === `ann-${kind}`)?.data?.remoteLease?.displayName === 'Ada'
      );
      const node = result.find((n) => n.id === `ann-${kind}`);
      expect(node.data.remoteLease).toEqual({
        clientId: 'c2',
        color: '#e6194b',
        displayName: 'Ada',
      });
      expect(node.draggable).toBe(false);
    });

    it(`restores dragging for a '${kind}' annotation once its remote edit lease clears`, () => {
      const { rerender } = render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          remoteLeases={{
            [`ann-${kind}`]: { clientId: 'c2', color: '#e6194b', displayName: 'Ada' },
          }}
        />
      );
      hoisted.setNodes.mockClear();
      rerender(<GraphCanvas nodes={[]} edges={[]} remoteLeases={{}} />);
      const seed = [
        {
          id: `ann-${kind}`,
          type: kind,
          position: { x: 0, y: 0 },
          data: { remoteLease: { clientId: 'c2', color: '#e6194b', displayName: 'Ada' } },
          draggable: false,
        },
      ];
      const result = findResult(
        seed,
        (r) => r.find((n) => n.id === `ann-${kind}`)?.data?.remoteLease == null
      );
      const node = result.find((n) => n.id === `ann-${kind}`);
      expect(node.data.remoteLease).toBeNull();
      expect(node.draggable).toBe(true);
    });

    it(`deletes a remote '${kind}' annotation`, () => {
      render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          remoteAnnotationOps={[{ action: 'delete', id: `ann-${kind}` }]}
        />
      );
      const seed = [{ id: `ann-${kind}`, type: kind, position: { x: 0, y: 0 } }];
      const result = findResult(seed, (r) => !r.some((n) => n.id === `ann-${kind}`));
      expect(result?.some((n) => n.id === `ann-${kind}`)).toBe(false);
    });
  }
});
