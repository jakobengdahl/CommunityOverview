import { describe, it, expect } from 'vitest';
import { createSelfEchoDedup } from './selfEchoDedup.js';

describe('createSelfEchoDedup', () => {
  it('claim() always returns true for an id that was never marked pending (an ordinary remote annotation)', () => {
    const dedup = createSelfEchoDedup();
    expect(dedup.claim('never-marked')).toBe(true);
    // Not a one-shot fluke: an id outside the raced set is never affected by
    // dedup bookkeeping, so repeated claims (further remote edits to the
    // same ordinary annotation) all return true too.
    expect(dedup.claim('never-marked')).toBe(true);
    expect(dedup.claim('never-marked')).toBe(true);
  });

  it('the first claim() after markPending renders it; the second is a no-op — order A: the direct optimistic apply reaches claim() before the echo', () => {
    const dedup = createSelfEchoDedup();
    dedup.markPending('ann-1');

    // Order A: this browser's own handleImageIngest calls claim() first
    // (its POST resolved before the SSE echo arrived).
    expect(dedup.claim('ann-1')).toBe(true); // renders it
    // The later SSE echo's claim() call for the same id must not re-render
    // it (would otherwise clobber an interim move/resize).
    expect(dedup.claim('ann-1')).toBe(false);
  });

  it('the first claim() after markPending renders it; the second is a no-op — order B: the SSE echo reaches claim() before the direct optimistic apply', () => {
    const dedup = createSelfEchoDedup();
    dedup.markPending('ann-1');

    // Order B: the server broadcasts the op before this browser's own POST
    // resolves, so the echo's claim() call happens first this time.
    expect(dedup.claim('ann-1')).toBe(true); // renders it, via the echo
    // The direct optimistic-apply call, once its POST resolves, must not
    // re-render an annotation the echo already painted.
    expect(dedup.claim('ann-1')).toBe(false);
  });

  it('a third claim() for the same id, after the race has resolved, behaves like an ordinary annotation again (a genuine later update is not swallowed)', () => {
    const dedup = createSelfEchoDedup();
    dedup.markPending('ann-1');
    dedup.claim('ann-1'); // first delivery: renders
    dedup.claim('ann-1'); // second delivery: no-op, race resolved

    // A real subsequent update to this same annotation (e.g. a remote
    // collaborator drags it) must render normally, not be swallowed by
    // leftover race-bookkeeping.
    expect(dedup.claim('ann-1')).toBe(true);
  });

  it('tracks multiple pending ids independently', () => {
    const dedup = createSelfEchoDedup();
    dedup.markPending('a');
    dedup.markPending('b');

    expect(dedup.claim('a')).toBe(true);
    expect(dedup.claim('b')).toBe(true);
    expect(dedup.claim('a')).toBe(false);
    expect(dedup.claim('b')).toBe(false);
  });

  it('treats non-string/empty ids as never raced', () => {
    const dedup = createSelfEchoDedup();
    dedup.markPending(undefined);
    dedup.markPending('');
    dedup.markPending(null);

    expect(dedup.claim(undefined)).toBe(true);
    expect(dedup.claim('')).toBe(true);
    expect(dedup.claim(null)).toBe(true);
  });

  it('forget() drops a reservation whose upload never actually completed, so a later unrelated create for the same id is not swallowed', () => {
    const dedup = createSelfEchoDedup();
    dedup.markPending('ann-1');

    // The POST failed (or the caller bailed before sending it) — no
    // annotation was ever created server-side, so no echo will ever arrive
    // for this id either.
    dedup.forget('ann-1');

    // A later, unrelated create that happens to reuse this id must render
    // normally, not be swallowed by the abandoned reservation.
    expect(dedup.claim('ann-1')).toBe(true);
  });

  it('forget() on an id that was never marked is a harmless no-op', () => {
    const dedup = createSelfEchoDedup();
    expect(() => dedup.forget('never-marked')).not.toThrow();
    expect(dedup.claim('never-marked')).toBe(true);
  });

  it('clear() drops any pending/resolved state, so ids from a wholesale resync are not treated as raced', () => {
    const dedup = createSelfEchoDedup();
    dedup.markPending('still-pending');
    dedup.markPending('already-resolved');
    dedup.claim('already-resolved'); // first delivery landed; second never will

    dedup.clear();

    expect(dedup.claim('still-pending')).toBe(true);
    expect(dedup.claim('already-resolved')).toBe(true);
  });
});
