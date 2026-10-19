/**
 * Typed client for the NovaFlow backend.
 *
 * Phase 2, defect C: the Launch Audit action uploaded a video but never called
 * the analysis endpoint, so the pipeline was never triggered and the dashboard
 * polled for violations that could not exist.
 *
 * The request/response shapes below were read off backend/app.py rather than
 * assumed:
 *
 *   POST /upload-reel        -> { video_id, video_url, filename, status, message }
 *   POST /analyze/{video_id} -> { video_id, status, message }   404 if not found
 *   GET  /violations/{id}    -> { video_id, detections[], frame_count }
 *
 * KNOWN CONTRACT GAP: the backend exposes no job-status or completion endpoint.
 * `/analyze` returns as soon as the work is queued as a FastAPI BackgroundTask,
 * and `/violations` reports whatever has been written so far with no "finished"
 * flag. The UI therefore cannot truthfully claim an audit has completed; it
 * reports progress only. Adding durable job records is a later-phase task.
 */

export function apiBaseUrl(): string {
  return process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
}

/** Storage key for the session token; owned by AuthContext. */
export const TOKEN_STORAGE_KEY = "novaflow_token";

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

export interface UploadReelResponse {
  video_id: string;
  video_url: string;
  filename: string;
  status: string;
  message?: string;
}

export interface StartAnalysisResponse {
  video_id: string;
  /** Mirrors `job.state`; retained for backwards compatibility. */
  status: string;
  job?: Job;
  message?: string;
}

/** A single violation as stored by analyzer.py and flattened by /violations. */
export interface Detection {
  category?: string;
  description?: string;
  confidence?: number;
  /** Normalized 0-1000 as [ymin, xmin, ymax, xmax]; see backend/prompts.py. */
  bounding_box?: number[];
  risk_level?: string;
  /** Added per-detection by /violations from the frame's timestamp, seconds. */
  timestamp?: number;
}

export interface ViolationsResponse {
  video_id: string;
  detections: Detection[];
  /** Frames in THIS page. `job.frames_extracted` is the total. */
  frame_count: number;
  audited_frames: number;
  failed_frames: number;
  next_cursor: string | null;
  has_more: boolean;
  /** Authoritative job state. Never infer completion from an empty list. */
  job: Job;
}

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
export async function fetchJob(videoId: string): Promise<Job> {
  const res = await fetch(
    `${apiBaseUrl()}/jobs/${encodeURIComponent(videoId)}`,
    { headers: authHeaders() }
  );

  if (!res.ok) {
    throw await readError(res, `Could not fetch job status (HTTP ${res.status})`);
  }
  return (await res.json()) as Job;
}

/** Re-run analysis for a job that has reached a terminal state. */
export async function retryAnalysis(videoId: string): Promise<Job> {
  const res = await fetch(
    `${apiBaseUrl()}/analyze/${encodeURIComponent(videoId)}/retry`,
    { method: "POST", headers: authHeaders() }
  );

  if (!res.ok) {
    throw await readError(res, `Could not retry analysis (HTTP ${res.status})`);
  }
  const data = await res.json();
  return data.job as Job;
}

/** An error carrying the backend's HTTP status and `detail` message. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }

  /** True when the session is missing, expired, or rejected. */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }
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
  return new ApiError(detail, res.status);
}

/**
 * Upload a video file. This only stores the file; it does NOT start analysis.
 * Call startAnalysis afterwards.
 */
export async function uploadReel(file: File): Promise<UploadReelResponse> {
  const form = new FormData();
  form.append("file", file);

  const res = await fetch(`${apiBaseUrl()}/upload-reel`, {
    method: "POST",
    headers: authHeaders(),
    body: form,
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

/**
 * Trigger the real analysis pipeline (frame extraction -> Bedrock -> DynamoDB).
 *
 * Returns once the backend has *accepted* the job. The work runs in the
 * background, so a successful return means "queued", not "complete".
 */
export async function startAnalysis(videoId: string): Promise<StartAnalysisResponse> {
  const res = await fetch(`${apiBaseUrl()}/analyze/${encodeURIComponent(videoId)}`, {
    method: "POST",
    headers: authHeaders(),
  });

  if (!res.ok) {
    throw await readError(res, `Could not start analysis (HTTP ${res.status})`);
  }
  return (await res.json()) as StartAnalysisResponse;
}

/** Fetch whatever detections have been persisted for a video so far. */
export async function fetchViolations(
  videoId: string,
  options: { cursor?: string | null; limit?: number } = {}
): Promise<ViolationsResponse> {
  const params = new URLSearchParams();
  if (options.cursor) params.set("cursor", options.cursor);
  if (options.limit) params.set("limit", String(options.limit));
  const query = params.toString();

  const res = await fetch(
    `${apiBaseUrl()}/violations/${encodeURIComponent(videoId)}${query ? `?${query}` : ""}`,
    { headers: authHeaders() }
  );

  if (!res.ok) {
    throw await readError(res, `Could not fetch violations (HTTP ${res.status})`);
  }

  const data = (await res.json()) as ViolationsResponse;
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
  maxPages = 20
): Promise<ViolationsResponse> {
  let cursor: string | null = null;
  let pages = 0;
  const detections: Detection[] = [];
  let frameCount = 0;
  let audited = 0;
  let failed = 0;
  let job: Job | undefined;

  do {
    const page: ViolationsResponse = await fetchViolations(videoId, { cursor });
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
    job: job as Job,
  };
}

export interface Submission {
  id: string;
  filename?: string;
  original_filename?: string | null;
  title?: string;
  status?: string;
  created_at?: string | null;
  size_bytes?: number;
  url?: string;
}

/** List the signed-in user's own uploads. Never another user's. */
export async function fetchSubmissions(): Promise<Submission[]> {
  const res = await fetch(`${apiBaseUrl()}/submissions`, {
    headers: authHeaders(),
  });

  if (!res.ok) {
    throw await readError(res, `Could not fetch submissions (HTTP ${res.status})`);
  }

  const data = await res.json();
  return Array.isArray(data?.submissions) ? data.submissions : [];
}

/**
 * True when a detection carries the fields the overlay needs to render.
 *
 * Model output is not schema-validated server-side yet, so a malformed
 * detection must be skipped rather than rendered as NaN.
 */
export function isRenderableDetection(d: Detection | null | undefined): boolean {
  return Boolean(
    d &&
      Array.isArray(d.bounding_box) &&
      d.bounding_box.length === 4 &&
      d.bounding_box.every((n) => typeof n === "number" && Number.isFinite(n))
  );
}
