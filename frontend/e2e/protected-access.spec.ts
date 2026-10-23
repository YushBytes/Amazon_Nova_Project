import { expect, test } from "@playwright/test";

import {
  AGENT,
  API,
  CLIENT,
  installApiFallback,
  readStoredToken,
  seedSession,
  seedSessionOnce,
  stubAuthMe,
  stubRemediationCapabilities,
  stubSubmissions,
} from "./fixtures/api";

/**
 * Journey 3: access control, as the application actually implements it.
 *
 * Two things this file is careful NOT to claim.
 *
 * It does not test authorization. The backend is the only authority: every
 * protected route requires a JWT and re-checks ownership per request, and it
 * answers 404 rather than 403 so ids cannot be enumerated. None of that is
 * observable from a browser with stubbed responses, and hiding a nav entry is
 * presentation, not a permission check.
 *
 * What it does test is the client-side half that is genuinely the frontend's
 * job: that a signed-out or expired session cannot render private data, and
 * that the app discards a session the server has rejected instead of leaving
 * the user apparently signed in.
 *
 * The app has only `/` and `/login`. Inner views switch in client state, so
 * there are no deep links to protect and none are invented here.
 */

test.describe("protected access", () => {
  test("a signed-out visit to / renders no private data", async ({ page }) => {
    const api = await installApiFallback(page);

    await page.goto("/");

    await expect(page).toHaveURL(/\/login$/);
    // The shell's navigation and chrome must never have rendered.
    await expect(page.getByRole("navigation", { name: "Main" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);
    // And it did not try to fetch anything owned.
    expect(api.unmatched).toEqual([]);
  });

  test("an expired token is discarded and the user is sent to login", async ({
    page,
  }) => {
    // The server is the authority on whether a cached token is still valid.
    // AuthContext must believe a 401 and clear the session, rather than
    // leaving the user signed in until their next request happens to fail.
    await installApiFallback(page);
    await seedSession(page, AGENT);
    await stubAuthMe(page, null, 401);

    await page.goto("/");

    await expect(page).toHaveURL(/\/login$/);
    expect(await readStoredToken(page)).toBeNull();
    await expect(page.getByRole("navigation", { name: "Main" })).toHaveCount(0);
  });

  test("a 401 clears the cached user record too, not just the token", async ({
    page,
  }) => {
    // Leaving the cached user behind would let the next boot render a name
    // and role for a session the server has already rejected.
    await installApiFallback(page);
    await seedSession(page, AGENT);
    await stubAuthMe(page, null, 401);

    await page.goto("/");
    await expect(page).toHaveURL(/\/login$/);

    const cached = await page.evaluate(() =>
      window.localStorage.getItem("novaflow_user")
    );
    expect(cached).toBeNull();
  });

  test("a server outage keeps the session rather than signing the user out", async ({
    page,
  }) => {
    // Deliberate, and the opposite of the 401 case: a 500 means the server
    // refused this request, not that the session is invalid. Signing the user
    // out on a blip would be its own defect. Any protected request is still
    // rejected server-side if the token really is bad.
    await installApiFallback(page);
    await seedSession(page, AGENT);
    await stubAuthMe(page, null, 500);
    await stubSubmissions(page, []);
    await stubRemediationCapabilities(page);

    await page.goto("/");

    await expect(page).toHaveURL(/\/$/);
    expect(await readStoredToken(page)).not.toBeNull();
  });

  test("the server's role wins over a tampered cached role", async ({ page }) => {
    // A user can edit localStorage freely. If the cache claimed "agent" and
    // the server says "client", the UI must follow the server. This is not a
    // security boundary - the backend enforces roles regardless - but a UI
    // that trusted the cache would show controls the account cannot use.
    await installApiFallback(page);
    await page.addInitScript(() => {
      window.localStorage.setItem("novaflow_token", "tampered-session-token");
      window.localStorage.setItem(
        "novaflow_user",
        JSON.stringify({ username: "e2e-client", role: "agent" })
      );
    });
    await stubAuthMe(page, CLIENT);
    await stubSubmissions(page, []);
    await stubRemediationCapabilities(page);

    await page.goto("/");

    await expect(page).toHaveURL(/\/$/);
    // Agent-only navigation must not appear for an account the server calls
    // a client.
    await expect(
      page.getByRole("button", { name: /Infrastructure/i })
    ).toHaveCount(0);
    await expect(page.getByRole("button", { name: /Your Videos/i })).toBeVisible();
  });

  test("signing out clears the session and returns to login", async ({ page }) => {
    await installApiFallback(page);
    await seedSession(page, AGENT);
    await stubAuthMe(page, AGENT);
    await stubSubmissions(page, []);
    await stubRemediationCapabilities(page);
    await page.route(`${API}/health/worker`, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ queue_depth: 0, note: "Inferred." }),
      })
    );

    await page.goto("/");
    await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();

    await page.getByRole("button", { name: "Sign out" }).click();

    await expect(page).toHaveURL(/\/login$/);
    expect(await readStoredToken(page)).toBeNull();

    // Note the honest limit: this clears the browser's copy. It does NOT
    // revoke the token server-side - the backend has no revocation - so the
    // token stays valid until it expires. The UI does not claim otherwise.
  });

  test("a cleared session cannot be restored by navigating back to /", async ({
    page,
  }) => {
    // `seedSessionOnce`, not `seedSession`: an init script would re-seed the
    // token on the second navigation and this would pass for the wrong
    // reason - or rather, fail, which is how it was caught.
    await installApiFallback(page);
    await stubAuthMe(page, AGENT);
    await stubSubmissions(page, []);
    await stubRemediationCapabilities(page);
    await seedSessionOnce(page, AGENT);

    await page.goto("/");
    await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();

    await page.evaluate(() => window.localStorage.clear());
    await page.goto("/");

    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole("navigation", { name: "Main" })).toHaveCount(0);
  });
});
