import { defineConfig } from 'vitest/config';
import { cloudflareTest } from '@cloudflare/vitest-plugin';

// No Wrangler AI binding is loaded. Tests supply only a local spy as env.AI.
export default defineConfig({
  plugins: [cloudflareTest({
    remoteBindings: false,
    miniflare: { compatibilityDate: '2026-03-01' }
  })],
  test: { include: ['test/**/*.test.ts'] }
});
