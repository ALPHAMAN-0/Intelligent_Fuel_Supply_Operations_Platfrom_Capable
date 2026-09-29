import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.js'],
    coverage: {
      provider: 'v8',
      include: ['js/**/*.js'],
      exclude: ['js/login-page.js', 'js/console-page.js'],  // two-line bootstraps; behaviour is tested via their modules
      thresholds: { lines: 80, functions: 80, branches: 80, statements: 80 },
    },
  },
});
