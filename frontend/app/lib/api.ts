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

export interface UploadReelResponse {
  video_id: string;
  video_url: string;
  filename: string;
  status: string;
  message?: string;
}

export interface StartAnalysisResponse {
  video_id: string;
  status: string;
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
  frame_count: number;
}

/** An error carrying the backend's HTTP status and `detail` message. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
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
  });

  if (!res.ok) {
    throw await readError(res, `Could not start analysis (HTTP ${res.status})`);
  }
  return (await res.json()) as StartAnalysisResponse;
}

/** Fetch whatever detections have been persisted for a video so far. */
export async function fetchViolations(videoId: string): Promise<ViolationsResponse> {
  const res = await fetch(
    `${apiBaseUrl()}/violations/${encodeURIComponent(videoId)}`
  );

  if (!res.ok) {
    throw await readError(res, `Could not fetch violations (HTTP ${res.status})`);
  }

  const data = (await res.json()) as ViolationsResponse;
  return {
    video_id: data?.video_id ?? videoId,
    detections: Array.isArray(data?.detections) ? data.detections : [],
    frame_count: typeof data?.frame_count === "number" ? data.frame_count : 0,
  };
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
