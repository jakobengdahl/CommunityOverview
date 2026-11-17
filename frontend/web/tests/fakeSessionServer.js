import { vi } from 'vitest';
import { MEMBERSHIP_OPS } from '../src/services/sessionSyncClient';

/**
 * A stand-in for the session ops endpoint, for tests that render App. Like the
 * server, it sequences each membership op and broadcasts it on that session's
 * stream, to its sender too: that broadcast is what draws added nodes. Other
 * ops are acknowledged and not broadcast.
 *
 * @param {Function} streams  Returns the fake EventSources opened so far.
 */
export function echoingOpsFetch(streams) {
  let seq = 1000;
  return vi.fn(async (url, opts) => {
    let ops = [];
    try {
      ops = JSON.parse(opts?.body).ops || [];
    } catch {
      /* not an op batch */
    }
    const applied = ops
      .filter((op) => MEMBERSHIP_OPS.has(op?.op))
      .map((op) => ({ ...op, seq: ++seq }));
    const session = String(url).match(/\/sessions\/([^/]+)\//)?.[1];
    const stream = streams()
      .filter((s) => s.url.includes(`/sessions/${session}/`))
      .at(-1);
    setTimeout(() =>
      applied.forEach((op) =>
        stream?.onmessage?.({
          data: JSON.stringify({ type: 'op', client_id: 'server', op, seq: op.seq }),
        })
      )
    );
    return { ok: true, status: 200, json: async () => ({ applied, seq }) };
  });
}
