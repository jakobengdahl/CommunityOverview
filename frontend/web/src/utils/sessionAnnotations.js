// Pure transforms between the server-side annotation model (design 3.1) and the
// canvas-facing shapes GraphCanvas emits/consumes. Shared by the shared-session
// lifecycle (useSharedSession) and by App's incremental-op and snapshot paths.

// Group boxes persist inside the generic server-side annotation list as
// `kind: "group"` (design 3.1). These two helpers translate between that
// server shape and the {groups, parentIds} shape the canvas round-trips.
export function annotationsToGroups(annotations) {
  const groups = [];
  const parentIds = {};
  for (const a of annotations || []) {
    if (a?.kind !== 'group') continue;
    groups.push({
      id: a.id,
      label: a.label || 'Group',
      position: a.position || { x: 0, y: 0 },
      style: a.size ? { width: a.size.w, height: a.size.h } : undefined,
      color: a.color,
    });
    for (const m of a.member_node_ids || []) parentIds[m] = a.id;
  }
  return { groups, parentIds };
}

export function groupsToAnnotations(viewGroups, parentIds) {
  const membersByGroup = {};
  for (const [nodeId, groupId] of Object.entries(parentIds || {})) {
    (membersByGroup[groupId] = membersByGroup[groupId] || []).push(nodeId);
  }
  return (viewGroups || []).map((g) => ({
    id: g.id,
    kind: 'group',
    position: g.position || { x: 0, y: 0 },
    label: g.label || '',
    color: g.color,
    size: g.style ? { w: g.style.width, h: g.style.height } : undefined,
    member_node_ids: membersByGroup[g.id] || [],
  }));
}

// Note/label/arrow annotations round-trip between the server annotation model
// (design 3.1) and the canvas-shape overlay descriptors the GraphCanvas emits
// (via onSaveView) and consumes (via annotationsToRestore). Groups keep their
// own translation above; these cover the free-floating overlays from step 5.
export function annotationsToOverlays(annotations) {
  const out = [];
  for (const a of annotations || []) {
    if (a?.kind === 'note') {
      out.push({
        id: a.id,
        kind: 'note',
        position: a.position || { x: 0, y: 0 },
        text: a.text || '',
        color: a.color,
        fontSize: a.fontSize,
        size: a.size,
      });
    } else if (a?.kind === 'label') {
      out.push({
        id: a.id,
        kind: 'label',
        position: a.position || { x: 0, y: 0 },
        text: a.text || '',
        color: a.style?.color,
        fontSize: a.style?.fontSize,
      });
    } else if (a?.kind === 'arrow') {
      const from = a.from || a.position || { x: 0, y: 0 };
      const to = a.to || { x: from.x + 160, y: from.y };
      const overlay = {
        id: a.id,
        kind: 'arrow',
        position: { x: from.x, y: from.y },
        dx: to.x - from.x,
        dy: to.y - from.y,
        color: a.style?.color,
        startArrow: a.startArrow ?? false,
        endArrow: a.endArrow ?? true,
      };
      if (a.startAnchor) overlay.startAnchor = a.startAnchor;
      if (a.endAnchor) overlay.endAnchor = a.endAnchor;
      out.push(overlay);
    }
  }
  return out;
}

export function overlaysToAnnotations(overlays) {
  return (overlays || []).map((o) => {
    if (o.kind === 'note') {
      return {
        id: o.id,
        kind: 'note',
        position: o.position || { x: 0, y: 0 },
        text: o.text || '',
        color: o.color,
        fontSize: o.fontSize,
        size: o.size,
      };
    }
    if (o.kind === 'label') {
      return {
        id: o.id,
        kind: 'label',
        position: o.position || { x: 0, y: 0 },
        text: o.text || '',
        style: { color: o.color, fontSize: o.fontSize },
      };
    }
    // arrow: store both endpoints as absolute points (design 3.1)
    const from = o.position || { x: 0, y: 0 };
    const dx = o.dx ?? 160;
    const dy = o.dy ?? 0;
    const ann = {
      id: o.id,
      kind: 'arrow',
      position: { x: from.x, y: from.y },
      from: { x: from.x, y: from.y },
      to: { x: from.x + dx, y: from.y + dy },
      style: { color: o.color },
      startArrow: o.startArrow ?? false,
      endArrow: o.endArrow ?? true,
    };
    if (o.startAnchor) ann.startAnchor = o.startAnchor;
    if (o.endAnchor) ann.endAnchor = o.endAnchor;
    return ann;
  });
}
