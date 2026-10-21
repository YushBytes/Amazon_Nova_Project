# NovaFlow frontend

The Next.js dashboard for NovaFlow / Brand Guardian. It is a client for the
FastAPI backend in `../backend`; it holds no business logic and is not an
authorization boundary.

This file replaced the `create-next-app` boilerplate in Phase 7. That template
described a Geist font this project does not use and a Vercel deployment this
project does not have.

---

## What this screen set actually does

Every view below is listed with the endpoint behind it. A view with no
endpoint says so on screen rather than showing invented data — see
"Deliberately unavailable".

| View | Backed by | Notes |
| :-- | :-- | :-- |
| `/login` — sign in / register | `POST /auth/login`, `POST /auth/signup` | No role is ever sent. Public signup always creates a Client. |
| Session restore | `GET /auth/me` | The server's answer is authoritative for both username and role; `localStorage` is never trusted. |
| Dashboard (agent only) | `POST /upload-reel`, `POST /analyze/{id}`, `GET /jobs/{id}`, `GET /violations/{id}`, `POST /analyze/{id}/retry` | Upload → audit → findings, with job-state polling. |
| Your Videos | `GET /submissions` | The caller's own uploads only, with each one's real job state. |
| Audit detail | `GET /jobs/{id}`, `GET /violations/{id}` | The verdict and the findings. Can also start or re-run an analysis. |
| Compliance Log | `GET /submissions` | The caller's audit history, filterable by real job state. |
| Infrastructure (agent only) | `GET /health/worker` | Job-queue counts and inferred worker health. |
| Account | `GET /auth/me` (via the auth context) | The signed-in identity. Nothing else; see below. |

There are only three routes: `/`, `/login`, and the generated `/_not-found`.
The shell at `/` switches views in client state rather than by URL, so a view
is not deep-linkable. That is a known limitation, not a design goal.

### Deliberately unavailable

These panels state that a capability does not exist instead of simulating it:

- **Custom audit instructions / brand rules.** Audits run against one fixed
  prompt in `backend/prompts.py`. The route that an instruction box would have
  posted to (`POST /execute`) returns placeholder text from an unimplemented
  orchestrator, so no control is offered.
- **Account management.** There is no endpoint to change a password, edit a
  profile, list sessions, or delete an account. Signing out clears this
  browser's token; it does **not** revoke it server-side, and the panel says so.
- **Per-worker telemetry.** `/health/worker` infers queue health; it does not
  contact the worker process. The endpoint's own caveat is rendered.

### Removed in Phase 7

Screens that presented hardcoded data as live application state. Recorded here
so the patterns are recognisable if they reappear; each has a regression test.

- `AgentsView` — two invented "operational units" with stock photographs and
  ONLINE/SYNCING badges, plus "Logic Load 42%", "Vision Latency 68ms", "99.9%
  uptime", "2.1ms sync", and an "Expand Cluster" button wired to nothing.
- `LogsView` — three hardcoded physical-security "incidents" ("Unauthorized
  Access Attempt", "Tailgating Detected") under a "Live Anomaly Detection"
  subtitle with a "LIVE AI" badge.
- `ProjectDetailView` — an **unconditional** "AUDIT PASSED / verified by
  NovaFlow Neural Agent #xyz / Compliance & quality standards met" card, shown
  for every video regardless of whether analysis had run, was running, or had
  failed. Also "Encoding: H.265 Neural", "Sync Integrity 99.8%", a "Turbo
  Boost" toggle, and a "Post to Social" button.
- `ClientPortal` — a "VERIFIED" shield badge keyed to a status string the API
  never returns, filter tabs naming a model ("LLAVA-CORE") and an agent
  ("ACT-AGENT") that do not exist, and a "Post" button for social publishing.
- `page.tsx` — an account screen for "Commander Niru, Level 10 Overseer" with
  a generated third-party avatar and a "Security Clearance" panel ("Bio-Sync
  98% VERIFIED"), plus a header "Sync: US-EAST-1" indicator and a
  notification bell with an unread dot.
- `HUDView` — a placeholder video that played a sample clip from an MDN
  domain as though it were the user's footage, fabricated "Network 82 / Nodes
  54" gauges, and a "Max 60MB / 60S" hint that did not match the backend.
- `/login` — a footer claiming "Security Hash: SHA-256 (AUTO)" and "AES-256
  Encryption Active" (passwords are Argon2id; nothing here does AES), and a
  role selector that sent nothing and changed nothing while implying the
  client could pick its own privilege.

---

## Running it

Requires Node 20+ (developed on v24.18.1) and a backend at
`NEXT_PUBLIC_API_URL`.

```bash
cd frontend
npm install
npm run dev            # http://localhost:3000
```

The dashboard needs a signed-in account, so the backend must be reachable:

```bash
cd backend && uvicorn app:app --host 0.0.0.0 --port 8000
```

Without a backend, `/login` renders and reports "Could not reach the server";
`/` redirects to `/login` because the session cannot be verified.

### Environment variables

All of them are `NEXT_PUBLIC_*`, which means **they are compiled into the
browser bundle and are public**. Never put a secret in one. The full template
is `../.env.example`.

| Name | Required | Default | Purpose |
| :-- | :-- | :-- | :-- |
| `NEXT_PUBLIC_API_URL` | No | `http://localhost:8000` | Backend origin. Every request goes through `app/lib/api.ts`, which is the only place this is read. |
| `NEXT_PUBLIC_MAX_UPLOAD_BYTES` | No | `104857600` (100 MiB) | The cap the **browser** enforces before transferring, and the text beside the file picker. |
| `NEXT_PUBLIC_ALLOWED_VIDEO_EXTENSIONS` | No | `mp4,mov,avi,webm,mkv` | The extensions the **browser** accepts. |

The last two mirror the backend's `MAX_UPLOAD_BYTES` and
`ALLOWED_VIDEO_EXTENSIONS`. The browser cannot read the server's
configuration, so client-side validation is a pre-flight courtesy: the backend
validates independently and wins, and its rejection message is surfaced
verbatim. **If you narrow the server limits, set these to match** — otherwise
the UI advertises a limit the server refuses.

---

## Checks

```bash
npm test          # 263 tests (Vitest + @testing-library/react, jsdom)
npm run typecheck # tsc --noEmit
npm run lint      # eslint app
npm run build     # next build
```

All four pass as of Phase 7: 263 tests, 0 type errors, **0 lint errors and 0
lint warnings**, and a successful build of `/`, `/_not-found` and `/login`.

**Order matters after deleting a route.** `npm run typecheck` reads generated
validators under `.next/types/`, which still reference a removed
`app/<route>/page.tsx` until `npm run build` regenerates them. A failure there
right after a route deletion is stale, not a real type error — run
`npm run build` first.

### What the tests do and do not prove

`fetch` is stubbed at the network boundary in every test; nothing contacts a
backend, AWS, or Bedrock. They prove the UI renders the right thing for a
given API response — including that it refuses to overstate one. They prove
**nothing** about real backend behaviour, real AWS integration, or detection
accuracy.

There is no end-to-end test covering login → upload → analyze → review
against a live backend, and no browser-automation stack. That is Phase 10.

---

## How it talks to the backend

`app/lib/api.ts` is the single API layer. Everything goes through it:

- One `requestJson` helper does the status check, the defensive `detail`
  parse, and the bearer-token attachment. `ApiError` carries the HTTP status,
  the backend's own `detail`, and `Retry-After` when present.
- **No request URL is ever logged.** A playback URL carries a media token in
  its query string, so logging the target would leak a credential to the
  browser console. Errors carry a status and a message, never the URL.
- Every read accepts an `AbortSignal`, and each view aborts on unmount, so a
  late response cannot update a gone component or overwrite newer state.
- Uploads and analysis starts are **never retried automatically**: a repeated
  upload stores a second copy, and a repeated analysis bills the model again
  per frame. Retry is always an explicit user action.
- Polling is scheduled only after the previous request settles, so requests
  cannot overlap or stack up behind a slow response.

### The rule that governs every status display

`app/lib/jobs.ts` owns it, and four views share it:

> A job is finished only when the backend reports `state === "completed"`,
> which it sets solely after analysis finished **and** its results were
> persisted. An empty detections list means "no violations found" only in that
> state. In any other state an empty list means *unknown*.

So a failed or still-running analysis is never rendered as compliant, and a
missing confidence stays missing rather than becoming a fabricated `0%`.

### Authentication

The session token lives in `localStorage` and is sent as a bearer header. On
load, the stored token is verified against `GET /auth/me` rather than trusted;
a 401 clears it and redirects, while a network failure keeps the cached
session (the backend being briefly unreachable is not proof the token is bad).
An unrecognised role is narrowed to the least-privileged one.

Role-based navigation is **presentation only**. Hiding a nav entry is not
authorization: the backend authorizes every route and re-checks ownership per
request, returning 404 (not 403) for another user's video so ids cannot be
enumerated.

---

## Known limitations

- **Views are not URL-addressable.** `/` switches views in client state, so
  nothing below the shell can be linked, bookmarked, or opened in a new tab,
  and the browser's back button does not move between views.
- **The audit detail view does not poll.** It loads once; a run queued from it
  needs a manual reload to follow. Only the dashboard polls.
- **Search and filtering are client-side**, over the list already fetched.
  There is no search endpoint; `GET /submissions` caps at 100 records.
- **The compliance log shows no verdict per row.** `/submissions` carries the
  job state but not the recorded violation count, and a verdict without that
  count would be a guess. Open a row to get the real figures.
- **No token revocation.** Signing out clears this browser only.
- **Media tokens travel in URLs.** They are short-lived and never logged
  here, but a URL can still reach browser history, `Referer`, and any
  proxy/CDN log, which this application cannot redact.
- **Bounding-box overlays assume the `[ymin, xmin, ymax, xmax]` 0–1000 form**
  from `backend/prompts.py`, and are positioned as percentages of the player
  box — so they are only correct when the video fills it.
- **Nothing here has been exercised against live AWS or Bedrock.** No claim is
  made about detection accuracy.

See `../CLAUDE.md` §5 for the backend's remaining gaps and `../README.md` for
the system as a whole.
