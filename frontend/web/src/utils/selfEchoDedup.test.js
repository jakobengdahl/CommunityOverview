import { describe, it, expect } from 'vitest';
import { createSelfEchoDedup } from './selfEchoDedup.js';

describe('createSelfEchoDedup', () => {
  it('skips exactly the first shouldSkip after a markApplied for the same id', () => {
    const dedup = createSelfEchoDedup();
    dedup.markApplied('ann-1');

    expect(dedup.shouldSkip('ann-1')).toBe(true);
    // Delete-on-first-sight: a second echo for the same id is not swallowed —
    // it's a genuine later update (e.g. a remote collaborator's edit), not a
    // repeat of the one this browser already applied optimistically.
    expect(dedup.shouldSkip('ann-1')).toBe(false);
  });

  it('never skips an id that was not marked', () => {
    const dedup = createSelfEchoDedup();
    expect(dedup.shouldSkip('never-applied')).toBe(false);
  });

  it('tracks multiple ids independently', () => {
    const dedup = createSelfEchoDedup();
    dedup.markApplied('a');
    dedup.markApplied('b');

    expect(dedup.shouldSkip('a')).toBe(true);
    expect(dedup.shouldSkip('b')).toBe(true);
    expect(dedup.shouldSkip('a')).toBe(false);
  });

  it('treats non-string/empty ids as never marked or matched', () => {
    const dedup = createSelfEchoDedup();
    dedup.markApplied(undefined);
    dedup.markApplied('');
    dedup.markApplied(null);

    expect(dedup.shouldSkip(undefined)).toBe(false);
    expect(dedup.shouldSkip('')).toBe(false);
    expect(dedup.shouldSkip(null)).toBe(false);
  });

  it('forget() drops a mark whose upload never actually completed, so a later unrelated create for the same id is not swallowed', () => {
    const dedup = createSelfEchoDedup();
    dedup.markApplied('ann-1');

    // The POST failed (or the caller bailed before sending it) — no
    // annotation was ever created server-side, so no echo will ever arrive
    // for this id.
    dedup.forget('ann-1');

    // A later, unrelated create that happens to reuse this id must not be
    // swallowed by the abandoned mark.
    expect(dedup.shouldSkip('ann-1')).toBe(false);
  });

  it('forget() on an id that was never marked is a harmless no-op', () => {
    const dedup = createSelfEchoDedup();
    expect(() => dedup.forget('never-marked')).not.toThrow();
    expect(dedup.shouldSkip('never-marked')).toBe(false);
  });

  it('clear() drops a pending mark whose echo never arrived, so a later unrelated update for the same id is not swallowed', () => {
    const dedup = createSelfEchoDedup();
    dedup.markApplied('ann-1');

    // The confirming echo never showed up as a discrete op (e.g. the stream
    // reconnected and resynced wholesale instead) — the caller clears the
    // whole set rather than let the mark linger.
    dedup.clear();

    // A later, genuine annotation_updated for the same id (a real remote
    // edit reusing it) must go through normally, not be swallowed by a stale
    // mark from the upload this browser made long before.
    expect(dedup.shouldSkip('ann-1')).toBe(false);
  });
});
