/**
 * CLAUDE.md requires every UI string key to exist in both en.json and sv.json:
 * a key missing from sv.json silently renders in English, and one missing from
 * en.json renders as the raw key name.
 * Past violations were caught only by manual review, so this guards it in CI.
 */
import { describe, it, expect } from 'vitest';
import en from './en.json';
import sv from './sv.json';

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

// Arrays are leaves: their elements are translated content, not keys.
function leafEntries(obj, prefix = '', out = new Map()) {
  for (const [key, value] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (isPlainObject(value)) {
      leafEntries(value, path, out);
    } else {
      out.set(path, value);
    }
  }
  return out;
}

function placeholders(value) {
  const strings = Array.isArray(value) ? value : [value];
  const names = new Set();
  for (const s of strings) {
    if (typeof s !== 'string') continue;
    // Same token syntax as interpolate() in ./index.jsx.
    for (const match of s.matchAll(/\{(\w+)\}/g)) names.add(match[1]);
  }
  return [...names].sort();
}

function missingFrom(source, target) {
  return [...source.keys()].filter((path) => !target.has(path)).sort();
}

const enLeaves = leafEntries(en);
const svLeaves = leafEntries(sv);

describe('i18n key parity between en.json and sv.json', () => {
  it('has no key in en.json that is missing from sv.json', () => {
    const missing = missingFrom(enLeaves, svLeaves);
    expect(missing, `Keys in en.json missing from sv.json:\n  ${missing.join('\n  ')}`).toEqual([]);
  });

  it('has no key in sv.json that is missing from en.json', () => {
    const missing = missingFrom(svLeaves, enLeaves);
    expect(missing, `Keys in sv.json missing from en.json:\n  ${missing.join('\n  ')}`).toEqual([]);
  });

  it('uses the same interpolation placeholders for each shared key', () => {
    const mismatched = [...enLeaves.keys()]
      .filter((path) => svLeaves.has(path))
      .filter(
        (path) =>
          placeholders(enLeaves.get(path)).join(',') !== placeholders(svLeaves.get(path)).join(',')
      )
      .map(
        (path) =>
          `${path}: en {${placeholders(enLeaves.get(path)).join(', ')}} vs sv {${placeholders(svLeaves.get(path)).join(', ')}}`
      );
    expect(mismatched, `Placeholder mismatches:\n  ${mismatched.join('\n  ')}`).toEqual([]);
  });
});
