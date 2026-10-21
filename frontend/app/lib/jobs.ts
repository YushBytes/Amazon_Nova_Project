/**
 * Shared presentation of a job's state.
 *
 * Phase 7. Four views now describe the same job (the dashboard, the asset
 * list, the asset detail, the compliance log). Before this module each one
 * invented its own wording, and two of them showed a fixed "Verified" /
 * "Audit Passed" badge that was not derived from any backend field at all.
 *
 * The rules encoded here are the ones that must not drift:
 *
 *  - Only `state === "completed"` means analysis finished AND its results
 *    were persisted. Nothing else may be described as a finished audit.
 *  - A completed job with no detections is "no violations found". A job in
 *    any other state with no detections is "unknown", NEVER compliant - the
 *    distinction is the whole point of the backend's state machine.
 *  - Status is never conveyed by colour alone; every badge carries a word.
 *
 * Labels mirror backend/job_state.py STAGE_LABELS, but this module owns the
 * shorter forms used in lists and badges.
 */

import type { Detection, Job, JobState } from "./api";

/** Every state the backend's machine can report. */
export const JOB_STATES: readonly JobState[] = [
  "uploaded",
  "queued",
  "extracting",
  "analyzing",
  "completed",
  "failed",
] as const;

/**
 * Interpret an untrusted status string as a JobState.
 *
 * Mirrors `job_state.coerce`: tolerant by design, because records written
 * before Phase 4 carry "UPLOADED" or "ANALYZING" in upper case and some carry
 * no status at all. Anything unrecognised reads as "uploaded" - the state that
 * claims the least - rather than being rendered raw.
 */
export function jobStateOf(value: unknown): JobState {
  if (typeof value !== "string") return "uploaded";
  const normalized = value.trim().toLowerCase() as JobState;
  return JOB_STATES.includes(normalized) ? normalized : "uploaded";
}

/** Short label for a badge or list row. */
export const JOB_STATE_LABELS: Record<JobState, string> = {
  uploaded: "Not analyzed",
  queued: "Queued",
  extracting: "Extracting",
  analyzing: "Analyzing",
  completed: "Analyzed",
  failed: "Failed",
};

/** Longer label, matching backend/job_state.py STAGE_LABELS. */
export const JOB_STAGE_LABELS: Record<JobState, string> = {
  uploaded: "Uploaded; analysis not started",
  queued: "Queued for analysis",
  extracting: "Extracting frames",
  analyzing: "Analyzing frames",
  completed: "Analysis complete",
  failed: "Analysis failed",
};

/** True for a state the backend treats as finished. */
export function isTerminalState(state: JobState): boolean {
  return state === "completed" || state === "failed";
}

/** True while a worker is, or should be, doing something. */
export function isActiveState(state: JobState): boolean {
  return state === "queued" || state === "extracting" || state === "analyzing";
}

/**
 * The tone a state should be rendered in.
 *
 * "neutral" is deliberately the default: an unfinished job is neither good
 * news nor bad news, and showing it as either would be a claim the backend
 * has not made.
 */
export type StateTone = "success" | "critical" | "active" | "neutral";

export function toneForState(state: JobState): StateTone {
  if (state === "completed") return "success";
  if (state === "failed") return "critical";
  if (isActiveState(state)) return "active";
  return "neutral";
}

/**
 * The compliance verdict for a job, which is NOT the same as its state.
 *
 *   "clean"    analysis completed and recorded no violations
 *   "flagged"  analysis completed and recorded at least one violation
 *   "failed"   analysis did not finish; nothing may be concluded
 *   "pending"  analysis has not finished yet; nothing may be concluded
 *   "unknown"  no job record was available
 *
 * `detectionCount` is only consulted for a completed job. For any other state
 * an empty list means "we do not know", so it must never produce "clean".
 */
export type Verdict = "clean" | "flagged" | "failed" | "pending" | "unknown";

export function verdictFor(
  job: Job | null | undefined,
  detectionCount: number | null = null
): Verdict {
  if (!job) return "unknown";
  const state = jobStateOf(job.state);
  if (state === "failed") return "failed";
  if (state !== "completed") return "pending";

  // Completed: prefer the server's own recorded total over whatever this
  // client happened to page in, so a partial fetch cannot read as clean.
  const recorded =
    typeof job.detections_recorded === "number"
      ? job.detections_recorded
      : detectionCount;
  if (recorded === null || recorded === undefined) return "unknown";
  return recorded > 0 ? "flagged" : "clean";
}

export const VERDICT_LABELS: Record<Verdict, string> = {
  clean: "No violations found",
  flagged: "Violations found",
  failed: "Analysis failed",
  pending: "Analysis incomplete",
  unknown: "Status unavailable",
};

export function toneForVerdict(verdict: Verdict): StateTone {
  if (verdict === "clean") return "success";
  if (verdict === "flagged" || verdict === "failed") return "critical";
  if (verdict === "pending") return "active";
  return "neutral";
}

// ---------------------------------------------------------------------------
// Detections
// ---------------------------------------------------------------------------

/** Severity ranking used for sorting; unknown severities sort last. */
const SEVERITY_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 };

/**
 * Normalise a model-supplied risk level for display.
 *
 * `risk_level` is whatever the model returned - backend/prompts.py asks for
 * "High|Medium|Low" but nothing enforces it, and the field is absent when the
 * model omitted it. An unrecognised value is shown as given rather than
 * silently mapped to a severity it may not mean.
 */
export function severityKey(detection: Detection): string | null {
  const raw = detection.risk_level;
  if (typeof raw !== "string" || raw.trim() === "") return null;
  return raw.trim().toLowerCase();
}

export function severityLabel(detection: Detection): string {
  const key = severityKey(detection);
  if (!key) return "Severity not reported";
  if (key in SEVERITY_ORDER) return `${key[0].toUpperCase()}${key.slice(1)} risk`;
  return detection.risk_level as string;
}

export function toneForSeverity(detection: Detection): StateTone {
  const key = severityKey(detection);
  if (key === "high") return "critical";
  if (key === "medium") return "active";
  if (key === "low") return "neutral";
  return "neutral";
}

/** Most severe first; detections with no severity keep their original order at the end. */
export function sortBySeverity(detections: Detection[]): Detection[] {
  return [...detections].sort((a, b) => {
    const ka = severityKey(a);
    const kb = severityKey(b);
    const ra = ka !== null && ka in SEVERITY_ORDER ? SEVERITY_ORDER[ka] : 99;
    const rb = kb !== null && kb in SEVERITY_ORDER ? SEVERITY_ORDER[kb] : 99;
    if (ra !== rb) return ra - rb;
    return (a.timestamp ?? 0) - (b.timestamp ?? 0);
  });
}

/**
 * Format a confidence as a percentage, or null when the model omitted it.
 *
 * A missing confidence must stay missing: a fabricated "0%" reads as
 * "the model was certain there is nothing here", which is a different claim.
 */
export function formatConfidence(detection: Detection): string | null {
  const value = detection.confidence;
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const fraction = value > 1 ? value / 100 : value;
  return `${Math.round(Math.max(0, Math.min(1, fraction)) * 100)}%`;
}

/** Format a timestamp in seconds as m:ss, or null when absent. */
export function formatTimestamp(seconds: number | null | undefined): string | null {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) {
    return null;
  }
  const whole = Math.floor(seconds);
  const mins = Math.floor(whole / 60);
  const secs = whole % 60;
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

/** Render an ISO timestamp as a short local date-time, or a dash when absent. */
export function formatDateTime(iso?: string | null): string {
  if (!iso) return "--";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "--";
  return parsed.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
