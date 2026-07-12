import { describe, it, expect } from 'vitest';
import { relativeTime, entityName, computeDiff, formatValue, isUpdate } from './history';

describe('relativeTime', () => {
  const now = Date.parse('2026-07-11T12:00:00Z');

  it('reports just now for very recent events', () => {
    const r = relativeTime('2026-07-11T11:59:40Z', now);
    expect(r.key).toBe('history.time.just_now');
  });

  it('reports minutes for events within the hour', () => {
    const r = relativeTime('2026-07-11T11:30:00Z', now);
    expect(r).toEqual({ key: 'history.time.minutes_ago', count: 30 });
  });

  it('reports hours within the day', () => {
    const r = relativeTime('2026-07-11T09:00:00Z', now);
    expect(r).toEqual({ key: 'history.time.hours_ago', count: 3 });
  });

  it('reports days beyond 24h', () => {
    const r = relativeTime('2026-07-09T12:00:00Z', now);
    expect(r).toEqual({ key: 'history.time.days_ago', count: 2 });
  });

  it('handles unparseable input', () => {
    expect(relativeTime('not-a-date', now).key).toBe('history.time.unknown');
  });
});

describe('entityName', () => {
  it('prefers the after-state name for creates/updates', () => {
    expect(entityName({ after: { name: 'Alice' }, entity_id: 'n1' })).toBe('Alice');
  });

  it('falls back to the before-state for deletes', () => {
    expect(entityName({ before: { name: 'Bob' }, entity_id: 'n2' })).toBe('Bob');
  });

  it('falls back to the entity id when no name is present', () => {
    expect(entityName({ entity_id: 'n3' })).toBe('n3');
  });
});

describe('computeDiff', () => {
  it('derives before→after pairs from the patch and before snapshot', () => {
    const entry = {
      event_type: 'node.update',
      before: { name: 'Old', summary: 'keep' },
      after: { name: 'New', summary: 'keep' },
      patch: { name: 'New' },
    };
    expect(computeDiff(entry)).toEqual([{ field: 'name', before: 'Old', after: 'New' }]);
  });

  it('shows an added field (absent in before) as undefined→value', () => {
    const entry = {
      event_type: 'node.update',
      before: {},
      after: { tags: ['x'] },
      patch: { tags: ['x'] },
    };
    expect(computeDiff(entry)).toEqual([{ field: 'tags', before: undefined, after: ['x'] }]);
  });

  it('falls back to before/after diffing when no patch is present (edges)', () => {
    const entry = {
      event_type: 'edge.update',
      before: { label: 'A', type: 'REL' },
      after: { label: 'B', type: 'REL' },
      patch: null,
    };
    expect(computeDiff(entry)).toEqual([{ field: 'label', before: 'A', after: 'B' }]);
  });

  it('returns nothing when there is no before/after/patch', () => {
    expect(computeDiff({ event_type: 'node.create', after: { name: 'x' } })).toEqual([]);
  });
});

describe('formatValue', () => {
  it('renders empty values as an em dash', () => {
    expect(formatValue(null)).toBe('—');
    expect(formatValue('')).toBe('—');
  });

  it('joins arrays', () => {
    expect(formatValue(['a', 'b'])).toBe('a, b');
  });

  it('json-encodes objects', () => {
    expect(formatValue({ a: 1 })).toBe('{"a":1}');
  });
});

describe('isUpdate', () => {
  it('is true only for update events', () => {
    expect(isUpdate({ event_type: 'node.update' })).toBe(true);
    expect(isUpdate({ event_type: 'edge.update' })).toBe(true);
    expect(isUpdate({ event_type: 'node.create' })).toBe(false);
    expect(isUpdate({})).toBe(false);
  });
});
