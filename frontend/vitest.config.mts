import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

/**
 * Vitest configuration for the NovaFlow frontend.
 *
 * The project had no test framework at the Phase 1 baseline. Vitest was chosen
 * over Jest because:
 *   - it needs no Babel/SWC transform config for the existing TS + JSX setup,
 *   - version 3.2.4 is peer-compatible with this project's @types/node ^20 pin,
 *     so adding tests required no unrelated dependency upgrades.
 *
 * Tests render components with @testing-library/react in jsdom and stub
 * `global.fetch`; nothing contacts a real backend.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["app/**/*.test.{ts,tsx}", "tests/**/*.test.{ts,tsx}"],
    // Next.js build output and dependencies are not test sources.
    exclude: ["node_modules/**", ".next/**"],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "."),
    },
  },
});
