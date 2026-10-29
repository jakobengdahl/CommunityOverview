// Test double for ../i18n whose t() records every key en.json lacks. Mocking
// the module with it catches an unknown key wherever the component uses it
// (text, placeholder, aria-label, title, alert message), not only in the
// visible text a DOM query reads.
import { cleanup } from '@testing-library/react';
import { expect } from 'vitest';
import en from '../i18n/en.json';

export const missingKeys = [];

function t(key, params, fallback) {
  const value = key.split('.').reduce((node, part) => node?.[part], en);
  if (value === undefined) {
    missingKeys.push(key);
    return fallback ?? key;
  }
  if (typeof value !== 'string' || !params) return value;
  return value.replace(/\{(\w+)\}/g, (_, name) => params[name] ?? `{${name}}`);
}

export const strictI18nModule = {
  useI18n: () => ({ t, language: 'en', setLanguage: () => {} }),
};

// For a test file's afterEach. Unmounts first: a throwing afterEach stops the
// later hooks, Testing Library's own cleanup among them, so the failing test's
// DOM would otherwise leak into every test after it.
export function expectAllKeysResolved() {
  cleanup();
  expect(missingKeys.splice(0)).toEqual([]);
}
