// Deployment theme overrides (`presentation.theme` in the schema config) mapped
// onto the design tokens in styles/tokens.css. The backend already keeps only
// these keys with plain colour values; unknown keys are ignored here too.
const THEME_TOKENS = {
  accent: '--co-accent',
  accent_hover: '--co-accent-hover',
  accent_contrast: '--co-accent-contrast',
};

/**
 * Set the deployment's token overrides on the root element, replacing any set
 * by an earlier call. An accent without its own hover colour gets a darker
 * shade of itself, so the default hover never sits under a different accent.
 * @param {Record<string, string> | null | undefined} theme
 * @param {HTMLElement} [root]
 */
export function applyTheme(theme, root = document.documentElement) {
  Object.values(THEME_TOKENS).forEach((property) => root.style.removeProperty(property));
  const entries = theme && typeof theme === 'object' ? theme : {};
  Object.entries(THEME_TOKENS).forEach(([key, property]) => {
    if (typeof entries[key] === 'string') root.style.setProperty(property, entries[key]);
  });
  if (typeof entries.accent === 'string' && typeof entries.accent_hover !== 'string') {
    root.style.setProperty(
      THEME_TOKENS.accent_hover,
      `color-mix(in srgb, ${entries.accent} 85%, black)`
    );
  }
}
