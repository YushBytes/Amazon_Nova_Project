/**
 * Shared presentation of a remediation record.
 *
 * Phase 8, and the same discipline as `./jobs.ts`: the wording that decides
 * what a user believes lives in one place, so two views cannot describe the
 * same record differently.
 *
 * The rules encoded here must not drift from `backend/remediation_state.py`:
 *
 *  - `proposed` is not approved. `approved` is not generated. `asset_ready`
 *    is not a changed video. `applied` is not compliant.
 *  - ONLY `verified` may be described as compliant, and the backend already
 *    answers that question in `is_compliance_claim` - this module never
 *    re-derives it from the state string, it defers to the server's field and
 *    treats a missing one as "no".
 *  - Status is never conveyed by colour alone; every label is a word.
 *
 * Phase 9 added the states between `asset_ready` and `verified`. The wording
 * for those is the most load-bearing in the file: `applied` means a new video
 * exists and NOTHING has checked it, so it reads "New video built, not yet
 * checked" rather than anything resembling "corrected".
 */

import type { Remediation, RemediationState } from "./api";
import type { StateTone } from "./jobs";

/** Every state the backend's machine can report. */
export const REMEDIATION_STATES: readonly RemediationState[] = [
  "proposed",
  "approved",
  "processing",
  "asset_ready",
  "reconstructing",
  "applied",
  "verifying",
  "verified",
  "rejected",
  "cancelled",
  "superseded",
  "failed",
  "verification_failed",
] as const;

/**
 * Short label for a badge or a row.
 *
 * Phrasing is deliberate. "Approved, not generated" is longer than "Approved"
 * because the thing a reader will otherwise assume is exactly the thing that
 * has not happened.
 */
export const REMEDIATION_LABELS: Record<RemediationState, string> = {
  proposed: "Awaiting your review",
  // Phase 9: an approved proposal needs a replacement image before anything
  // can be built, and the label says which step is outstanding.
  approved: "Approved, awaiting a replacement image",
  processing: "Generating",
  asset_ready: "Image ready, video unchanged",
  reconstructing: "Building a new video, original unchanged",
  // The most carefully worded label here. An output video exists and nothing
  // has analysed it, so "corrected" would be a claim nobody has checked.
  applied: "New video built, not yet checked",
  verifying: "Checking the new video",
  verified: "Checked: the finding is gone",
  rejected: "Rejected",
  cancelled: "Cancelled",
  superseded: "Superseded",
  failed: "Failed",
  verification_failed: "Checked: the finding is still there",
};

export function remediationLabel(state: RemediationState | string): string {
  return REMEDIATION_LABELS[state as RemediationState] ?? "Unknown";
}

/**
 * The tone a state is rendered in.
 *
 * `approved` is neutral, not a success: agreeing to a change is not the change
 * happening. `applied` is neutral too - a modified video whose compliance has
 * not been re-established is neither good news nor bad.
 */
export function toneForRemediation(state: RemediationState | string): StateTone {
  switch (state) {
    case "verified":
      return "success";
    case "failed":
    case "verification_failed":
      return "critical";
    case "processing":
    case "reconstructing":
    case "verifying":
      return "active";
    default:
      // `applied` lands here deliberately: a built video whose compliance has
      // not been re-established is neither good news nor bad.
      return "neutral";
  }
}

/** States from which the owner still has a decision to make. */
export function awaitsDecision(record: Remediation): boolean {
  return record.state === "proposed";
}

/**
 * Whether this record can still be withdrawn.
 *
 * Up to and including `asset_ready`. Once a worker holds it
 * (`reconstructing`) or an output exists, there is nothing to withdraw: the
 * backend's state machine refuses, and offering the control would be a lie.
 */
export function canCancel(record: Remediation): boolean {
  return (
    record.state === "proposed" ||
    record.state === "approved" ||
    record.state === "asset_ready"
  );
}

/**
 * Whether the owner still needs to supply a replacement image.
 *
 * Phase 9: this is the step that unblocks everything else, because no image
 * provider is configured.
 */
export function needsAsset(record: Remediation): boolean {
  return record.state === "approved" && !record.has_asset;
}

/** Whether building the corrected video is the next step. */
export function canExecute(record: Remediation): boolean {
  return record.state === "asset_ready";
}

/** Whether work is in flight on the backend, so the UI should poll. */
export function isExecuting(record: Remediation): boolean {
  return (
    record.state === "processing" ||
    record.state === "reconstructing" ||
    record.state === "applied" ||
    record.state === "verifying"
  );
}

/** Whether an output video exists that the owner may play. */
export function hasOutput(record: Remediation): boolean {
  return Boolean(record.has_output) && Boolean(record.output_video_id);
}

/**
 * Whether the targeted finding was shown to be gone.
 *
 * Reads the server's own evidence field. `null`/absent means "no verdict",
 * which this returns as false - absent is never permission to claim a fix.
 */
export function targetResolved(record: Remediation): boolean {
  return record.verification?.target_resolved === true;
}

/** Findings the output's analysis reported that are NOT the target. */
export function otherFindings(record: Remediation) {
  return record.verification?.other_findings ?? [];
}

/**
 * A reader-facing summary of the verdict, or null when there is none.
 *
 * Deliberately separates the three facts the backend keeps apart: whether the
 * target is gone, whether the configured policy passed, and what else is in
 * the output. It never merges them into one word.
 */
export function verdictSummary(record: Remediation): string | null {
  const verification = record.verification;
  if (!verification || !verification.outcome) return null;

  switch (verification.outcome) {
    case "verified":
      return verification.reason ?? "The targeted finding is no longer detected.";
    case "verification_failed":
      return (
        verification.reason ??
        "The new video was checked and the finding is still there."
      );
    case "pending":
      return "The new video is still being checked.";
    default:
      return (
        verification.reason ??
        "The check could not reach a conclusion about the new video."
      );
  }
}

/**
 * Whether a failure is worth offering a retry for.
 *
 * Only a `failed` record, and only while attempts remain. A
 * `verification_failed` record is NOT retryable: the correction was built and
 * checked, and it did not work - retrying the same asset would build the same
 * video. The user needs a different replacement image, which means a new
 * proposal.
 */
export function canRetryExecution(record: Remediation): boolean {
  if (record.state !== "failed") return false;
  const max = record.max_attempts ?? 3;
  return (record.attempt ?? 0) < max;
}

/**
 * Whether this record may be described as compliant.
 *
 * Reads the server's own field rather than re-deriving it. A response missing
 * the field is treated as "no": absent is not permission to claim compliance.
 */
export function claimsCompliance(record: Remediation): boolean {
  return record.is_compliance_claim === true;
}

/** Human text for the confidence gate recorded on the proposal. */
export function gateLabel(gate: string | undefined): string {
  switch (gate) {
    case "pass":
      return "Met the confidence threshold";
    case "below_threshold":
      return "Below the confidence threshold";
    case "not_reported":
      return "No confidence reported";
    default:
      return "Confidence unknown";
  }
}

export function toneForGate(gate: string | undefined): StateTone {
  return gate === "pass" ? "neutral" : "active";
}
