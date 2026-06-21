// ESLint flat config — DEV-ONLY tooling for the browser UI.
// Not part of the Python package; see package.json devDependencies.
// Run: npm install  (once)  then  npm run lint
import js from '@eslint/js';
import globals from 'globals';

export default [
  {
    files: ['src/flightmanager/templates/js/**/*.js'],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: 'module',
      globals: {
        ...globals.browser,
        // Third-party libs loaded via <script> before the module bundle
        L: 'readonly', // Leaflet
        Cesium: 'readonly', // CesiumJS (lazy-loaded from CDN)
      },
    },
    rules: {
      ...js.configs.recommended.rules,
      // Leaked globals / typos in identifiers — the highest-value rule here.
      'no-undef': 'error',
      // Unused imports and dead locals.
      'no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
      // Pragmatic: the codebase intentionally uses some empty catch blocks.
      'no-empty': ['warn', { allowEmptyCatch: true }],
    },
  },
];
