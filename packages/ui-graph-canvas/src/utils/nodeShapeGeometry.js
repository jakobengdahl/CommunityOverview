// Local body coordinates are shared by SVG paths, pointer hits and edge rays.
const BASES = {
  rectangle: [200, 120],
  'rectangle-flat': [240, 96],
  'triangle-up': [240, 180],
  'triangle-down': [240, 180],
};

export function createBoxGeometry(width, height, radius = 0) {
  const r = Math.min(radius, width / 2, height / 2);
  return makeGeometry(width, height, r, null, 0);
}

/** A validated definition produces a descriptor; no saved scale is clamped. */
export function createNodeShapeGeometry(base, scale = 1) {
  if (!Object.hasOwn(BASES, base) || !Number.isFinite(scale) || scale < 0.1 || scale > 10) {
    throw new RangeError('Invalid node shape geometry');
  }
  const [width, height] = BASES[base].map((n) => n * scale);
  const vertices = base.startsWith('triangle')
    ? base === 'triangle-up'
      ? [
          { x: width / 2, y: 0 },
          { x: width, y: height },
          { x: 0, y: height },
        ]
      : [
          { x: 0, y: 0 },
          { x: width, y: 0 },
          { x: width / 2, y: height },
        ]
    : null;
  return Object.freeze({
    ...makeGeometry(width, height, vertices ? 0 : 8 * scale, vertices, vertices ? 28 * scale : 0),
    base,
    scale,
    bandHeight: 28 * scale,
    padding: 12 * scale,
    text: Object.freeze({ type: 0.75 * scale, title: 0.9 * scale, summary: 0.75 * scale }),
  });
}

function makeGeometry(width, height, radius, vertices, labelHeight) {
  const bodyBounds = Object.freeze({ x: 0, y: 0, width, height });
  const layoutBounds = Object.freeze({
    x: 0,
    y: -labelHeight,
    width,
    height: height + labelHeight,
  });
  if (vertices) vertices = Object.freeze(vertices.map((p) => Object.freeze(p)));
  const path = vertices
    ? `M ${vertices.map((p) => `${p.x} ${p.y}`).join(' L ')} Z`
    : `M ${radius} 0 H ${width - radius} A ${radius} ${radius} 0 0 1 ${width} ${radius} V ${height - radius} A ${radius} ${radius} 0 0 1 ${width - radius} ${height} H ${radius} A ${radius} ${radius} 0 0 1 0 ${height - radius} V ${radius} A ${radius} ${radius} 0 0 1 ${radius} 0 Z`;
  const geometry = { width, height, radius, vertices, path, bodyBounds, layoutBounds };
  return Object.freeze({
    ...geometry,
    contains: (point) => contains(geometry, point),
    anchor: (direction) => anchor(geometry, direction),
  });
}

function cross(a, b) {
  return a.x * b.y - a.y * b.x;
}

function contains(g, p) {
  if (p.x < 0 || p.y < 0 || p.x > g.width || p.y > g.height) return false;
  if (g.vertices) {
    return g.vertices.every((a, i) => {
      const b = g.vertices[(i + 1) % g.vertices.length];
      return cross({ x: b.x - a.x, y: b.y - a.y }, { x: p.x - a.x, y: p.y - a.y }) >= -1e-8;
    });
  }
  const dx = Math.max(g.radius - p.x, 0, p.x - (g.width - g.radius));
  const dy = Math.max(g.radius - p.y, 0, p.y - (g.height - g.radius));
  return dx * dx + dy * dy <= g.radius * g.radius + 1e-8;
}

function anchor(g, direction) {
  let { x: dx, y: dy } = direction;
  if (dx === 0 && dy === 0) dy = -1;
  const length = Math.hypot(dx, dy);
  dx /= length;
  dy /= length;
  const origin = { x: g.width / 2, y: g.height / 2 };
  const hits = [];
  function segment(a, b) {
    const edge = { x: b.x - a.x, y: b.y - a.y };
    const denominator = cross({ x: dx, y: dy }, edge);
    if (Math.abs(denominator) < 1e-12) return;
    const delta = { x: a.x - origin.x, y: a.y - origin.y };
    const t = cross(delta, edge) / denominator;
    const u = cross(delta, { x: dx, y: dy }) / denominator;
    if (t > 0 && u >= -1e-8 && u <= 1 + 1e-8) {
      const n = Math.hypot(edge.x, edge.y);
      hits.push({ t, normal: { x: edge.y / n || 0, y: -edge.x / n || 0 } });
    }
  }
  const { width: w, height: h, radius: r } = g;
  if (g.vertices) {
    g.vertices.forEach((p, i) => segment(p, g.vertices[(i + 1) % 3]));
  } else {
    segment({ x: r, y: 0 }, { x: w - r, y: 0 });
    segment({ x: w, y: r }, { x: w, y: h - r });
    segment({ x: w - r, y: h }, { x: r, y: h });
    segment({ x: 0, y: h - r }, { x: 0, y: r });
    if (r) {
      for (const [cx, cy, signX, signY] of [
        [r, r, -1, -1],
        [w - r, r, 1, -1],
        [w - r, h - r, 1, 1],
        [r, h - r, -1, 1],
      ]) {
        const ox = origin.x - cx;
        const oy = origin.y - cy;
        const b = ox * dx + oy * dy;
        const discriminant = b * b - (ox * ox + oy * oy - r * r);
        if (discriminant < 0) continue;
        for (const t of [-b - Math.sqrt(discriminant), -b + Math.sqrt(discriminant)]) {
          const nx = origin.x + t * dx - cx;
          const ny = origin.y + t * dy - cy;
          if (t > 0 && nx * signX >= -1e-8 && ny * signY >= -1e-8) {
            hits.push({ t, normal: { x: nx / r, y: ny / r } });
          }
        }
      }
    }
  }
  const hit = hits.reduce((best, next) => (next.t < best.t ? next : best));
  return { x: origin.x + hit.t * dx, y: origin.y + hit.t * dy, normal: hit.normal };
}
