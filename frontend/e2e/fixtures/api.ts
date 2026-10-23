import type { Page, Route } from "@playwright/test";

/**
 * Deterministic API fixtures for the Playwright journeys (Phase 10.1).
 *
 * READ THIS BEFORE TRUSTING A GREEN E2E RUN.
 *
 * Nothing here contacts a backend. `playwright.config.ts` points
 * `NEXT_PUBLIC_API_URL` at the TCP discard port, so an un-stubbed request
 * fails with a connection error rather than silently reaching a real server.
 * Everything below is a fixture, and a fixture proves only that the FRONTEND
 * handles that shape correctly. It is not evidence about the backend, and a
 * corrected video served from `fulfill` is **not** AI output - it is bytes
 * this file made up.
 *
 * Token values are synthetic strings, not real JWTs: the frontend treats the
 * token as an opaque bearer credential and never parses it, so a realistic
 * signature would add nothing and a real one must never be committed.
 */

export const API = "http://127.0.0.1:9";

export const TOKEN_STORAGE_KEY = "novaflow_token";
export const USER_STORAGE_KEY = "novaflow_user";

/** Obviously-synthetic, and clearly not a real signed token. */
export const FAKE_TOKEN = "e2e-synthetic-token-not-a-real-jwt";

export interface SeedUser {
  username: string;
  role: "agent" | "client";
}

export const AGENT: SeedUser = { username: "e2e-agent", role: "agent" };
export const CLIENT: SeedUser = { username: "e2e-client", role: "client" };

function json(route: Route, status: number, body: unknown) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

/**
 * Put a valid-looking session in localStorage *before* the app boots.
 *
 * `addInitScript` runs before any page script, which matters: AuthContext
 * reads localStorage during its first effect, so writing the token after
 * navigation would race the redirect to /login.
 *
 * This seeds the CLIENT's cache only. It is not an authorization bypass -
 * AuthContext still revalidates the token against GET /auth/me before
 * trusting it, so `stubAuthMe` must agree or the session is discarded. That
 * revalidation is itself one of the things these tests check.
 */
export async function seedSession(page: Page, user: SeedUser = AGENT) {
  await page.addInitScript(
    ([tokenKey, userKey, token, record]) => {
      window.localStorage.setItem(tokenKey as string, token as string);
      window.localStorage.setItem(userKey as string, record as string);
    },
    [
      TOKEN_STORAGE_KEY,
      USER_STORAGE_KEY,
      FAKE_TOKEN,
      JSON.stringify({ username: user.username, role: user.role }),
    ] as const
  );
}

/**
 * Seed a session ONCE, for tests that then clear it and navigate again.
 *
 * `seedSession` uses `addInitScript`, which re-runs on every navigation - so
 * a test that clears storage and reloads would silently get the token back,
 * and an assertion about the signed-out state would pass or fail for the
 * wrong reason. This writes storage a single time from an already-loaded
 * same-origin page instead.
 *
 * Call before navigating to `/`.
 */
export async function seedSessionOnce(page: Page, user: SeedUser = AGENT) {
  // /login is the only page that renders without a session.
  await page.goto("/login");
  await page.evaluate(
    ([tokenKey, userKey, token, record]) => {
      window.localStorage.setItem(tokenKey as string, token as string);
      window.localStorage.setItem(userKey as string, record as string);
    },
    [
      TOKEN_STORAGE_KEY,
      USER_STORAGE_KEY,
      FAKE_TOKEN,
      JSON.stringify({ username: user.username, role: user.role }),
    ] as const
  );
}

/** GET /auth/me — the session revalidation the app does on every boot. */
export async function stubAuthMe(
  page: Page,
  user: SeedUser | null = AGENT,
  status = 200
) {
  await page.route(`${API}/auth/me`, (route) => {
    if (!user) return json(route, status, { detail: "Not authenticated" });
    return json(route, 200, { username: user.username, role: user.role });
  });
}

