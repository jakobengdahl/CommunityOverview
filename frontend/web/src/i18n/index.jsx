import { createContext, useContext, useState, useCallback, useEffect } from 'react';
import en from './en.json';
import sv from './sv.json';

const translations = { en, sv };
const SUPPORTED_LANGUAGES = ['en', 'sv'];
const DEFAULT_LANGUAGE = 'en';

const I18nContext = createContext(null);

/**
 * Get a nested value from an object using a dot-separated path.
 */
function getNestedValue(obj, path) {
  return path.split('.').reduce((current, key) => current?.[key], obj);
}

/**
 * Replace {placeholder} tokens in a string with values from a params object.
 */
function interpolate(str, params) {
  if (!params || typeof str !== 'string') return str;
  return str.replace(/\{(\w+)\}/g, (_, key) => params[key] ?? `{${key}}`);
}

/**
 * Detect the initial language from URL parameters, localStorage, or default.
 */
function detectLanguage() {
  // 1. Check URL parameter ?lang=sv
  const urlParams = new URLSearchParams(window.location.search);
  const urlLang = urlParams.get('lang');
  if (urlLang && SUPPORTED_LANGUAGES.includes(urlLang)) {
    return urlLang;
  }

  // 2. Check localStorage
  const storedLang = localStorage.getItem('app_language');
  if (storedLang && SUPPORTED_LANGUAGES.includes(storedLang)) {
    return storedLang;
  }

  // 3. Default
  return DEFAULT_LANGUAGE;
}

/**
 * I18n Provider component. Wrap your app with this.
 */
export function I18nProvider({ children, defaultLanguage }) {
  const [language, setLanguageState] = useState(() => {
    const detected = detectLanguage();
    // If a defaultLanguage from backend config is provided and no URL/localStorage override
    if (defaultLanguage && !new URLSearchParams(window.location.search).get('lang') && !localStorage.getItem('app_language')) {
      return SUPPORTED_LANGUAGES.includes(defaultLanguage) ? defaultLanguage : detected;
    }
    return detected;
  });

  const setLanguage = useCallback((lang) => {
    if (SUPPORTED_LANGUAGES.includes(lang)) {
      setLanguageState(lang);
      localStorage.setItem('app_language', lang);
    }
  }, []);

  // Update language when backend config provides a default (only if no user override)
  useEffect(() => {
    if (defaultLanguage && !localStorage.getItem('app_language') && !new URLSearchParams(window.location.search).get('lang')) {
      if (SUPPORTED_LANGUAGES.includes(defaultLanguage)) {
        setLanguageState(defaultLanguage);
      }
    }
  }, [defaultLanguage]);

  const t = useCallback((key, params) => {
    const value = getNestedValue(translations[language], key)
      ?? getNestedValue(translations[DEFAULT_LANGUAGE], key)
      ?? key;

    if (typeof value === 'string') {
      return interpolate(value, params);
    }
    return value; // arrays, objects returned as-is
  }, [language]);

  const value = { language, setLanguage, t, supportedLanguages: SUPPORTED_LANGUAGES };

  return (
    <I18nContext.Provider value={value}>
      {children}
    </I18nContext.Provider>
  );
}

/**
 * Hook to access i18n functionality.
 *
 * Usage:
 *   const { t, language, setLanguage } = useI18n();
 *   t('chat.send') // "Send" or "Skicka"
 *   t('chat.error_prefix', { message: 'oops' }) // "Error: oops"
 */
export function useI18n() {
  const ctx = useContext(I18nContext);
  if (!ctx) {
    // Fallback when used outside provider (e.g. in tests)
    return {
      language: DEFAULT_LANGUAGE,
      setLanguage: () => {},
      t: (key, params) => {
        const value = getNestedValue(translations[DEFAULT_LANGUAGE], key) ?? key;
        return typeof value === 'string' ? interpolate(value, params) : value;
      },
      supportedLanguages: SUPPORTED_LANGUAGES,
    };
  }
  return ctx;
}

export { SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE };
