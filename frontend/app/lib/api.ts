/**
 * Typed client for the NovaFlow backend.
 *
 * Every shape below was read off `backend/app.py` and `backend/auth.py`
 * rather than assumed. The route surface this file covers:
 *
 *   POST /auth/signup|login   -> { access_token, token_type, role }
 *   GET  /auth/me             -> { username, role }
 *   POST /upload-reel         -> { video_id, video_url, filename, status, ... }
 *   POST /analyze/{video_id}  -> { video_id, status, job }   404 if not found
 *   POST /analyze/{id}/retry  -> { video_id, status, job }
 *   GET  /jobs/{video_id}     -> the job view: state, stage, counts, attempt
 *   GET  /violations/{id}     -> { detections[], counts, next_cursor, job }
 *   GET  /submissions         -> { submissions[] }  the caller's own uploads
 *   GET  /health/worker       -> queue counts, inferred worker health
 *   GET  /remediations/capabilities      -> what this deployment can do
 *   GET|POST /remediations                -> list for a video | propose
 *   GET  /remediations/{id}               -> one record
 *   POST /remediations/{id}/approve|reject|cancel|execute
 *   POST /remediations/{id}/asset         -> attach a replacement image
 *   GET  /remediations/{id}/status        -> state + the output's analysis
 *   GET  /remediations/{id}/output        -> a scoped playback URL
 *
 * `GET /jobs/{id}` is authoritative for progress: treat an empty `detections`
 * list as "no violations found" ONLY when `state === "completed"`, because
 * that state means analysis finished AND its results were persisted. Any
 * other state means the picture is incomplete.
 *
 * How the backend executes the job is deliberately invisible here. It queues
 * the job durably before responding, and either a separate worker process or
 * an in-process task runs it depending on server configuration; the response
 * shape is identical either way, so nothing in this file depends on it.
 *
 * Logging policy: this module never logs a request URL. A playback URL
 * carries a media token in its query string, so a logged URL would leak a
 * credential into the browser console. Errors carry a status and the
 * backend's own `detail`, never the target.
 */

export function apiBaseUrl(): string {
  return process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
}

/** Storage key for the session token; owned by AuthContext. */
export const TOKEN_STORAGE_KEY = "novaflow_token";
/** Storage key for the cached user record; owned by AuthContext. */
export const USER_STORAGE_KEY = "novaflow_user";

/**
 * Read the stored session token.
 *
 * Returns null during server-side rendering and when the user is signed out.
 * The token is a bearer credential, so it is only ever attached to requests
 * aimed at this application's own API.
 */
export function getStoredToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    // Storage can throw in private browsing modes.
    return null;
  }
}

/**
 * Authorization header for the current session, or an empty object.
 *
 * Phase 3 made every data route require authentication, so each call below
 * attaches this.
 */
function authHeaders(): Record<string, string> {
  const token = getStoredToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** An error carrying the backend's HTTP status and `detail` message. */
export class ApiError extends Error {
  readonly status: number;
  /** Seconds to wait, parsed from a `Retry-After` header. Null when absent. */
  readonly retryAfterSeconds: number | null;

  constructor(message: string, status: number, retryAfterSeconds: number | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryAfterSeconds = retryAfterSeconds;
  }

  /** True when the session is missing, expired, or rejected. */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  /** True when the caller is being throttled rather than refused. */
  get isThrottled(): boolean {
    return this.status === 429;
  }

  /**
   * True when the resource is absent or not the caller's.
   *
   * The backend deliberately returns 404 rather than 403 for another user's
   * video, so ids cannot be enumerated; both land here.
   */
  get isNotFound(): boolean {
    return this.status === 404 || this.status === 403;
  }
}

/** Read `Retry-After` as whole seconds, or null when it is absent/invalid. */
function readRetryAfter(res: Response): number | null {
  const raw = Number(res.headers.get("Retry-After"));
  return Number.isFinite(raw) && raw > 0 ? raw : null;
}

async function readError(res: Response, fallback: string): Promise<ApiError> {
  let detail = fallback;
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") {
      detail = body.detail;
    }
  } catch {
    // Non-JSON error body; keep the fallback message.
  }
  return new ApiError(detail, res.status, readRetryAfter(res));
}

/** Options every request helper accepts. */
export interface RequestOptions {
  /** Abort signal, so a view can cancel in-flight work on unmount. */
  signal?: AbortSignal;
}

/**
 * Single place every JSON request goes through.
 *
 * Centralising this is what keeps error handling consistent: one status
 * check, one defensive `detail` parse, one place that attaches the bearer
 * token, and one place that must never log the URL.
 */
async function requestJson<T>(
  path: string,
  init: RequestInit,
  fallbackMessage: string,
  options: RequestOptions = {}
): Promise<T> {
  const res = await fetch(`${apiBaseUrl()}${path}`, {
    ...init,
    headers: { ...authHeaders(), ...(init.headers as Record<string, string>) },
    signal: options.signal,
  });

  if (!res.ok) {
    throw await readError(res, fallbackMessage);
  }

  try {
    return (await res.json()) as T;
  } catch {
    throw new ApiError("The server returned a malformed response", res.status);
  }
}

/** True when a rejection is an abort, which is a normal cancellation. */
export function isAbortError(err: unknown): boolean {
  return (
    (err instanceof DOMException && err.name === "AbortError") ||
    (err instanceof Error && err.name === "AbortError")
  );
}

/**
 * A human-readable message for any rejection, with no internals.
 *
 * Views call this instead of rendering `String(err)`, so a thrown object can
 * never reach the DOM as "[object Object]" and a stack trace is never shown.
 */
export function describeError(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.isUnauthorized) return "Your session has expired. Please sign in again.";
    return err.message || fallback;
  }
  if (err instanceof Error && err.message) {
    // A fetch failure in a browser is reported as "Failed to fetch", which is
    // accurate but unhelpful on its own.
    if (err.name === "TypeError") {
      return "Could not reach the server. Check your connection and try again.";
    }
    return err.message;
  }
  return fallback;
}

// ---------------------------------------------------------------------------
// Authentication
// ---------------------------------------------------------------------------

/** The two roles the backend recognises; see backend/config.py VALID_ROLES. */
export type UserRole = "client" | "agent";

export interface AuthenticatedUser {
  username: string;
  role: UserRole;
}

export interface TokenResponse {
  access_token: string;
  token_type?: string;
  role: string;
}

/**
 * Narrow an untrusted role value.
 *
 * The role arrives from the API and from localStorage, and drives which
 * navigation entries are rendered. An unrecognised value is treated as the
 * least-privileged role rather than passed through: presentation must never
 * widen on the strength of an unknown string. This is presentation only -
 * the backend remains the authority on what the role may actually do.
 */
export function coerceRole(value: unknown): UserRole {
  return value === "agent" ? "agent" : "client";
}

/** Sign in. Never sends a role: the backend reads it from stored account data. */
export async function login(
  username: string,
  password: string,
  options: RequestOptions = {}
): Promise<TokenResponse> {
  return requestJson<TokenResponse>(
    "/auth/login",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    },
    "Authentication failed",
    options
  );
}

/**
 * Register. Never sends a role.
 *
 * Public signup always creates a least-privileged account server-side; a
 * `role` field in the body is ignored, so sending one would only imply the
 * client could choose it.
 */
export async function signup(
  username: string,
  password: string,
  options: RequestOptions = {}
): Promise<TokenResponse> {
  return requestJson<TokenResponse>(
    "/auth/signup",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    },
    "Registration failed",
    options
  );
}

/**
 * Verify the stored session against the server.
 *
 * The stored token may be expired, or signed with a key this backend no
 * longer accepts, and the cached role may be stale. This is the only
 * trustworthy source for both.
 */
export async function fetchCurrentUser(
  options: RequestOptions = {}
): Promise<AuthenticatedUser> {
  const data = await requestJson<{ username?: unknown; role?: unknown }>(
    "/auth/me",
    { method: "GET" },
    "Could not verify your session",
    options
  );
  return {
    username: typeof data.username === "string" ? data.username : "",
    role: coerceRole(data.role),
  };
}

// ---------------------------------------------------------------------------
// Upload
// ---------------------------------------------------------------------------

export interface UploadReelResponse {
  video_id: string;
  video_url: string;
  filename: string;
  original_filename?: string | null;
  size_bytes?: number;
  status: string;
  message?: string;
}

/**
 * Upload limits enforced client-side, before a byte leaves the browser.
 *
 * IMPORTANT: these mirror the backend defaults (`MAX_UPLOAD_BYTES`,
 * `ALLOWED_VIDEO_EXTENSIONS` in backend/config.py). The browser cannot read
 * the server's configuration, so this is a pre-flight courtesy, NOT the
 * authority: the backend validates every upload independently and its
 * rejection message is surfaced verbatim when the two disagree. A deployment
 * that narrows the server limits should set the matching NEXT_PUBLIC_* values
 * so the hint shown to the user stays true.
 */
export const DEFAULT_MAX_UPLOAD_BYTES = 104857600; // 100 MiB
export const DEFAULT_ALLOWED_VIDEO_EXTENSIONS = ["mp4", "mov", "avi", "webm", "mkv"];

export function maxUploadBytes(): number {
  const configured = Number(process.env.NEXT_PUBLIC_MAX_UPLOAD_BYTES);
  return Number.isFinite(configured) && configured > 0
    ? configured
    : DEFAULT_MAX_UPLOAD_BYTES;
}

export function allowedVideoExtensions(): string[] {
  const configured = process.env.NEXT_PUBLIC_ALLOWED_VIDEO_EXTENSIONS;
  if (!configured) return DEFAULT_ALLOWED_VIDEO_EXTENSIONS;
  const parsed = configured
    .split(",")
    .map((part) => part.trim().toLowerCase().replace(/^\./, ""))
    .filter((part) => /^[a-z0-9]+$/.test(part));
  return parsed.length > 0 ? parsed : DEFAULT_ALLOWED_VIDEO_EXTENSIONS;
}

/** Render a byte count for display. */
export function formatBytes(bytes: number | null | undefined): string {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes <= 0) return "--";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

/**
 * Validate a chosen file against the limits above.
 *
 * Returns null when the file passes, or a message to show inline. Mirrors the
 * backend's own checks (extension allowlist, size cap, non-empty body) so the
 * common mistakes are caught without a 100 MiB round trip. Neither check
 * proves the bytes are a safe video; the backend does not prove that either.
 */
export function validateVideoFile(file: File): string | null {
  const extensions = allowedVideoExtensions();
  const suffix = file.name.replace(/\\/g, "/").split("/").pop() ?? "";
  const dot = suffix.lastIndexOf(".");
  const extension = dot > 0 ? suffix.slice(dot + 1).toLowerCase() : "";

  if (!extension) {
    return `File must have an extension. Supported: ${extensions.join(", ")}.`;
  }
  if (!extensions.includes(extension)) {
    return `Unsupported file type ".${extension}". Supported: ${extensions.join(", ")}.`;
  }
  if (file.size === 0) {
    return "That file is empty.";
  }
  const limit = maxUploadBytes();
  if (file.size > limit) {
    return `That file is ${formatBytes(file.size)}; the limit is ${formatBytes(limit)}.`;
  }
  return null;
}

/**
 * Upload a video file. This only stores the file; it does NOT start analysis.
 * Call startAnalysis afterwards.
 *
 * Deliberately NOT retried automatically: a repeated POST would store a
 * second copy of the same video and bill a second analysis. A retry is the
 * user's explicit action.
 */
export async function uploadReel(
  file: File,
  options: RequestOptions = {}
): Promise<UploadReelResponse> {
  const form = new FormData();
  form.append("file", file);

  // Note: no Content-Type header. The browser must set the multipart
  // boundary itself.
  const res = await fetch(`${apiBaseUrl()}/upload-reel`, {
    method: "POST",
    headers: authHeaders(),
    body: form,
    signal: options.signal,
  });

  if (!res.ok) {
    throw await readError(res, `Upload failed (HTTP ${res.status})`);
  }

  const data = (await res.json()) as UploadReelResponse;
  if (!data?.video_id) {
    throw new ApiError("Upload response did not include a video_id", res.status);
  }
  return data;
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

/**
 * Analysis job state, as reported by GET /jobs/{video_id}.
 *
 * `state` is the backend's validated job state. The server only sets
 * "completed" once analysis finished AND its results were persisted, so this
 * is the one field a client may use to decide an audit is done.
 */
export type JobState =
  | "uploaded"
  | "queued"
  | "extracting"
  | "analyzing"
  | "completed"
  | "failed";

export interface Job {
  video_id: string;
  state: JobState;
  /** Human-readable stage name. Deliberately not a percentage. */
  stage: string;
  is_terminal: boolean;
  is_active: boolean;
  attempt: number;
  max_attempts: number;
  /** Times a crashed run was automatically requeued; see video_repo.job_view. */
  recovered_count?: number;
  frames_extracted: number | null;
  frames_audited: number | null;
  frames_failed: number | null;
  detections_recorded: number | null;
  /** Safe summary written by the backend; never an exception string. */
  error_summary: string | null;
  model_id: string | null;
  pipeline_version: string | null;
  original_filename: string | null;
  size_bytes: number | null;
  created_at: string | null;
  updated_at: string | null;
  completed_at: string | null;
}

/** Fetch the authoritative job state for a video. */
export async function fetchJob(
  videoId: string,
  options: RequestOptions = {}
): Promise<Job> {
  return requestJson<Job>(
    `/jobs/${encodeURIComponent(videoId)}`,
    { method: "GET" },
    `Could not fetch job status (HTTP error)`,
    options
  );
}

export interface StartAnalysisResponse {
  video_id: string;
  /** Mirrors `job.state`; retained for backwards compatibility. */
  status: string;
  job?: Job;
  message?: string;
}

/**
 * Trigger the real analysis pipeline (frame extraction -> Bedrock -> DynamoDB).
 *
 * Returns once the backend has *accepted* the job. The work runs in the
 * background, so a successful return means "queued", not "complete".
 *
 * Never retried automatically: each run bills the model per frame.
 */
export async function startAnalysis(
  videoId: string,
  options: RequestOptions = {}
): Promise<StartAnalysisResponse> {
  return requestJson<StartAnalysisResponse>(
    `/analyze/${encodeURIComponent(videoId)}`,
    { method: "POST" },
    "Could not start analysis",
    options
  );
}

/** Re-run analysis for a job that has reached a terminal state. */
export async function retryAnalysis(
  videoId: string,
  options: RequestOptions = {}
): Promise<Job> {
  const data = await requestJson<StartAnalysisResponse>(
    `/analyze/${encodeURIComponent(videoId)}/retry`,
    { method: "POST" },
    "Could not retry analysis",
    options
  );
  return data.job as Job;
}

// ---------------------------------------------------------------------------
// Violations
// ---------------------------------------------------------------------------

/** A single violation as stored by analyzer.py and flattened by /violations. */
export interface Detection {
  category?: string;
  description?: string;
  confidence?: number;
  /** Normalized 0-1000 as [ymin, xmin, ymax, xmax]; see backend/prompts.py. */
  bounding_box?: number[];
  /** "High" | "Medium" | "Low" per backend/prompts.py, but model-supplied. */
  risk_level?: string;
  /** Added per-detection by /violations from the frame's timestamp, seconds. */
  timestamp?: number;
  /** The frame this detection came from; added by /violations. */
  frame_number?: number;
  /**
   * This detection's position within its frame's stored list; added by
   * /violations in Phase 8.
   *
   * Together with the video id and `frame_number` it is how the remediation
   * API addresses a finding. It cannot be derived here: /violations flattens
   * detections across frames, so list position in this array is not the index
   * within a frame.
   */
  detection_index?: number;
}

/** A detection whose bounding box is present and well formed. */
export type RenderableDetection = Detection & {
  bounding_box: [number, number, number, number];
};

export interface ViolationsResponse {
  video_id: string;
  detections: Detection[];
  /** Frames in THIS page. `job.frames_extracted` is the total. */
  frame_count: number;
  audited_frames: number;
  failed_frames: number;
  next_cursor: string | null;
  has_more: boolean;
  /**
   * Authoritative job state. Never infer completion from an empty list.
   *
   * Optional because a malformed or legacy response may omit it; a caller
   * must treat a missing job as "state unknown", never as completed.
   */
  job?: Job;
}

/** Fetch whatever detections have been persisted for a video so far. */
export async function fetchViolations(
  videoId: string,
  options: RequestOptions & { cursor?: string | null; limit?: number } = {}
): Promise<ViolationsResponse> {
  const params = new URLSearchParams();
  if (options.cursor) params.set("cursor", options.cursor);
  if (options.limit) params.set("limit", String(options.limit));
  const query = params.toString();

  const data = await requestJson<Partial<ViolationsResponse>>(
    `/violations/${encodeURIComponent(videoId)}${query ? `?${query}` : ""}`,
    { method: "GET" },
    "Could not fetch violations",
    options
  );

  return {
    video_id: data?.video_id ?? videoId,
    detections: Array.isArray(data?.detections) ? data.detections : [],
    frame_count: typeof data?.frame_count === "number" ? data.frame_count : 0,
    audited_frames: typeof data?.audited_frames === "number" ? data.audited_frames : 0,
    failed_frames: typeof data?.failed_frames === "number" ? data.failed_frames : 0,
    next_cursor: data?.next_cursor ?? null,
    has_more: Boolean(data?.has_more),
    job: data?.job,
  };
}

/**
 * Fetch every page of detections for a video.
 *
 * A long clip holds more frames than one DynamoDB query returns. `maxPages`
 * bounds the walk so a pathological response cannot loop forever.
 */
export async function fetchAllViolations(
  videoId: string,
  options: RequestOptions & { maxPages?: number } = {}
): Promise<ViolationsResponse> {
  const maxPages = options.maxPages ?? 20;
  let cursor: string | null = null;
  let pages = 0;
  const detections: Detection[] = [];
  let frameCount = 0;
  let audited = 0;
  let failed = 0;
  let job: Job | undefined;

  do {
    const page: ViolationsResponse = await fetchViolations(videoId, {
      cursor,
      signal: options.signal,
    });
    detections.push(...page.detections);
    frameCount += page.frame_count;
    audited += page.audited_frames;
    failed += page.failed_frames;
    job = page.job;
    cursor = page.next_cursor;
    pages += 1;
  } while (cursor && pages < maxPages);

  return {
    video_id: videoId,
    detections,
    frame_count: frameCount,
    audited_frames: audited,
    failed_frames: failed,
    next_cursor: cursor,
    has_more: Boolean(cursor),
    job,
  };
}

/**
 * True when a detection carries the fields the overlay needs to render.
 *
 * A type predicate, so a caller that filters by it no longer needs a
 * non-null assertion to read the box. Model output is not schema-validated
 * server-side, so a malformed detection must be skipped rather than rendered
 * as NaN.
 */
export function isRenderableDetection(
  d: Detection | null | undefined
): d is RenderableDetection {
  return Boolean(
    d &&
      Array.isArray(d.bounding_box) &&
      d.bounding_box.length === 4 &&
      d.bounding_box.every((n) => typeof n === "number" && Number.isFinite(n))
  );
}

// ---------------------------------------------------------------------------
// Submissions
// ---------------------------------------------------------------------------

export interface Submission {
  id: string;
  filename?: string;
  original_filename?: string | null;
  title?: string;
  /**
   * The video's stored job state, lower-case: see backend/job_state.py.
   * Legacy records can hold "UPLOADED" in upper case, so compare
   * case-insensitively - use `jobStateOf` in ./jobs.
   */
  status?: string;
  created_at?: string | null;
  size_bytes?: number;
  /** Playback URL carrying a short-lived media token. Never log it. */
  url?: string;
}

/** List the signed-in user's own uploads. Never another user's. */
export async function fetchSubmissions(
  options: RequestOptions = {}
): Promise<Submission[]> {
  const data = await requestJson<{ submissions?: unknown }>(
    "/submissions",
    { method: "GET" },
    "Could not fetch submissions",
    options
  );
  return Array.isArray(data?.submissions) ? (data.submissions as Submission[]) : [];
}

// ---------------------------------------------------------------------------
// Worker health
// ---------------------------------------------------------------------------

/**
 * What GET /health/worker reports.
 *
 * Read the backend docstring before presenting any of this: the endpoint does
 * NOT contact the worker process. It infers whether work is being consumed
 * from the job queue, and says so in `note`. Counts only - never ids or
 * owners.
 */
export interface WorkerHealth {
  status: "ok" | "degraded" | "unknown" | string;
  execution_mode?: string;
  counts?: Record<string, number>;
  active?: number;
  abandoned?: number;
  note?: string;
  detail?: string;
}

/**
 * Read inferred worker health.
 *
 * Returns the body on 503 as well as 200: the backend answers "degraded" with
 * a 503 so it can drive an alert, and the body is the useful part. Only a
 * transport failure or an unreadable body throws.
 */
export async function fetchWorkerHealth(
  options: RequestOptions = {}
): Promise<WorkerHealth> {
  const res = await fetch(`${apiBaseUrl()}/health/worker`, {
    method: "GET",
    headers: authHeaders(),
    signal: options.signal,
  });

  // 503 is an expected, informative answer here, not a failure to report.
  if (!res.ok && res.status !== 503) {
    throw await readError(res, "Could not read worker health");
  }

  try {
    return (await res.json()) as WorkerHealth;
  } catch {
    throw new ApiError("The server returned a malformed response", res.status);
  }
}

// ---------------------------------------------------------------------------
// Remediation (Phase 8)
// ---------------------------------------------------------------------------

/**
 * Remediation lifecycle, mirroring `backend/remediation_state.py`.
 *
 * These are seven different statements about the world and none may be
 * inferred from another. In particular `approved` does not mean an image was
 * generated, `asset_ready` does not mean the video changed, `applied` does not
 * mean the result is compliant, and ONLY `verified` may be described as
 * compliant - and it is reachable only after a completed re-analysis.
 *
 * Phase 9 made all of these reachable EXCEPT `processing`, which means "a
 * provider call is in flight" - and no image provider is implemented. The rest
 * are reached through the supplied-asset path: the owner uploads the image a
 * provider would have generated, the backend builds a new output video, and
 * that output is analysed in its own right.
 */
export type RemediationState =
  | "proposed"
  | "approved"
  | "processing"
  | "asset_ready"
  | "reconstructing"
  | "applied"
  | "verifying"
  | "verified"
  | "rejected"
  | "cancelled"
  | "superseded"
  | "failed"
  | "verification_failed";

/** How the finding's confidence compared with the configured threshold. */
export type ConfidenceGate = "pass" | "below_threshold" | "not_reported";

/** The evidence snapshot the proposal was built from. */
export interface RemediationFinding {
  frame_number: number;
  detection_index: number;
  category: string;
  description?: string;
  risk_level?: string | null;
  confidence?: number | null;
  bounding_box?: number[] | null;
  timestamp_seconds?: number | null;
}

/** What is proposed, and on what basis. All of it server-derived. */
export interface RemediationProposal {
  action: string;
  summary: string;
  rationale: string;
  /** The model's own words, quoted and attributed. Never evidence. */
  model_claim: string;
  target_description: string;
  limitations: string;
  requires_approval: boolean;
  confidence_gate: ConfidenceGate | string;
  gate_explanation: string;
}

export interface Remediation {
  remediation_id: string;
  video_id: string;
  frame_number: number;
  detection_index: number;
  state: RemediationState;
  /** Human-readable label written by the backend. Not a percentage. */
  stage: string;
  is_terminal: boolean;
  is_active: boolean;
  /**
   * The single authoritative answer to "may this be called compliant?".
   * Computed server-side so no client has to re-derive the rule.
   */
  is_compliance_claim: boolean;
  /** Whether an OUTPUT video exists. The upload is never overwritten. */
  source_video_modified: boolean;
  finding: RemediationFinding;
  /**
   * Optional on purpose (Phase 10.1). `remediation_repo.view` emits
   * `record.get("proposal")`, so a document stored without one serialises as
   * `null` - and the backend's own routes read it as
   * `(record.get("proposal") or {})`. Typing it as required made the
   * non-null dereference in RemediationPanel look safe when it was not, and
   * a null blanked the whole audit detail view. Guard before use.
   */
  proposal?: RemediationProposal | null;
  approved_by: string | null;
  approved_at: string | null;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  provider: string | null;
  provider_model_id: string | null;
  /** Presence only. The storage key is never sent to a client. */
  has_asset: boolean;
  asset_bytes: number | null;
  asset_content_type: string | null;
  /** "upload" for an owner-supplied file, or a provider name. */
  asset_source?: string | null;
  asset_supplied_by?: string | null;
  asset_supplied_at?: string | null;
  /**
   * The output video's id. Safe to hold: it is how playback is requested
   * through the authorized media route, which re-checks ownership. Its
   * storage key is never published.
   */
  output_video_id: string | null;
  /** Whether an output video actually exists. Not a compliance claim. */
  has_output?: boolean;
  output_bytes?: number | null;
  output_duration_seconds?: number | null;
  output_has_audio?: boolean | null;
  /** The audit trail of the reconstruction, written by the backend. */
  reconstruction?: RemediationReconstruction | null;
  analysis_job_video_id?: string | null;
  analysis_submitted_at?: string | null;
  /** Written only by the backend's verification service, from real results. */
  verification?: RemediationVerification | null;
  verification_video_id: string | null;
  verified_at: string | null;
  error_code: string | null;
  error_summary: string | null;
  attempt: number;
  max_attempts?: number;
  recovered_count?: number;
  created_at: string | null;
  updated_at: string | null;
}

/** How the output video was built. Everything here is server-measured. */
export interface RemediationReconstruction {
  method?: string;
  region_pixels?: { x: number; y: number; width: number; height: number };
  window_seconds?: number[];
  source_width?: number;
  source_height?: number;
  source_duration_seconds?: number;
  source_had_audio?: boolean;
  output_duration_seconds?: number;
  output_has_audio?: boolean;
  audio_preserved?: boolean;
  warnings?: string[];
}

/**
 * The verdict and its evidence.
 *
 * Three SEPARATE facts, and the UI must never merge them:
 *   `target_resolved`  the targeted finding is gone from the output
 *   `policy_passed`    the output satisfies the configured policy
 *   `other_findings`   what else the output's analysis reported
 *
 * `null` for the first two means "no verdict", which is not the same as
 * `false`. An `outcome` of "pending" or "inconclusive" is not a failure and
 * emphatically not a pass.
 */
export interface RemediationVerification {
  outcome?:
    | "verified"
    | "verification_failed"
    | "pending"
    | "inconclusive"
    | string;
  policy?: string;
  evaluated_at?: string;
  source_video_id?: string;
  output_video_id?: string;
  analysis_job_video_id?: string;
  analysis_state?: string;
  frames_extracted?: number | null;
  frames_audited?: number | null;
  frames_failed?: number | null;
  target_category?: string | null;
  target_timestamp_seconds?: number | null;
  timestamp_tolerance_seconds?: number;
  target_resolved?: boolean | null;
  policy_passed?: boolean | null;
  target_matches?: Detection[];
  other_findings?: Detection[];
  other_finding_count?: number;
  reason?: string;
}

/** What GET /remediations/{id}/status returns: the record plus live progress. */
export interface RemediationStatus extends Remediation {
  /** The OUTPUT video's own analysis job, or null when there is none yet. */
  output_analysis: Job | null;
}

/** What GET /remediations/{id}/output returns. */
export interface RemediationOutput {
  remediation_id: string;
  output_video_id: string;
  /**
   * Carries a short-lived media token in its query string. Never log it, and
   * never persist it: it is scoped to one video and expires.
   */
  playback_url: string;
  duration_seconds: number | null;
  has_audio: boolean | null;
  size_bytes: number | null;
}

/**
 * What GET /remediations/capabilities reports.
 *
 * Read before rendering any remediation control: it is how the UI knows
 * whether generation exists on this deployment rather than guessing.
 */
export interface RemediationCapabilities {
  remediation_enabled: boolean;
  proposals_supported: boolean;
  generation_available: boolean;
  generation_provider: string;
  generation_unavailable_reason: string | null;
  min_confidence: number;
  applies_assets_to_video: boolean;
  verifies_compliance: boolean;
  notes: string;
  /** Phase 9: whether the owner may supply the replacement image themselves. */
  asset_upload_supported?: boolean;
  max_asset_bytes?: number;
  accepted_asset_types?: string[];
  /** Whether an approved remediation can actually be built into a video. */
  reconstruction_available?: boolean;
  reconstruction_unavailable_reason?: string | null;
  /**
   * How the replacement is applied. "scaled_overlay" means the image is
   * scaled to the approved region and drawn over it with hard edges - it
   * conceals that region and does NOT inpaint, match grain or track motion.
   * Never render this as "seamless".
   */
  reconstruction_method?: string | null;
  verification_policy?: string | null;
}

/** Read this deployment's remediation capabilities. */
export async function fetchRemediationCapabilities(
  options: RequestOptions = {}
): Promise<RemediationCapabilities> {
  return requestJson<RemediationCapabilities>(
    "/remediations/capabilities",
    { method: "GET" },
    "Could not read remediation capabilities",
    options
  );
}

/** List the caller's remediation records for one video. */
export async function fetchRemediations(
  videoId: string,
  options: RequestOptions = {}
): Promise<Remediation[]> {
  const data = await requestJson<{ remediations?: unknown }>(
    `/remediations?video_id=${encodeURIComponent(videoId)}`,
    { method: "GET" },
    "Could not load remediations",
    options
  );
  return Array.isArray(data?.remediations)
    ? (data.remediations as Remediation[])
    : [];
}

/**
 * Propose a remediation for one finding.
 *
 * Addresses the finding positionally, which is how the backend stores it:
 * (video_id, frame_number, detection_index). Creates nothing but a proposal -
 * no provider is called and no video is touched.
 *
 * Not retried automatically. The backend makes a repeat for the same finding
 * idempotent (it returns the open record with 200), but a retry is still the
 * user's action.
 */
export async function createRemediation(
  videoId: string,
  frameNumber: number,
  detectionIndex: number,
  options: RequestOptions = {}
): Promise<Remediation> {
  return requestJson<Remediation>(
    "/remediations",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        video_id: videoId,
        frame_number: frameNumber,
        detection_index: detectionIndex,
      }),
    },
    "Could not create a remediation proposal",
    options
  );
}

