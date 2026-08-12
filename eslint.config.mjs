// ESLint flat config for the JavaScript/JSX workspaces (frontend/web,
// frontend/widget, packages/ui-graph-canvas).
//
// This is a mechanical correctness gate, not a style enforcer: Prettier owns
// formatting (eslint-config-prettier, applied last, disables every rule that
// would conflict). ESLint here catches the high-value problems — undefined
// names, unused variables, and React rules-of-hooks violations. Opinionated or
// noisy rules are demoted to warnings so the baseline stays green while still
// surfacing issues; CI fails on errors only. See STRUCTURE_REVIEW item C2.

import js from '@eslint/js';
import globals from 'globals';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import prettier from 'eslint-config-prettier';

export default [
  {
    ignores: [
      '**/dist/**',
      '**/node_modules/**',
      '**/coverage/**',
      '**/playwright-report/**',
      '**/test-results/**',
      '**/*.min.js',
    ],
  },

  // Pre-existing eslint-disable comments that no longer match a firing rule
  // are surfaced as warnings, not baseline-breaking errors.
  {
    linterOptions: {
      reportUnusedDisableDirectives: 'warn',
    },
  },

  js.configs.recommended,
  react.configs.flat.recommended,
  react.configs.flat['jsx-runtime'],

  {
    files: ['**/*.{js,jsx}'],
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    settings: {
      react: { version: 'detect' },
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // Rules-of-hooks violations are real bugs — keep them as errors.
      'react-hooks/rules-of-hooks': 'error',
      // The remaining react-hooks rules (React-Compiler-era advisories:
      // dependency completeness, set-state-in-effect, ref/immutability
      // heuristics, manual-memoization) are valuable but flag many existing
      // intentional patterns — surface them as warnings, not baseline errors.
      'react-hooks/exhaustive-deps': 'warn',
      'react-hooks/set-state-in-effect': 'warn',
      'react-hooks/refs': 'warn',
      'react-hooks/immutability': 'warn',
      'react-hooks/preserve-manual-memoization': 'warn',
      // Prop-types are not used in this codebase.
      'react/prop-types': 'off',
      // Allow unescaped quotes/apostrophes in JSX text.
      'react/no-unescaped-entities': 'off',
      // Unused vars are warnings; allow intentionally-unused args/caps prefixes.
      'no-unused-vars': ['warn', { varsIgnorePattern: '^[A-Z_]', argsIgnorePattern: '^_' }],
    },
  },

  // Test files, setup files, and Vite/Vitest config use vitest globals and
  // Node APIs.
  {
    files: [
      '**/*.test.{js,jsx}',
      '**/*.spec.{js,jsx}',
      '**/test/**',
      '**/tests/**',
      '**/e2e/**',
      '**/setup.js',
      '**/*.config.js',
    ],
    languageOptions: {
      globals: {
        ...globals.node,
        ...globals.vitest,
      },
    },
  },

  // react-three-fiber renders three.js objects as JSX host elements whose props
  // (position, args, intensity, …) are not DOM attributes, so the generic
  // unknown-property check does not apply to the XR workspace.
  {
    files: ['frontend/xr/**/*.{js,jsx}'],
    rules: {
      'react/no-unknown-property': 'off',
    },
  },

  // eslint-config-prettier must come last: it disables formatting-related rules.
  prettier,
];
