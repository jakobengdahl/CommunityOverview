// Runs the node shape performance scenes in headless Chromium and prints the
// p50/p95 table the performance gate records (docs/design/NODE_SHAPES.md §
// Theme, accessibility and scale). From frontend/web:
//
//   npm run perf:node-shapes -- [--sizes 1000,5000] [--scales 0.1,1,10]
//     [--themes light,dark] [--runs 5] [--out results.json]
//
// Each run opens a fresh page with a production build of one scene, so a
// shaped scene and its legacy-card baseline start from the same state; the
// two alternate within every run to spread machine drift over both.
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { build, preview } from 'vite';
import react from '@vitejs/plugin-react';
import { chromium } from '@playwright/test';
import { SCENE_SCALES, SCENE_SEED, SCENE_SIZES, SCENE_THEMES } from './scene.js';
import { BUDGET, compareScenes, markdownReport, summarize } from './stats.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const arg = (name, fallback) => {
  const at = process.argv.indexOf(`--${name}`);
  return at === -1 ? fallback : process.argv[at + 1];
};
const list = (name, fallback) => arg(name, fallback.join(',')).split(',').map(Number);
const sizes = list('sizes', SCENE_SIZES);
const scales = list('scales', SCENE_SCALES);
const themes = arg('themes', SCENE_THEMES.join(',')).split(',');
const runs = Number(arg('runs', '5'));
const out = arg('out', null);
const VIEWPORT = { width: 1280, height: 800 };
const GESTURE_FRAMES = 60;
const MAX_CRASHES = 3;

async function runScene(browser, url, theme) {
  const context = await browser.newContext({ viewport: VIEWPORT, colorScheme: theme });
  try {
    return await measureScene(context, url);
  } finally {
    await context.close().catch(() => {});
  }
}

async function measureScene(context, url) {
  const page = await context.newPage();
  await page.goto(url, { waitUntil: 'load' });
  // Everything after the page has loaded is the scene's own traffic.
  let requests = 0;
  page.on('request', () => requests++);
  const cdp = await context.newCDPSession(page);
  await cdp.send('Performance.enable');
  const sample = { requests: 0 };
  sample.initialRender = await page.evaluate(() => window.bench.mount());
  sample.loadAll = await page.evaluate(() => window.bench.loadAll());
  if (sample.loadAll === null) delete sample.loadAll;
  // Let fit-view finish its animation before the steady-state measurements.
  await page.waitForTimeout(1200);
  await cdp.send('HeapProfiler.collectGarbage');
  const { metrics } = await cdp.send('Performance.getMetrics');
  const metric = (name) => metrics.find((m) => m.name === name)?.value;
  sample.heapMB = metric('JSHeapUsedSize') / 2 ** 20;
  sample.domNodes = metric('Nodes');

  const pane = await page.evaluate(() => window.bench.panePoint());
  if (pane) {
    const before = await page.evaluate(() => window.bench.viewport());
    sample.panFrame = await page.evaluate(
      ({ from, frames }) => window.bench.gesture({ from, step: { x: 4, y: 3 }, frames, button: 2 }),
      { from: pane, frames: GESTURE_FRAMES }
    );
    // A gesture that did not pan measured nothing.
    if ((await page.evaluate(() => window.bench.viewport())) === before) delete sample.panFrame;
  }

  const node = await page.evaluate(() => window.bench.dragPoint());
  if (node) {
    const before = await page.evaluate((id) => window.bench.nodeTransform(id), node.id);
    sample.edgeUpdateFrame = await page.evaluate(
      ({ from, frames }) => window.bench.gesture({ from, step: { x: 3, y: 2 }, frames, button: 0 }),
      { from: node, frames: GESTURE_FRAMES }
    );
    const after = await page.evaluate((id) => window.bench.nodeTransform(id), node.id);
    if (after === before) delete sample.edgeUpdateFrame;
    else sample.draggedEdges = node.edges;
  }

  sample.shapeApply = [];
  for (const on of [true, false, true, false]) {
    sample.shapeApply.push(await page.evaluate((value) => window.bench.apply(value), on));
  }
  sample.requests = requests;
  return sample;
}

const outDir = mkdtempSync(path.join(os.tmpdir(), 'node-shape-perf-'));
await build({
  root: here,
  base: './',
  logLevel: 'warn',
  plugins: [react()],
  build: { outDir, emptyOutDir: true },
});
const server = await preview({
  root: here,
  logLevel: 'warn',
  build: { outDir },
  preview: { port: 0, host: '127.0.0.1' },
});
const origin = server.resolvedUrls.local[0];
let browser = await chromium.launch();
// What the browser reports about itself, rather than what the launch implies.
const browserInfo = await (async () => {
  const page = await browser.newPage();
  try {
    return await page.evaluate(() => {
      const gl = document.createElement('canvas').getContext('webgl');
      const debug = gl?.getExtension('WEBGL_debug_renderer_info');
      return {
        userAgent: navigator.userAgent,
        webglRenderer: !gl
          ? 'no WebGL context'
          : gl.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : gl.RENDERER),
      };
    });
  } finally {
    await page.close();
  }
})();
browserInfo.browser = `${browser.browserType().name()} ${browser.version()}`;
const git = (...args) => execFileSync('git', args, { cwd: here, encoding: 'utf8' }).trim();
const commit = `${git('rev-parse', 'HEAD')}${git('status', '--porcelain') ? ' (uncommitted changes)' : ''}`;
const loadStart = os.loadavg();
const scenes = [];
try {
  for (const size of sizes) {
    for (const theme of themes) {
      for (const scale of scales) {
        const samples = { legacy: [], shaped: [] };
        const crashes = { legacy: 0, shaped: 0 };
        for (let run = 0; run < runs; run++) {
          const order = run % 2 ? ['shaped', 'legacy'] : ['legacy', 'shaped'];
          for (const variant of order) {
            const url = `${origin}index.html?size=${size}&scale=${scale}&variant=${variant}`;
            // A crashed or stuck page measured nothing: count it and run again.
            for (;;) {
              try {
                samples[variant].push(await runScene(browser, url, theme));
                break;
              } catch (error) {
                console.error(`${variant} run failed: ${error.message.split('\n')[0]}`);
                if (++crashes[variant] > MAX_CRASHES) throw error;
                if (!browser.isConnected()) browser = await chromium.launch();
              }
            }
          }
        }
        const scene = {
          size,
          theme,
          scale,
          crashes,
          legacy: summarize(samples.legacy),
          shaped: summarize(samples.shaped),
        };
        scene.budget = compareScenes(scene.legacy, scene.shaped);
        scenes.push(scene);
        console.error(`done: ${size} nodes, ${theme}, scale ${scale}`);
      }
    }
  }
} finally {
  await browser.close();
  await server.close();
  rmSync(outDir, { recursive: true, force: true });
}

const result = {
  commit,
  seed: SCENE_SEED,
  runs,
  budget: BUDGET,
  environment: {
    cpu: os.cpus()[0]?.model,
    logicalCores: os.cpus().length,
    memoryGB: Math.round(os.totalmem() / 2 ** 30),
    os: `${os.type()} ${os.release()}`,
    node: process.version,
    browser: browserInfo.browser,
    userAgent: browserInfo.userAgent,
    webglRenderer: browserInfo.webglRenderer,
    viewport: VIEWPORT,
    gestureFrames: GESTURE_FRAMES,
    loadAverage: { start: loadStart, end: os.loadavg() },
  },
  scenes,
};
if (out) writeFileSync(out, `${JSON.stringify(result, null, 2)}\n`);
console.log(markdownReport(result));
