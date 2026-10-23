import { expect, test } from "@playwright/test";

import {
  AGENT,
  API,
  CLIENT,
  collectPageErrors,
  errorAlert,
  installApiFallback,
  readStoredToken,
  stubAuthMe,
  stubLoginFailure,
  stubLoginSuccess,
  stubLoginThrottled,
  stubRemediationCapabilities,
  stubSubmissions,
  stubWorkerHealth,
} from "./fixtures/api";

/**
 * Journey 2: signing in.
 *
 * The property that matters most here is the negative one: a REJECTED login
 * must not leave anything resembling a session behind. A bug that stored the
 * token before the request resolved, or that navigated on failure, would be a
 * real authentication defect, so each failure test asserts on localStorage
 * and on the URL rather than only on the visible message.
 */

test.describe("login", () => {
  test("empty fields are blocked by the browser without any request", async ({
    page,
  }) => {
    const api = await installApiFallback(page);
    await page.goto("/login");

    await page.getByRole("button", { name: "Sign in" }).click();

    // Both inputs are `required`, so native validation stops submission.
    // Nothing reached the network, and we are still on /login.
    expect(api.unmatched).toEqual([]);
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByLabel("Username")).toBeFocused();
  });

  test("a username with no password still does not submit", async ({ page }) => {
    const api = await installApiFallback(page);
    await page.goto("/login");

    await page.getByLabel("Username").fill("someone");
    await page.getByRole("button", { name: "Sign in" }).click();

    expect(api.unmatched).toEqual([]);
    await expect(page).toHaveURL(/\/login$/);
  });

  test("a rejected login shows the server's message in an alert", async ({
    page,
  }) => {
    await installApiFallback(page);
    await stubLoginFailure(page, "Incorrect username or password");

    await page.goto("/login");
    await page.getByLabel("Username").fill("wrong-user");
    await page.getByLabel("Password").fill("wrong-password");
    await page.getByRole("button", { name: "Sign in" }).click();

    // role="alert" so a screen reader announces the failure.
    const alert = errorAlert(page);
    await expect(alert).toBeVisible();
    await expect(alert).toContainText("Incorrect username or password");
  });

  test("a rejected login establishes no session at all", async ({ page }) => {
    await installApiFallback(page);
    await stubLoginFailure(page);

    await page.goto("/login");
    await page.getByLabel("Username").fill("wrong-user");
    await page.getByLabel("Password").fill("wrong-password");
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(errorAlert(page)).toBeVisible();

    // The three things that would each be an authentication defect.
    await expect(page).toHaveURL(/\/login$/);
    expect(await readStoredToken(page)).toBeNull();
    await expect(page.getByRole("navigation", { name: "Main" })).toHaveCount(0);
  });

  test("the error message does not reveal which field was wrong", async ({
    page,
  }) => {
    // The backend deliberately does not distinguish; the UI must not either.
    await installApiFallback(page);
    await stubLoginFailure(page, "Incorrect username or password");

    await page.goto("/login");
    await page.getByLabel("Username").fill("wrong-user");
    await page.getByLabel("Password").fill("wrong-password");
    await page.getByRole("button", { name: "Sign in" }).click();

    const text = (await errorAlert(page).textContent()) ?? "";
    expect(text).not.toMatch(/no such user|unknown user|user not found/i);
    expect(text).not.toMatch(/wrong password|bad password|password incorrect/i);
  });

  test("the submitted password is never written into the DOM", async ({ page }) => {
    await installApiFallback(page);
    await stubLoginFailure(page);

    await page.goto("/login");
    await page.getByLabel("Username").fill("wrong-user");
    await page.getByLabel("Password").fill("super-secret-value");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(errorAlert(page)).toBeVisible();

    // The controlled input holds it as a value; it must not appear as text.
    const body = (await page.locator("body").innerText()) ?? "";
    expect(body).not.toContain("super-secret-value");
  });

  test("a throttled login reports the wait instead of a rejected password", async ({
    page,
  }) => {
    await installApiFallback(page);
    await stubLoginThrottled(page, 42);

    await page.goto("/login");
    await page.getByLabel("Username").fill("someone");
    await page.getByLabel("Password").fill("some-password");
    await page.getByRole("button", { name: "Sign in" }).click();

    const alert = errorAlert(page);
    await expect(alert).toBeVisible();
    await expect(alert).toContainText(/Too many attempts/i);

    // The user is told this is temporary, not that their password was wrong.
    // That distinction is the point of the branch.
    const text = (await alert.textContent()) ?? "";
    expect(text).not.toMatch(/incorrect|invalid|wrong/i);

    // FIXED in Phase 10 finalization. This test previously asserted the
    // DEGRADED message ("a moment"), because `Retry-After` is not a
    // CORS-safelisted response header and the backend's CORSMiddleware named
    // no `expose_headers` - so the browser hid it, `retryAfterSeconds` was
    // null, and the exact wait could never be shown cross-origin. The 391
    // jsdom tests all passed over that broken path because they stub `fetch`
    // and never apply CORS filtering; only this browser test could see it.
    //
    // `app.py` now sets `expose_headers=["Retry-After"]`, and the fixture
    // models that, so the real wait is reachable and asserted here.
    await expect(alert).toContainText("42 seconds");
    await expect(alert).not.toContainText("a moment");

    expect(await readStoredToken(page)).toBeNull();
  });

  test("an unreachable backend reports a failure rather than signing in", async ({
    page,
  }) => {
    // No login stub at all: the fallback answers 503, which is what a down
    // backend looks like to the browser.
    await installApiFallback(page);

    await page.goto("/login");
    await page.getByLabel("Username").fill("someone");
    await page.getByLabel("Password").fill("some-password");
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(errorAlert(page)).toBeVisible();
    expect(await readStoredToken(page)).toBeNull();
    await expect(page).toHaveURL(/\/login$/);
  });

  test("a successful login stores the session and reaches the shell", async ({
    page,
  }) => {
    const errors = collectPageErrors(page);

    const api = await installApiFallback(page);
    await stubLoginSuccess(page, AGENT);
    await stubAuthMe(page, AGENT);
    await stubSubmissions(page, []);
    await stubRemediationCapabilities(page);
    await stubWorkerHealth(page, { queue_depth: 0, note: "Inferred." });

    await page.goto("/login");
    await page.getByLabel("Username").fill(AGENT.username);
    await page.getByLabel("Password").fill("correct-horse-battery-staple");
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
    expect(await readStoredToken(page)).not.toBeNull();

    expect(errors).toEqual([]);
    expect(api.unmatched).toEqual([]);
  });

  test("a client signing in gets the client navigation, not the agent's", async ({
    page,
  }) => {
    // Role comes from the server's response, never from the client. The
    // backend is the authority; this checks the UI honours what it is told.
    const api = await installApiFallback(page);
    await stubLoginSuccess(page, CLIENT);
    await stubAuthMe(page, CLIENT);
    await stubSubmissions(page, []);
    await stubRemediationCapabilities(page);

    await page.goto("/login");
    await page.getByLabel("Username").fill(CLIENT.username);
    await page.getByLabel("Password").fill("some-password");
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("button", { name: /Your Videos/i })).toBeVisible();
    // Agent-only entries are absent for a client.
    await expect(
      page.getByRole("button", { name: /Infrastructure/i })
    ).toHaveCount(0);
    expect(api.unmatched).toEqual([]);
  });

  test("the sign-up toggle switches the form and clears a stale error", async ({
    page,
  }) => {
    await installApiFallback(page);
    await stubLoginFailure(page);

    await page.goto("/login");
    await page.getByLabel("Username").fill("wrong-user");
    await page.getByLabel("Password").fill("wrong-password");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(errorAlert(page)).toBeVisible();

    await page.getByRole("button", { name: /PROTOCOL_SIGNUP/ }).click();

    await expect(page.getByRole("button", { name: "Create account" })).toBeVisible();
    await expect(errorAlert(page)).toHaveCount(0);
    // The real role policy is stated rather than offered as a choice.
    await expect(
      page.getByText(/New accounts are created as Client/i)
    ).toBeVisible();
  });

  test("the sign-up form offers no role selector", async ({ page }) => {
    // Public signup always creates a least-privileged account server-side. A
    // role control would imply the client could choose, which it cannot.
    await installApiFallback(page);
    await page.goto("/login");
    await page.getByRole("button", { name: /PROTOCOL_SIGNUP/ }).click();

    await expect(page.locator("select")).toHaveCount(0);
    await expect(page.getByRole("radio")).toHaveCount(0);
    await expect(page.getByRole("combobox")).toHaveCount(0);
  });

  test("the submit button is disabled while a login is in flight", async ({
    page,
  }) => {
    // This disabled state IS the duplicate-submit guard, so assert it
    // directly rather than inferring it from a request count. Two POSTs would
    // mean two rate-limit counter increments for one user action.
    let posts = 0;
    let release: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });

    await installApiFallback(page);
    await page.route(`${API}/auth/login`, async (route) => {
      posts += 1;
      await held;
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Incorrect username or password" }),
      });
    });

    await page.goto("/login");
    await page.getByLabel("Username").fill("someone");
    await page.getByLabel("Password").fill("some-password");

    const submit = page.getByRole("button", { name: "Sign in" });
    await submit.click();

    // Mid-flight: disabled, and labelled with the work in progress.
    const inFlight = page.getByRole("button", { name: "Signing in" });
    await expect(inFlight).toBeVisible();
    await expect(inFlight).toBeDisabled();

    release?.();
    await expect(errorAlert(page)).toBeVisible();
    expect(posts).toBe(1);

    // Re-enabled afterwards, so a genuine retry is still possible.
    await expect(page.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  test("a real double-click sends only one login request", async ({ page }) => {
    // `dblclick` dispatches two genuine clicks the way a user would, rather
    // than a forced click that bypasses the disabled attribute. A forced
    // click within the same React commit CAN double-submit, but that is not
    // reachable by a human: React commits the disabled state in well under
    // the gap between two real clicks. Noted in the Phase 10.1 report.
    let posts = 0;
    await installApiFallback(page);
    await page.route(`${API}/auth/login`, async (route) => {
      posts += 1;
      await new Promise((resolve) => setTimeout(resolve, 400));
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Incorrect username or password" }),
      });
    });

    await page.goto("/login");
    await page.getByLabel("Username").fill("someone");
    await page.getByLabel("Password").fill("some-password");
    await page.getByRole("button", { name: "Sign in" }).dblclick();

    await expect(errorAlert(page)).toBeVisible();
    expect(posts).toBe(1);
  });
});
