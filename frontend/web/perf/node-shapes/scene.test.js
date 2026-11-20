// covers: frontend/web/perf/node-shapes/scene.js, frontend/web/perf/node-shapes/stats.js
import { describe, it, expect } from 'vitest';
import { createNodeShapeResolver } from '@community-graph/ui-graph-canvas';
import { SCENE_SCALES, SCENE_TYPE_BASES, createNodeShapeScene } from './scene';
import { BUDGET, NOT_COMPARABLE, compareScenes, markdownReport, summarize } from './stats';

describe('node shape performance scenes', () => {
  it.each(SCENE_SCALES)(
    'are fixed, mix every base at scale %s and differ from the legacy scene only in shape',
    (scale) => {
      const shaped = createNodeShapeScene({ size: 400, scale, shaped: true });
      const legacy = createNodeShapeScene({ size: 400, scale, shaped: false });
      expect(createNodeShapeScene({ size: 400, scale, shaped: true })).toEqual(shaped);
      expect(legacy.nodes).toEqual(shaped.nodes);
      expect(legacy.edges).toEqual(shaped.edges);
      expect(createNodeShapeScene({ size: 400, scale, shaped: true, seed: 1 }).nodes).not.toEqual(
        shaped.nodes
      );

      const resolve = (scene) =>
        createNodeShapeResolver({
          nodeTypes: scene.schema.node_types,
          savedShapes: scene.savedShapes,
          typeShapes: scene.typeShapes,
        });
      const looks = shaped.nodes.map((node) => resolve(shaped)(node)?.definition);
      expect(new Set(looks.map((look) => look.base))).toEqual(
        new Set(Object.values(SCENE_TYPE_BASES))
      );
      expect(looks.every((look) => look.scale === scale)).toBe(true);
      expect(legacy.nodes.some((node) => resolve(legacy)(node))).toBe(false);
    }
  );
});

describe('node shape performance budget', () => {
  const run = (value, requests = 0) => ({
    initialRender: value,
    loadAll: value,
    panFrame: [value, value],
    edgeUpdateFrame: [value],
    shapeApply: [value],
    heapMB: value,
    domNodes: 1,
    requests,
  });

  it('passes under a 10% regression, fails at it and never reports an unmeasured metric', () => {
    const legacy = summarize([run(100)]);
    const within = compareScenes(legacy, summarize([run(100 * (1 + BUDGET) - 0.01)]));
    const at = compareScenes(legacy, summarize([run(100 * (1 + BUDGET))]));
    expect(within.panFrame.pass).toBe(true);
    expect(at.panFrame.pass).toBe(false);

    const { edgeUpdateFrame: _skipped, ...partial } = run(100);
    const unmeasured = compareScenes(legacy, summarize([partial]));
    expect(summarize([partial]).edgeUpdateFrame).toEqual({ measured: false });
    expect(unmeasured.edgeUpdateFrame).toEqual({ measured: false });
    expect(unmeasured.initialRender.pass).toBe(true);
    // The two scenes time different operations here: no verdict either way.
    for (const metric of NOT_COMPARABLE) {
      expect(within[metric]).toEqual({ comparable: false });
      expect(at[metric]).toEqual({ comparable: false });
    }
  });

  it('puts the commit and every environment field into the reported table', () => {
    const environment = { cpu: 'cpu-x', browser: 'chromium 1', viewport: { width: 1 } };
    const report = markdownReport({
      commit: 'abc',
      seed: 7,
      runs: 2,
      environment,
      scenes: [],
    });
    const cells = report.split('\n').map((row) =>
      row
        .split('|')
        .slice(1, -1)
        .map((cell) => cell.trim())
    );
    const fields = Object.fromEntries(cells.filter((row) => row.length === 2));
    expect(fields).toMatchObject({
      commit: 'abc',
      seed: '7',
      runs: '2',
      cpu: 'cpu-x',
      browser: 'chromium 1',
      viewport: JSON.stringify(environment.viewport),
    });
  });

  it('fails any additional network request from the shaped scene', () => {
    const legacy = summarize([run(100, 2)]);
    expect(compareScenes(legacy, summarize([run(100, 2)])).requests.pass).toBe(true);
    expect(compareScenes(legacy, summarize([run(100, 3)])).requests.pass).toBe(false);
  });
});
