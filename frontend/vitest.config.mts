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
    // Next.js build output and dependencies are not test sources. `e2e/**` is
    // Playwright's (its specs use @playwright/test, not Vitest) and would
    // fail to run here, so the two runners never collect each other's files.
    exclude: ["node_modules/**", ".next/**", "e2e/**"],
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      reportsDirectory: "./coverage",
      // Measure the application only. Build config carries no logic worth
      // asserting, and `e2e/fixtures` is Playwright-only support code that
      // Vitest never loads - leaving them in reported 0% for files no unit
      // test is supposed to cover and diluted the figure that matters.
      //
      // No thresholds are set: a threshold here would be a number to satisfy,
      // and the uncovered branches are listed in the Phase 10.1 report
      // instead so they can be judged on merit.
      include: ["app/**/*.{ts,tsx}"],
      exclude: [
        "app/**/*.test.{ts,tsx}",
        "app/layout.tsx",
        "**/*.d.ts",
      ],
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "."),
    },
  },
});
