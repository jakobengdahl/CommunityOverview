/**
 * @vitest-environment jsdom
 */
import { createElement } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { expectAllKeysResolved, missingKeys, strictI18nModule } from './strictI18n';
import en from '../i18n/en.json';

const { t } = strictI18nModule.useI18n();

beforeEach(() => {
  missingKeys.length = 0;
});

describe('strictI18n', () => {
  it('records a key en.json lacks and returns the fallback or the key', () => {
    expect(t('no.such.key')).toBe('no.such.key');
    expect(t('no.other.key', undefined, 'Fallback')).toBe('Fallback');
    expect(missingKeys).toEqual(['no.such.key', 'no.other.key']);
  });

  it('resolves a known key with its params and records nothing', () => {
    expect(t('agent_dialog.name_too_long', { max: 185 })).toBe(
      en.agent_dialog.name_too_long.replace('{max}', '185')
    );
    expect(missingKeys).toEqual([]);
  });

  it('unmounts before failing on an unknown key, so the DOM cannot leak into the next test', () => {
    render(createElement('p', null, 'rendered'));
    t('no.such.key');
    expect(() => expectAllKeysResolved()).toThrow();
    expect(document.body.textContent).toBe('');
    // Drained, so the next test does not inherit the key either.
    expect(missingKeys).toEqual([]);
  });
});
