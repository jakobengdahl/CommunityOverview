/**
 * @vitest-environment jsdom
 *
 * Covers the interim English-only lock (founder note 2026-08-26,
 * Corp task cb61993e-1154-41cb-9acb-80aaa26991ed): the app must render in
 * English regardless of stored preference, URL param, or backend-provided
 * default, while the underlying localization architecture (sv.json,
 * SUPPORTED_LANGUAGES, the translation-key mechanism) stays intact and
 * trivially reversible.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import {
  I18nProvider,
  useI18n,
  SUPPORTED_LANGUAGES,
  DEFAULT_LANGUAGE,
  LANGUAGE_SWITCHING_ENABLED,
} from './index';

function Probe() {
  const { language, t, setLanguage, languageSwitchingEnabled, supportedLanguages } = useI18n();
  return (
    <div>
      <span data-testid="language">{language}</span>
      <span data-testid="switching-enabled">{String(languageSwitchingEnabled)}</span>
      <span data-testid="supported">{supportedLanguages.join(',')}</span>
      <span data-testid="menu-language-en">{t('menu.language_en')}</span>
      <button onClick={() => setLanguage('sv')}>switch-to-sv</button>
    </div>
  );
}

describe('I18nProvider — interim English-only lock', () => {
  beforeEach(() => {
    window.localStorage.clear();
    cleanup();
  });

  it('renders in English even with a stored Swedish preference', () => {
    window.localStorage.setItem('app_language', 'sv');
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>
    );
    expect(screen.getByTestId('language').textContent).toBe('en');
  });

  it('renders in English even when backend config supplies a Swedish default', () => {
    render(
      <I18nProvider defaultLanguage="sv">
        <Probe />
      </I18nProvider>
    );
    expect(screen.getByTestId('language').textContent).toBe('en');
  });

  it('ignores setLanguage calls while switching is disabled', () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>
    );
    fireEvent.click(screen.getByText('switch-to-sv'));
    expect(screen.getByTestId('language').textContent).toBe('en');
    expect(window.localStorage.getItem('app_language')).toBeNull();
  });

  it('exposes languageSwitchingEnabled=false to consumers', () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>
    );
    expect(screen.getByTestId('switching-enabled').textContent).toBe('false');
    expect(LANGUAGE_SWITCHING_ENABLED).toBe(false);
  });

  it('keeps the underlying localization architecture intact for later re-enabling', () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>
    );
    // sv is still a supported language and the sv.json translation resource
    // is still wired up — only the ability to switch to it is gated.
    expect(SUPPORTED_LANGUAGES).toEqual(['en', 'sv']);
    expect(screen.getByTestId('supported').textContent).toBe('en,sv');
    expect(DEFAULT_LANGUAGE).toBe('en');
    expect(screen.getByTestId('menu-language-en').textContent).toBe('English');
  });
});
