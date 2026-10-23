import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright configuration for the NovaFlow frontend (Phase 10.1).
 *
 * WHAT THESE TESTS DO AND DO NOT PROVE
 *
 * They drive the real Next.js application in a real browser, so they cover
 * what the Vitest suite cannot: routing, the auth redirect, hydration, and
 * whether a journey actually works end to end in a browser.
 *
 * They do NOT prove the backend works. **No FastAPI server runs during these
 * tests.** Every API response is a deterministic fixture installed with
 * `page.route`. That is a deliberate choice, not a shortcut: the backend has
 * 1100+ tests of its own, and a browser test that needed MongoDB, S3 and
 * Bedrock would be neither runnable here nor deterministic. Full-stack
 * integration remains unverified - see the Phase 10.1 report.
 *
 * `NEXT_PUBLIC_API_URL` is pointed at port 9 (the TCP "discard" port, which
 * reliably refuses connections) rather than the real default of
 * `localhost:8000`. That is a safety property: a test that forgets to install
 * a fixture fails fast with a connection error instead of silently reaching a
 * backend someone happens to be running locally. No test can quietly become a
 * real integration test.
 *
 * Port 3100, not 3000, so a dev server the developer already has open is not
 * disturbed and cannot be mistaken for the server under test.
 */

const PORT = 3100;
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  // Vitest owns `app/**` and `tests/**`; Playwright owns `e2e/**`. The two
  // globs do not overlap, so neither runner ever collects the other's specs.
  // `.spec.ts` only, so `global-setup.ts` and `fixtures/` are not collected
  // as tests.
  testMatch: /.*\.spec\.ts/,

  // Compiles the two routes the suite navigates to before any worker starts.
  // `next dev` compiles on demand, and six workers hitting a cold `/` at once
  // made one test exceed its timeout. See e2e/global-setup.ts.
  globalSetup: "./e2e/global-setup.ts",

  // No blanket retries. A flaky test here is a defect to fix or a locator to
  // correct, and retrying would hide it.
  retries: 0,
  forbidOnly: !!process.env.CI,
  fullyParallel: true,

  // Capped deliberately (Phase 10). Playwright's default is roughly half the
  // CPU count, which put six Chromium instances and an on-demand `next dev`
  // compile server on the same machine. The result was intermittent *test*
  // timeouts - not assertion failures: the captured page snapshots showed the
  // application in exactly the expected state, it just arrived after the 30s
  // budget. That is resource contention, so the fix is fewer concurrent
  // browsers, not a longer timeout and not a retry. Both of those would have
  // left a real slowness unexplained.
  workers: process.env.CI ? 1 : 3,

  reporter: process.env.CI
    ? [["list"], ["html", { open: "never" }]]
    : [["list"]],

  timeout: 30_000,
  expect: { timeout: 10_000 },

  use: {
    baseURL: BASE_URL,
    // Diagnostics on failure only, so a green run writes nothing.
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  webServer: {
    // `next dev` rather than `build` + `start`: it cannot serve a stale build,
    // and it reads NEXT_PUBLIC_* from the environment at runtime, which is
    // what lets the unreachable-API-URL safety property above work at all.
    command: `npm run dev -- --port ${PORT} --hostname 127.0.0.1`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    // A cold Next.js dev compile on this project takes a while.
    timeout: 180_000,
    stdout: "ignore",
    stderr: "pipe",
    env: {
      // See the note above: deliberately unreachable.
      NEXT_PUBLIC_API_URL: "http://127.0.0.1:9",
    },
  },
});
