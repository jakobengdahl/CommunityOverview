/**
 * CLAUDE.md requires every UI string key to exist in both en.json and sv.json:
 * a key missing from sv.json silently falls back to English, and one missing
 * from en.json renders as t()'s fallback argument, or the raw key name when
 * there is none, in every language that lacks it.
 * Past violations were caught only by manual review, so this guards it in CI.
 */
import { describe, it, expect } from 'vitest';
import en from './en.json';
import sv from './sv.json';

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

// Arrays are leaves: their elements are translated content, not keys. Empty
// objects are leaves too, so a `{}` present in only one file still counts.
function leafEntries(obj, prefix = '', out = new Map()) {
  for (const [key, value] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (isPlainObject(value) && Object.keys(value).length > 0) {
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

// t() resolves keys by splitting on '.', so a key containing a dot can never be
// looked up, and its joined path would collide with a genuinely nested one.
function dottedKeys(obj, prefix = '', out = []) {
  for (const [key, value] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (key.includes('.')) out.push(path);
    if (isPlainObject(value)) dottedKeys(value, path, out);
  }
  return out;
}

function leafKind(value) {
  if (Array.isArray(value)) return 'array';
  if (value === null) return 'null';
  return typeof value;
}

function missingFrom(source, target) {
  return [...source.keys()].filter((path) => !target.has(path)).sort();
}

const enLeaves = leafEntries(en);
const svLeaves = leafEntries(sv);

describe('i18n key parity between en.json and sv.json', () => {
  it('has no key name containing a dot in either file', () => {
    const dotted = [
      ...dottedKeys(en).map((p) => `en: ${p}`),
      ...dottedKeys(sv).map((p) => `sv: ${p}`),
    ];
    expect(dotted, `Key names containing '.':\n  ${dotted.join('\n  ')}`).toEqual([]);
  });

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

  it('uses the same kind of value (string, array, ...) for each shared key', () => {
    const mismatched = [...enLeaves.keys()]
      .filter((path) => svLeaves.has(path))
      .filter((path) => leafKind(enLeaves.get(path)) !== leafKind(svLeaves.get(path)))
      .map(
        (path) =>
          `${path}: en ${leafKind(enLeaves.get(path))} vs sv ${leafKind(svLeaves.get(path))}`
      );
    expect(mismatched, `Value kind mismatches:\n  ${mismatched.join('\n  ')}`).toEqual([]);
  });
});
