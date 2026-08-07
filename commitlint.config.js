// commitlint configuration for conventional commits.
//
// The release-please workflow relies on commit messages following
// the conventional-commits spec to decide whether to bump the
// major, minor, or patch version. A PR whose commits do not match
// will fail the commitlint job, blocking merge.
//
// Allowed types come from the Angular convention and align with
// the changelog-sections in release-please-config.json.
module.exports = {
  extends: ['@commitlint/config-conventional'],
  rules: {
    // Match the changelog sections exactly.
    'type-enum': [
      2,
      'always',
      [
        'feat',     // Features
        'fix',      // Bug Fixes
        'perf',     // Performance Improvements
        'refactor', // Refactoring
        'docs',     // Documentation
        'test',     // Tests
        'build',    // Build System
        'ci',       // Continuous Integration
        'chore',    // Miscellaneous Chores
        'revert',   // Revert a previous commit
        'style',    // Whitespace / formatting (no logic change)
      ],
    ],
    // Subject must be <= 100 chars so it fits in the changelog.
    'header-max-length': [2, 'always', 100],
    // Subject must not end with a period \u2014 conventional-commits style.
    'header-full-stop': [2, 'never', '.'],
    // Type + scope + colon must be lowercase: feat(rpc): ...
    'type-case': [2, 'always', 'lower-case'],
    'scope-case': [2, 'always', 'lower-case'],
  },
};