/** The owner decisions the backend accepts on a record. */
export type RemediationDecision = "approve" | "reject" | "cancel";

/**
 * Record an owner decision.
 *
 * The approving identity is taken from the session token server-side; nothing
 * here sends a user, an owner or a role, because the backend would ignore it
 * and sending one would imply the client could choose.
 */
export async function decideRemediation(
  remediationId: string,
  decision: RemediationDecision,
  note?: string,
  options: RequestOptions = {}
): Promise<Remediation> {
  return requestJson<Remediation>(
    `/remediations/${encodeURIComponent(remediationId)}/${decision}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note: note ?? null }),
    },
    `Could not ${decision} this remediation`,
    options
  );
}

/**
 * Attempt an approved remediation.
 *
 * Expect this to fail with 503 on every current deployment: no image provider
 * is implemented. The backend leaves the record `approved` when it refuses,
 * so this is safe to call and safe to retry later.
 */
export async function executeRemediation(
  remediationId: string,
  options: RequestOptions = {}
): Promise<Remediation> {
  return requestJson<Remediation>(
    `/remediations/${encodeURIComponent(remediationId)}/execute`,
    { method: "POST" },
    "Could not start this remediation",
    options
  );
}

/**
 * Attach an owner-supplied replacement image to an approved remediation.
 *
 * This is the path that makes an end-to-end correction possible with no image
 * provider. It is not a way around review: the backend requires the
 * remediation to be `approved`, re-checks the finding, and validates the
 * bytes by sniffing them - the filename and the declared type are ignored.
 *
 * Never retried automatically: a repeat would be refused anyway (an asset is
 * already attached), and a retry is the user's action.
 */
export async function uploadRemediationAsset(
  remediationId: string,
  file: File,
  options: RequestOptions = {}
): Promise<Remediation> {
  const form = new FormData();
  form.append("file", file);

  // No Content-Type header: the browser must set the multipart boundary.
  const res = await fetch(
    `${apiBaseUrl()}/remediations/${encodeURIComponent(remediationId)}/asset`,
    {
      method: "POST",
      headers: authHeaders(),
      body: form,
      signal: options.signal,
    }
  );

  if (!res.ok) {
    throw await readError(res, "The replacement image could not be attached");
  }
  return (await res.json()) as Remediation;
}

/**
 * Validate a chosen replacement image before uploading it.
 *
 * Mirrors the backend's own limits so the common mistakes are caught without
 * a round trip. A pre-flight courtesy only: the backend sniffs the bytes and
 * remains the authority, and its rejection message is shown verbatim when the
 * two disagree.
 */
export function validateReplacementImage(
  file: File,
  capabilities?: RemediationCapabilities | null
): string | null {
  const accepted = capabilities?.accepted_asset_types ?? [
    "image/png",
    "image/jpeg",
  ];
  const limit = capabilities?.max_asset_bytes ?? 8 * 1024 * 1024;

  if (file.size === 0) return "That file is empty.";
  if (file.size > limit) {
    return `That image is ${formatBytes(file.size)}; the limit is ${formatBytes(
      limit
    )}.`;
  }

  // The extension is a hint only - the server decides from the bytes - but a
  // clearly wrong one is worth catching here.
  const suffix = file.name.replace(/\\/g, "/").split("/").pop() ?? "";
  const dot = suffix.lastIndexOf(".");
  const extension = dot > 0 ? suffix.slice(dot + 1).toLowerCase() : "";
  if (!extension || extension === "jpeg") return null;

  const extensionFor: Record<string, string> = {
    "image/png": "png",
    "image/jpeg": "jpg",
  };
  const allowed = accepted
    .map((type) => extensionFor[type])
    .filter((value): value is string => Boolean(value));
  if (allowed.length > 0 && !allowed.includes(extension)) {
    return `Replacement images must be ${allowed.join(" or ")}.`;
  }
  return null;
}

/**
 * Ask the backend to build the corrected video.
 *
 * Returns once the request is DURABLY RECORDED, not once the video exists -
 * the work is a claimed, leased job. Poll `fetchRemediationStatus`.
 *
 * Safe to call again: the backend makes a repeat idempotent and returns the
 * record's current state rather than starting a second reconstruction.
 */
export async function startRemediationExecution(
  remediationId: string,
  options: RequestOptions = {}
): Promise<Remediation> {
  return requestJson<Remediation>(
    `/remediations/${encodeURIComponent(remediationId)}/execute`,
    { method: "POST" },
    "The correction could not be started",
    options
  );
}

/**
 * Read execution and verification status, with the output's real progress.
 *
 * Calling this also lets the backend record a verdict once the output's
 * analysis has finished. That evaluation is entirely server-side - this
 * request carries no body and cannot influence the outcome.
 */
export async function fetchRemediationStatus(
  remediationId: string,
  options: RequestOptions = {}
): Promise<RemediationStatus> {
  return requestJson<RemediationStatus>(
    `/remediations/${encodeURIComponent(remediationId)}/status`,
    { method: "GET" },
    "Could not read the correction's status",
    options
  );
}

/**
 * Get a short-lived, scoped playback URL for the corrected video.
 *
 * 404 until an output actually exists, which is deliberate: there must be no
 * placeholder URL for a download action to point at.
 */
export async function fetchRemediationOutput(
  remediationId: string,
  options: RequestOptions = {}
): Promise<RemediationOutput> {
  return requestJson<RemediationOutput>(
    `/remediations/${encodeURIComponent(remediationId)}/output`,
    { method: "GET" },
    "Could not load the corrected video",
    options
  );
}

/**
 * Key a remediation by the finding it addresses.
 *
 * A detection carries `frame_number` from `/violations`; the index is its
 * position within that frame's detections, which is how the backend names it.
 */
export function findingKey(frameNumber: number, detectionIndex: number): string {
  return `${frameNumber}:${detectionIndex}`;
}