/** POST /auth/login — success. */
export async function stubLoginSuccess(page: Page, user: SeedUser = AGENT) {
  await page.route(`${API}/auth/login`, (route) =>
    json(route, 200, {
      access_token: FAKE_TOKEN,
      token_type: "bearer",
      role: user.role,
    })
  );
}

/**
 * POST /auth/login — rejection, in the backend's real shape.
 *
 * FastAPI returns `{"detail": "..."}`, and the login page surfaces that
 * `detail` verbatim. The real backend deliberately does not say whether the
 * username or the password was wrong, so neither does this.
 */
export async function stubLoginFailure(
  page: Page,
  detail = "Incorrect username or password",
  status = 401
) {
  await page.route(`${API}/auth/login`, (route) => json(route, status, { detail }));
}

/**
 * POST /auth/login — rate limited, with a readable Retry-After.
 *
 * `access-control-expose-headers` is set deliberately, because the real
 * backend now sets it (`app.py`'s CORSMiddleware, `expose_headers`). Without
 * that header the browser hides `Retry-After` from JavaScript on a
 * cross-origin response, and this fixture is cross-origin: the page is served
 * from port 3100 and the API is port 9.
 *
 * That is not incidental - it is how the defect was found. The fixture
 * originally omitted it, exactly matching the unfixed server, and the UI
 * degraded to "a moment" in this very test while all 391 jsdom tests passed,
 * because those stub `fetch` and never apply CORS filtering. The fixture now
 * models the FIXED server, so this suite would catch a regression that
 * removed `expose_headers`.
 */
export async function stubLoginThrottled(page: Page, retryAfter = 42) {
  await page.route(`${API}/auth/login`, (route) =>
    route.fulfill({
      status: 429,
      contentType: "application/json",
      headers: {
        "retry-after": String(retryAfter),
        "access-control-expose-headers": "Retry-After",
      },
      body: JSON.stringify({ detail: "Too many attempts" }),
    })
  );
}

export interface SubmissionFixture {
  id: string;
  filename?: string;
  original_filename?: string | null;
  status?: string;
  created_at?: string | null;
  size_bytes?: number;
  url?: string;
}

/** GET /submissions */
export async function stubSubmissions(
  page: Page,
  submissions: SubmissionFixture[]
) {
  await page.route(`${API}/submissions`, (route) =>
    json(route, 200, { submissions })
  );
}

/** GET /submissions — failing, for the error-state journey. */
export async function stubSubmissionsError(page: Page, status = 500) {
  await page.route(`${API}/submissions`, (route) =>
    json(route, status, { detail: "Internal server error" })
  );
}

/** GET /jobs/{id} */
export async function stubJob(page: Page, videoId: string, job: unknown) {
  await page.route(`${API}/jobs/${videoId}`, (route) => json(route, 200, job));
}

/** GET /violations/{id} */
export async function stubViolations(
  page: Page,
  videoId: string,
  detections: unknown[]
) {
  await page.route(`${API}/violations/${videoId}**`, (route) =>
    json(route, 200, { video_id: videoId, detections, count: detections.length })
  );
}

/**
 * GET /remediations/capabilities
 *
 * Field names match `RemediationCapabilities` in app/lib/api.ts exactly. They
 * matter: the panel keys its "generation unavailable" and "reconstruction
 * unavailable" notices off `generation_available` and
 * `reconstruction_available`, so an invented field name silently produces a
 * panel with no notices at all.
 *
 * The default describes this build truthfully: proposals and owner-supplied
 * assets are supported, automated image generation is NOT.
 */
export async function stubRemediationCapabilities(
  page: Page,
  body: unknown = {
    remediation_enabled: true,
    proposals_supported: true,
    generation_available: false,
    generation_provider: "none",
    generation_unavailable_reason:
      "No image provider is configured on this deployment.",
    min_confidence: 0.8,
    applies_assets_to_video: true,
    verifies_compliance: true,
    notes: "Owner-supplied replacement images only.",
    asset_upload_supported: true,
    max_asset_bytes: 8388608,
    accepted_asset_types: ["image/png", "image/jpeg"],
    reconstruction_available: true,
    reconstruction_unavailable_reason: null,
    reconstruction_method: "scaled_overlay",
  }
) {
  await page.route(`${API}/remediations/capabilities`, (route) =>
    json(route, 200, body)
  );
}

