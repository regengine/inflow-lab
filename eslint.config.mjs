import globals from 'globals';

// The console is a single non-module browser script loaded straight from
// index.html: no build step, no bundler, no import graph. So this config is
// deliberately small -- it exists to fail on the classes of defect that
// actually bit this file (an unused binding left behind by a refactor, an
// unhandled rejection, a typo'd global) rather than to impose a style.
export default [
  {
    files: ['app/static/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'script',
      globals: {
        ...globals.browser,
      },
    },
    rules: {
      // Catches a binding left behind by a refactor, and a name that only
      // exists because of a typo -- the two failure modes this file has no
      // other signal for.
      'no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      'no-undef': 'error',
      'no-async-promise-executor': 'error',
      'no-var': 'error',
      'prefer-const': 'error',
      eqeqeq: ['error', 'smart'],
      // Deliberately off:
      //   no-implicit-globals   -- the file is one non-module script by
      //     design, so every top-level function trips it. Splitting it into
      //     ES modules is #154; until that lands this rule reports the
      //     architecture, not a defect.
      //   require-atomic-updates -- fires on every `ids.foo.value = ...` after
      //     an await. `ids` is a frozen table of DOM references resolved once
      //     at load, so there is no state to race on.
    },
  },
];
