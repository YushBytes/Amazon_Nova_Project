import type { FullConfig } from "@playwright/test";

/**
 * Warm the Next.js dev server before any test runs (Phase 10).
 *
 * WHY THIS EXISTS. `next dev` compiles a route the first time it is
 * requested. With `fullyParallel` and six workers, six tests can request `/`
 * at the same instant on a cold server, and the first navigation then takes
 * longer than the 10s assertion timeout. That produced exactly one flaky
 * failure in a full run - "a successful login stores the session and reaches
 * the shell" - which passed on its own and in three earlier full runs.
 *
 * The honest fixes were: raise the timeout (hides slowness), add a retry
 * (hides the flake entirely), or remove the cold-compile variable. This is the
 * third. Both routes the suite navigates to are compiled once, here, before
 * any worker starts asserting.
 *
 * It deliberately does NOT swallow failures: if the dev server cannot serve
 * its own routes, every test is about to fail anyway and the cause should be
 * reported here rather than as twelve confusing timeouts.
 */
async function globalSetup(config: FullConfig) {
  const baseURL =
    config.projects[0]?.use?.baseURL ?? "http://127.0.0.1:3100";

  // `/` redirects to `/login` for a signed-out visitor, so requesting both
  // compiles both. The response status is not asserted beyond "the server
  // answered": `/` legitimately returns a redirect.
  for (const path of ["/login", "/"]) {
    const url = new URL(path, baseURL).toString();
    const response = await fetch(url, { redirect: "follow" });
    if (!response.ok) {
      throw new Error(
        `Warm-up request to ${path} failed with HTTP ${response.status}. ` +
          `The dev server is not serving the application.`
      );
    }
    // Drain the body so the connection closes and the compile completes.
    await response.text();
  }
}

export default globalSetup;
