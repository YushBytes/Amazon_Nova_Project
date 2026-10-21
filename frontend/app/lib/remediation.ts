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
 */

import type { Remediation, RemediationState } from "./api";
import type { StateTone } from "./jobs";

/** Every state the backend's machine can report. */
export const REMEDIATION_STATES: readonly RemediationState[] = [
  "proposed",
  "approved",
  "processing",
  "asset_ready",
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
  approved: "Approved, not generated",
  processing: "Generating",
  asset_ready: "Asset ready, video unchanged",
  applied: "Applied, not verified",
  verifying: "Re-analysing",
  verified: "Verified by re-analysis",
  rejected: "Rejected",
  cancelled: "Cancelled",
  superseded: "Superseded",
  failed: "Failed",
  verification_failed: "Re-analysis did not confirm",
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
    case "verifying":
      return "active";
    default:
      return "neutral";
  }
}

/** States from which the owner still has a decision to make. */
export function awaitsDecision(record: Remediation): boolean {
  return record.state === "proposed";
}

/** Whether this record can still be withdrawn. */
export function canCancel(record: Remediation): boolean {
  return record.state === "proposed" || record.state === "approved";
}

/** Whether execution is the next step the backend would accept. */
export function canExecute(record: Remediation): boolean {
  return record.state === "approved";
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
