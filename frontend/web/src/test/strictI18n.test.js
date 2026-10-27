import { beforeEach, describe, expect, it } from 'vitest';
import { missingKeys, strictI18nModule } from './strictI18n';
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
});