/**
 * GET /remediations?video_id=...
 *
 * Matched with a predicate rather than a glob. A glob containing `?` does not
 * reliably match a query string, and the silent result is a route that never
 * fires - which reads as "the component rendered nothing" rather than as a
 * missing fixture.
 */
export async function stubRemediationList(page: Page, remediations: unknown[]) {
  await page.route(
    (url) =>
      url.origin === API &&
      url.pathname === "/remediations" &&
      url.searchParams.has("video_id"),
    (route) => json(route, 200, { remediations })
  );
}

/** GET /health/worker */
export async function stubWorkerHealth(page: Page, body: unknown) {
  await page.route(`${API}/health/worker`, (route) => json(route, 200, body));
}

export interface ApiFallback {
  /** URLs that reached the fallback because no specific stub claimed them. */
  unmatched: string[];
}

/**
 * Fail every API call that no specific stub claimed, and record it.
 *
 * **Install this FIRST**, before any specific stub. Playwright matches the
 * handler registered LAST, so a catch-all added last silently shadows every
 * specific stub - verified by probe during Phase 10.1, and it had already
 * made one test pass for the wrong reason: the catch-all answered `/auth/me`
 * with 503, which AuthContext treats as "server briefly unreachable, keep the
 * cached session", so the shell rendered and the assertion passed without the
 * intended fixture ever being used.
 *
 * Returned handle's `unmatched` is why that cannot happen again quietly:
 * assert it is empty and a forgotten fixture becomes a named failure instead
 * of a plausible-looking pass.
 */
export async function installApiFallback(page: Page): Promise<ApiFallback> {
  const fallback: ApiFallback = { unmatched: [] };

  await page.route(`${API}/**`, (route) => {
    // Path only: a playback URL carries a media token in its query string,
    // and the frontend's own rule is never to log a request target.
    const path = new URL(route.request().url()).pathname;
    fallback.unmatched.push(`${route.request().method()} ${path}`);
    return json(route, 503, { detail: "No fixture installed for this request" });
  });

  return fallback;
}

/**
 * Collect uncaught page errors and console errors for assertion.
 *
 * Returns a live array. Next.js dev-mode noise is filtered out: a hydration
 * warning or a React DevTools suggestion is not an application defect, and
 * failing on it would make the suite useless. Genuine uncaught exceptions are
 * always kept.
 */
export function collectPageErrors(page: Page) {
  const errors: string[] = [];
  const IGNORE = [
    "Download the React DevTools",
    "react-devtools",
    "[Fast Refresh]",
    "Failed to load resource",
  ];

  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const text = message.text();
    if (IGNORE.some((pattern) => text.includes(pattern))) return;
    errors.push(`console.error: ${text}`);
  });

  return errors;
}

/**
 * The application's own error alert, excluding Next.js's route announcer.
 *
 * Next injects `<div role="alert" id="__next-route-announcer__">` into every
 * page for screen-reader route changes, so a bare `getByRole("alert")` matches
 * two elements and fails Playwright's strict mode - and, worse, a
 * `toHaveCount(0)` assertion against it could never pass even when the app
 * renders no error. Scoping to the app's own alert is what makes both
 * directions meaningful.
 */
export function errorAlert(page: Page) {
  return page.locator('[role="alert"]:not(#__next-route-announcer__)');
}

/** Read the stored bearer token, or null. Used to assert no false session. */
export async function readStoredToken(page: Page): Promise<string | null> {
  return page.evaluate(
    (key) => window.localStorage.getItem(key as string),
    TOKEN_STORAGE_KEY
  );
}
