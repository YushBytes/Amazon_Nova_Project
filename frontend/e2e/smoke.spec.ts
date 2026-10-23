import { expect, test } from "@playwright/test";

import {
  AGENT,
  API,
  collectPageErrors,
  installApiFallback,
  seedSession,
  stubAuthMe,
  stubRemediationCapabilities,
  stubSubmissions,
  stubWorkerHealth,
} from "./fixtures/api";

/**
 * Journey 1: the application boots and renders in a real browser.
 *
 * The Vitest suite renders components in jsdom. None of it proves the Next.js
 * app actually serves, hydrates and runs - which is what this file covers.
 *
 * Note the fixture order in every test: `installApiFallback` FIRST, specific
 * stubs after. Playwright matches the handler registered last, so reversing
 * this makes the catch-all swallow every stub. See the fixture's docstring.
 */

test.describe("application smoke", () => {
  test("an unauthenticated visit to / lands on the login page", async ({ page }) => {
    // No session seeded: this is the signed-out case. AuthContext must
    // redirect rather than render the shell. It should not call /auth/me at
    // all, because there is no token to verify.
    const api = await installApiFallback(page);

    await page.goto("/");

    await expect(page).toHaveURL(/\/login$/);
    await expect(
      page.getByRole("heading", { level: 1, name: "NovaFlow" })
    ).toBeVisible();
    expect(api.unmatched).toEqual([]);
  });

  test("the login page renders its form with accessible labels", async ({ page }) => {
    await installApiFallback(page);

    await page.goto("/login");

    // By label, not by CSS: if the <label for> association regresses, a
    // screen-reader user loses the field and this test fails with it.
    await expect(page.getByLabel("Username")).toBeVisible();
    await expect(page.getByLabel("Password")).toBeVisible();
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  });

  test("booting the signed-out path raises no uncaught browser error", async ({
    page,
  }) => {
    const errors = collectPageErrors(page);
    await installApiFallback(page);

    await page.goto("/");
    await expect(page).toHaveURL(/\/login$/);
    await page.getByRole("button", { name: "Sign in" }).waitFor();

    expect(errors).toEqual([]);
  });

  test("an authenticated agent reaches the shell and sees real navigation", async ({
    page,
  }) => {
    const errors = collectPageErrors(page);

    const api = await installApiFallback(page);
    await seedSession(page, AGENT);
    await stubAuthMe(page, AGENT);
    await stubSubmissions(page, []);
    await stubRemediationCapabilities(page);
    await stubWorkerHealth(page, {
      queue_depth: 0,
      note: "Inferred from the job queue; the worker is not contacted.",
    });

    await page.goto("/");

    // Stayed on the shell rather than being bounced to /login.
    await expect(page).toHaveURL(/\/$/);
    // The agent's nav, from NAV_ITEMS filtered by role.
    await expect(page.getByRole("button", { name: /Dashboard/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /Your Videos/i })).toBeVisible();

    expect(errors).toEqual([]);
    // Every request was answered by an intended fixture, so the assertions
    // above are about the stubbed shapes and not about a 503 fallback.
    expect(api.unmatched).toEqual([]);
  });

  test("the session-verification placeholder is announced while /auth/me is in flight", async ({
    page,
  }) => {
    // `setIsLoading(false)` is in AuthContext's `finally`, so the shell is
    // gated on this request completing. Holding it open is the only way to
    // observe the loading state rather than a frame that has already passed.
    let release: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });

    await installApiFallback(page);
    await seedSession(page, AGENT);
    await page.route(`${API}/auth/me`, async (route) => {
      await held;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ username: AGENT.username, role: AGENT.role }),
      });
    });

    // `domcontentloaded`, not the default `load`: the held request would
    // otherwise keep the load event pending until the timeout.
    await page.goto("/", { waitUntil: "domcontentloaded" });

    const status = page.getByRole("status");
    await expect(status).toBeVisible();
    await expect(status).toContainText(/Verifying your session/i);

    release?.();
    // And it resolves into the shell once the request completes.
    await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  });
});
