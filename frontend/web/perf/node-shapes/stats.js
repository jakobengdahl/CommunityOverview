// Percentiles and the relative budget for the node shape performance scenes.

/** The shaped scene may cost at most this fraction more than its legacy baseline. */
export const BUDGET = 0.1;

// Budgeted metrics; a run records one number or a list of samples for each.
export const METRICS = [
  'initialRender',
  'loadAll',
  'panFrame',
  'edgeUpdateFrame',
  'shapeApply',
  'heapMB',
];
// Reported for information; `draggedEdges` shows both scenes dragged alike.
const INFO = ['domNodes', 'draggedEdges', 'requests'];
// Measured in both scenes but not the same operation: the legacy card has no
// shape to apply, so its timing is a reference, never a budget verdict.
export const NOT_COMPARABLE = ['shapeApply'];

/** Nearest-rank percentile of a non-empty list. */
export function percentile(values, p) {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.max(0, Math.ceil((p / 100) * sorted.length) - 1)];
}

/**
 * `{ metric: { p50, p95, n } }` over every sample of every run. A metric no
 * run measured is `{ measured: false }`, never a number.
 */
export function summarize(runs) {
  const summary = {};
  for (const metric of [...METRICS, ...INFO]) {
    const values = runs.flatMap((run) => run[metric] ?? []);
    summary[metric] = values.length
      ? { p50: percentile(values, 50), p95: percentile(values, 95), n: values.length }
      : { measured: false };
  }
  return summary;
}

/**
 * Per metric, the shaped scene's p50 and p95 relative to the legacy scene's
 * and whether both stay under the budget; network requests may not grow. A
 * metric the two scenes measure differently, or a scene with no completed
 * run, gets no verdict.
 */
export function compareScenes(legacy, shaped) {
  const result = {};
  for (const metric of METRICS) {
    if (legacy[metric].measured === false || shaped[metric].measured === false) {
      result[metric] = { measured: false };
      continue;
    }
    if (NOT_COMPARABLE.includes(metric)) {
      result[metric] = { comparable: false };
      continue;
    }
    const p50 = shaped[metric].p50 / legacy[metric].p50 - 1;
    const p95 = shaped[metric].p95 / legacy[metric].p95 - 1;
    result[metric] = { p50, p95, pass: p50 < BUDGET && p95 < BUDGET };
  }
  result.requests =
    legacy.requests.measured === false || shaped.requests.measured === false
      ? { measured: false }
      : {
          legacy: legacy.requests.p95,
          shaped: shaped.requests.p95,
          pass: shaped.requests.p95 <= legacy.requests.p95,
        };
  return result;
}

const fixed = (value, digits) => (value === undefined ? 'n/a' : value.toFixed(digits));
const percent = (value) => `${value >= 0 ? '+' : ''}${(value * 100).toFixed(1)}%`;
const cell = (summary, digits) =>
  summary.measured === false
    ? 'not measured'
    : `${fixed(summary.p50, digits)} / ${fixed(summary.p95, digits)}`;

const change = (budget) => {
  if (!budget) return '—';
  if (budget.measured === false) return 'not measured';
  if (budget.comparable === false) return 'not comparable';
  return `${percent(budget.p50)} / ${percent(budget.p95)}`;
};
const verdict = (budget) => {
  if (!budget || budget.comparable === false) return 'info';
  if (budget.measured === false) return '—';
  return budget.pass ? 'pass' : 'fail';
};
const value = (v) => (v !== null && typeof v === 'object' ? JSON.stringify(v) : String(v));

/**
 * The record as Markdown: what ran where (every environment field, so a pasted
 * table never lacks hardware and browser), then one row per scene and metric.
 */
export function markdownReport({ seed, runs, commit, environment, scenes }) {
  const rows = [
    '| Run | Value |',
    '| --- | --- |',
    `| commit | ${value(commit)} |`,
    `| seed | ${value(seed)} |`,
    `| runs | ${value(runs)} |`,
    ...Object.entries(environment).map(([key, v]) => `| ${key} | ${value(v)} |`),
    '',
    '| Nodes | Theme | Scale | Metric | Legacy p50 / p95 | Shaped p50 / p95 | Change p50 / p95 | Budget |',
    '| --- | --- | --- | --- | --- | --- | --- | --- |',
  ];
  for (const scene of scenes) {
    for (const metric of [...METRICS, 'domNodes', 'draggedEdges']) {
      const budget = scene.budget[metric];
      const digits = METRICS.includes(metric) ? 1 : 0;
      rows.push(
        `| ${scene.size} | ${scene.theme} | ${scene.scale} | ${metric} | ${cell(scene.legacy[metric], digits)} | ${cell(scene.shaped[metric], digits)} | ${change(budget)} | ${verdict(budget)} |`
      );
    }
    const { requests } = scene.budget;
    rows.push(
      requests.measured === false
        ? `| ${scene.size} | ${scene.theme} | ${scene.scale} | requests | not measured | not measured | not measured | — |`
        : `| ${scene.size} | ${scene.theme} | ${scene.scale} | requests | ${requests.legacy} | ${requests.shaped} | — | ${requests.pass ? 'pass' : 'fail'} |`
    );
    if (scene.crashes) {
      rows.push(
        `| ${scene.size} | ${scene.theme} | ${scene.scale} | failed runs (rerun) | ${scene.crashes.legacy} | ${scene.crashes.shaped} | — | info |`
      );
    }
    if (scene.completed) {
      rows.push(
        `| ${scene.size} | ${scene.theme} | ${scene.scale} | completed runs | ${scene.completed.legacy} | ${scene.completed.shaped} | — | info |`
      );
    }
  }
  return rows.join('\n');
}
